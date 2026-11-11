"""Samsung power frames and a real-HA native-backend adapter without live I/O."""
import asyncio
import base64
from datetime import timedelta
import logging
from pathlib import Path
import struct
import sys
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from homeassistant import loader
from homeassistant.components.media_player import MediaPlayerEntityFeature as Feature
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.ekf_appliances import media_player, protocol
from custom_components.ekf_appliances.runtime import ApplianceRuntime
from custom_components.ekf_appliances.samsung_tv import SamsungTvMediaPlayer


def wire_code(command):
    """Decode timings independently of the encoder and its command table."""
    raw = base64.b64decode(command[4:], validate=True)
    if len(raw) != 136:
        raise AssertionError(f"Expected 68 timings, got {len(raw) // 2}")
    timings = struct.unpack("<68H", raw)
    assert timings[:2] == (4480, 4480)
    assert all(mark == 560 for mark in timings[2:-1:2])
    assert all(space in (560, 1680) for space in timings[3:-2:2])
    assert sum(timings) == 108080
    return int("".join("1" if space == 1680 else "0" for space in timings[3:66:2]), 2)


class SamsungProtocolTests(unittest.TestCase):
    def test_literal_power_vectors_survive_raw_tuya_encoding(self):
        for key, expected in (
            ("power", 0xE0E040BF),
            ("power_on", 0xE0E09966),
            ("power_off", 0xE0E019E6),
        ):
            with self.subTest(key=key):
                self.assertEqual(
                    wire_code(protocol.encode_pulses(protocol.samsung_pulses(key))),
                    expected,
                )

    def test_power_on_matches_validated_live_timing_vector(self):
        # Decoded directly from the user's existing ON automation, whose
        # single frame was physically verified on 2026-11-11.
        expected = (
            4480, 4480, 560, 1680, 560, 1680, 560, 1680,
            560, 560, 560, 560, 560, 560, 560, 560,
            560, 560, 560, 1680, 560, 1680, 560, 1680,
            560, 560, 560, 560, 560, 560, 560, 560,
            560, 560, 560, 1680, 560, 560, 560, 560,
            560, 1680, 560, 1680, 560, 560, 560, 560,
            560, 1680, 560, 560, 560, 1680, 560, 1680,
            560, 560, 560, 560, 560, 1680, 560, 1680,
            560, 560, 560, 47040,
        )
        self.assertEqual(protocol.samsung_pulses("power_on"), expected)
        encoded = protocol.encode_pulses(protocol.samsung_pulses("power_on"))
        self.assertEqual(base64.b64decode(encoded[4:]), struct.pack("<68H", *expected))

    def test_unknown_samsung_command_is_rejected(self):
        with self.assertRaises(ValueError):
            protocol.samsung_pulses("invented_input")


CONFIG = {
    "remote_entity_id": "remote.ekf_connect_ir",
    "temperature_entity_id": "sensor.room_temperature",
    "projector_power_entity_id": "binary_sensor.projector_hdmi_connected",
    "tv_entity_id": "media_player.samsung_lan",
}
NATIVE_FEATURES = 24509
NATIVE_ATTRIBUTES = {
    "supported_features": NATIVE_FEATURES,
    "source": "TV", "source_list": ["TV", "HDMI"],
    "volume_level": 0.35, "is_volume_muted": False,
    "media_title": "Native title", "app_name": "Native app",
}


class SamsungAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        self.entry = await self.add_entry("ekf-test", "ekf_appliances", CONFIG)
        self.native_entry = await self.add_entry("samsung-test", "samsungtv", {"host": "192.0.2.10", "model": "UE48J5500"})
        self.native_device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.native_entry.entry_id, config_subentry_id=None,
            identifiers={("samsungtv", "native-tv-uuid")},
            name="Living room TV", manufacturer="Samsung", model="UE48J5500",
        )
        self.backend = er.async_get(self.hass).async_get_or_create(
            "media_player", "samsungtv", "native-tv-uuid",
            suggested_object_id="samsung_lan", config_entry=self.native_entry,
            device_id=self.native_device.id,
        )
        self.hass.states.async_set("remote.ekf_connect_ir", "on")
        self.set_native("off")
        self.commands = []
        self.native_calls = []
        self.sent = asyncio.Event()
        self.fail_ir = False
        self.feedback = False
        self.entities = []
        self.real_sleep = asyncio.sleep
        self.platform = EntityPlatform(
            hass=self.hass, logger=logging.getLogger(__name__),
            domain="media_player", platform_name="ekf_appliances", platform=None,
            scan_interval=timedelta(seconds=30), entity_namespace=None,
        )
        self.platform.config_entry = self.entry

        async def send(call):
            self.commands.append(dict(call.data))
            self.sent.set()
            if self.fail_ir:
                raise HomeAssistantError("IR failed")
            if self.feedback:
                code = wire_code(call.data["command"])
                current = self.hass.states.get(self.backend.entity_id).state
                target = "off" if code == 0xE0E019E6 else (
                    "on" if code == 0xE0E09966 or current == "off" else "off"
                )
                self.set_native(target)

        async def native(call):
            self.native_calls.append((call.service, dict(call.data)))

        self.hass.services.async_register("remote", "send_command", send)
        for service in (
            "turn_on", "turn_off", "toggle", "volume_up", "volume_down",
            "volume_set", "volume_mute", "select_source", "media_play",
            "media_pause", "media_play_pause", "media_stop", "media_next_track",
            "media_previous_track", "play_media",
        ):
            self.hass.services.async_register("media_player", service, native)
        self.runtime = ApplianceRuntime(self.hass, self.entry)
        self.coordinator = DataUpdateCoordinator(
            self.hass, logging.getLogger(__name__), config_entry=self.entry,
            name="test Samsung power", update_method=AsyncMock(return_value=False),
        )
        self.coordinator.async_set_updated_data(False)
        with patch(
            "custom_components.ekf_appliances.samsung_tv.SamsungPowerCoordinator",
            return_value=self.coordinator,
        ):
            self.player = SamsungTvMediaPlayer(self.runtime)
        self.no_delay = patch(
            "custom_components.ekf_appliances.transport.asyncio.sleep", self.fast_sleep
        )
        self.no_delay.start()

    async def add_entry(self, entry_id, domain, data):
        entry = ConfigEntry(
            entry_id=entry_id, data=data, domain=domain, options={},
            discovery_keys=MappingProxyType({}), subentries_data=[], title=domain,
            source="user", version=1, minor_version=1, unique_id=entry_id,
        )
        with patch.object(self.hass.config_entries, "async_setup", AsyncMock(return_value=True)):
            await self.hass.config_entries.async_add(entry)
        return entry

    def set_native(self, state, attrs=None):
        self.hass.states.async_set(
            self.backend.entity_id, state,
            NATIVE_ATTRIBUTES if attrs is None else attrs,
        )
        if hasattr(self, "coordinator"):
            self.coordinator.async_set_updated_data(
                True if state == "on" else False if state == "off" else None
            )

    async def fast_sleep(self, seconds):
        await self.real_sleep(0)

    async def attach(self):
        self.player.entity_id = "media_player.tv_adapter"
        await self.platform.async_add_entities([self.player])
        self.entities.append(self.player)

    async def asyncTearDown(self):
        await self.player.async_cancel_power()
        await self.platform.async_reset()
        self.no_delay.stop()
        await self.hass.async_block_till_done()
        self.tmp.cleanup()

    async def test_optional_backend_creates_no_tv_when_omitted(self):
        data = dict(CONFIG)
        data.pop("tv_entity_id")
        self.hass.config_entries.async_update_entry(self.entry, data=data)
        self.entry.runtime_data = ApplianceRuntime(self.hass, self.entry)
        added = []
        await media_player.async_setup_entry(self.hass, self.entry, added.extend)
        self.assertEqual(len(added), 1)
        self.assertIsInstance(added[0], media_player.ProjectorMediaPlayer)
        self.assertEqual(self.commands, [])

    async def test_adapter_binds_existing_device_without_merging_gateway(self):
        await self.attach()
        registered = er.async_get(self.hass).async_get(self.player.entity_id)
        self.assertIsNotNone(registered)
        self.assertEqual(registered.device_id, self.native_device.id)
        self.assertIsNone(self.player.device_info)
        self.assertEqual(self.player.device_entry.id, self.native_device.id)
        device = dr.async_get(self.hass).async_get(self.native_device.id)
        self.assertEqual(device.identifiers, {("samsungtv", "native-tv-uuid")})
        self.assertEqual(device.config_entry_id, self.native_entry.entry_id)
        self.assertEqual(len(dr.async_get(self.hass).devices), 1)
        self.assertEqual(self.player.unique_id, "ekf-test_samsung_native-tv-uuid")
        self.assertFalse(self.player.assumed_state)
        self.assertEqual(self.commands, [])

    async def test_backend_rename_preserves_identity_and_delegation_target(self):
        await self.attach()
        before = self.player.unique_id
        old_id = self.backend.entity_id
        self.backend = er.async_get(self.hass).async_update_entity(
            old_id, new_entity_id="media_player.tv_lan_renamed"
        )
        self.hass.states.async_remove(old_id)
        self.set_native("on")
        await self.hass.async_block_till_done()
        await self.player.async_volume_up()
        self.assertEqual(self.player.unique_id, before)
        self.assertEqual(self.player.state, "on")
        self.assertEqual(self.native_calls, [("volume_up", {"entity_id": self.backend.entity_id})])
        self.assertEqual(self.entry.data["tv_entity_id"], self.backend.entity_id)
        self.assertEqual(self.commands, [])

    async def test_rejects_self_or_non_native_backend(self):
        er.async_get(self.hass).async_get_or_create(
            "media_player", "ekf_appliances", "loop", suggested_object_id="recursive",
            config_entry=self.entry, device_id=self.native_device.id,
        )
        self.hass.states.async_set("media_player.recursive", "on")
        data = {**CONFIG, "tv_entity_id": "media_player.recursive"}
        self.hass.config_entries.async_update_entry(self.entry, data=data)
        with self.assertRaisesRegex(HomeAssistantError, "native Samsung"):
            SamsungTvMediaPlayer(ApplianceRuntime(self.hass, self.entry))

    async def test_state_capabilities_and_media_attributes_follow_native(self):
        await self.attach()
        self.set_native("on")
        await self.hass.async_block_till_done()
        state = self.hass.states.get(self.player.entity_id)
        self.assertEqual(state.state, "on")
        self.assertEqual(state.attributes["supported_features"], NATIVE_FEATURES)
        self.assertEqual(state.attributes["source_list"], ["TV", "HDMI"])
        self.assertEqual(state.attributes["volume_level"], 0.35)
        self.assertEqual(state.attributes["media_title"], "Native title")
        self.set_native("off")
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(self.player.entity_id).state, "off")
        self.assertEqual(self.commands, [])

    async def test_unknown_absolute_power_uses_discrete_frame_without_fake_state(self):
        await self.attach()
        for method, expected, observed in (
            (self.player.async_turn_on, 0xE0E09966, True),
            (self.player.async_turn_off, 0xE0E019E6, False),
        ):
            self.set_native("unknown")
            self.sent.clear()
            with patch("custom_components.ekf_appliances.samsung_tv.TV_TRANSITION_TIMEOUT", 0.5):
                request = asyncio.create_task(method())
                for _ in range(20):
                    if self.sent.is_set() or request.done():
                        break
                    await self.real_sleep(0)
                self.assertIsNone(self.player.state)
                if request.done():
                    await request
                self.assertEqual(wire_code(self.commands[-1]["command"]), expected)
                self.coordinator.async_set_updated_data(observed)
                await request
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(self.native_calls, [])

    async def test_unavailable_backend_and_transmitter_do_not_fake_state(self):
        await self.attach()
        self.set_native("unavailable")
        self.coordinator.async_set_updated_data(False)
        self.assertTrue(self.player.available)
        self.feedback = True
        await self.player.async_turn_on()
        self.assertEqual(wire_code(self.commands[0]["command"]), 0xE0E09966)
        self.commands.clear()
        self.set_native("on")
        self.hass.states.async_set("remote.ekf_connect_ir", "unavailable")
        self.assertTrue(self.player.available)
        await self.player.async_volume_up()
        with self.assertRaisesRegex(HomeAssistantError, "transmitter"):
            await self.player.async_turn_off()
        self.assertEqual(self.commands, [])

    async def test_power_is_idempotent_and_never_calls_native_power_services(self):
        self.feedback = True
        await self.player.async_turn_on()
        await self.player.async_turn_on()
        await self.player.async_turn_off()
        await self.player.async_turn_off()
        codes = [wire_code(call["command"]) for call in self.commands]
        self.assertEqual(codes[0], 0xE0E09966)
        self.assertEqual(codes[1:], [0xE0E019E6])
        self.assertEqual(self.native_calls, [])
        self.assertTrue(all(call["num_repeats"] == 1 and call["delay_secs"] == 0 for call in self.commands))

    async def test_duplicate_and_toggle_during_boot_share_one_frame(self):
        with patch("custom_components.ekf_appliances.samsung_tv.TV_TRANSITION_TIMEOUT", 0.5):
            first = asyncio.create_task(self.player.async_turn_on())
            await self.sent.wait()
            duplicate = asyncio.create_task(self.player.async_turn_on())
            toggle = asyncio.create_task(self.player.async_toggle())
            await self.real_sleep(0.01)
            self.assertEqual(len(self.commands), 1)
            self.assertEqual(self.player.state, "off")
            self.assertFalse(first.done())
            self.set_native("on")
            await asyncio.gather(first, duplicate, toggle)
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.native_calls, [])

    async def test_late_boot_confirmation_keeps_one_pending_power_frame(self):
        # Scale the observed 51-second cold boot to 0.51 seconds. The former
        # 35-second deadline must fail this scenario; a 75-second window passes.
        real_timeout = asyncio.timeout

        def scaled_timeout(seconds):
            return real_timeout(seconds / 100 if seconds in (35.0, 75.0) else seconds)

        with patch("custom_components.ekf_appliances.samsung_tv.asyncio.timeout", scaled_timeout):
            first = asyncio.create_task(self.player.async_turn_on())
            await self.sent.wait()
            duplicate = asyncio.create_task(self.player.async_turn_on())
            await self.real_sleep(0.51)
            waiting_for_feedback = not first.done() and not duplicate.done()
            before_feedback = self.player.state
            self.set_native("on")
            results = await asyncio.gather(first, duplicate, return_exceptions=True)
        self.assertTrue(waiting_for_feedback, "Cold boot must remain guarded after 51 seconds")
        self.assertEqual(before_feedback, "off")
        self.assertEqual(results, [None, None])
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(wire_code(self.commands[0]["command"]), 0xE0E09966)
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.player.state, "on")

    async def test_opposite_request_waits_for_boot_then_sends_one_off(self):
        with patch("custom_components.ekf_appliances.samsung_tv.TV_TRANSITION_TIMEOUT", 0.5):
            first = asyncio.create_task(self.player.async_turn_on())
            await self.sent.wait()
            opposite = asyncio.create_task(self.player.async_turn_off())
            await self.real_sleep(0.01)
            self.assertEqual(len(self.commands), 1)
            self.feedback = True
            self.set_native("on")
            await asyncio.gather(first, opposite)
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(wire_code(self.commands[-1]["command"]), 0xE0E019E6)
        self.assertEqual(self.player.state, "off")

    async def test_timeout_and_transport_failure_do_not_retry_or_invent_state(self):
        with patch("custom_components.ekf_appliances.samsung_tv.TV_TRANSITION_TIMEOUT", 0.03):
            results = await asyncio.gather(
                self.player.async_turn_on(), self.player.async_turn_on(), return_exceptions=True
            )
        self.assertTrue(all(isinstance(result, HomeAssistantError) for result in results))
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.player.state, "off")
        self.fail_ir = True
        with self.assertRaisesRegex(HomeAssistantError, "IR failed"):
            await self.player.async_turn_on()
        self.assertEqual(self.player.state, "off")
        self.assertEqual(len(self.commands), 2)

    async def test_cancelled_caller_keeps_guard_until_feedback(self):
        with patch("custom_components.ekf_appliances.samsung_tv.TV_TRANSITION_TIMEOUT", 0.5):
            first = asyncio.create_task(self.player.async_turn_on())
            await self.sent.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(self.player.async_turn_on())
            await self.real_sleep(0.01)
            self.assertEqual(len(self.commands), 1)
            self.set_native("on")
            await second
        self.assertEqual(len(self.commands), 1)

    async def test_unknown_explicit_toggle_sends_one_frame_and_coalesces(self):
        self.set_native("unknown")
        with patch("custom_components.ekf_appliances.samsung_tv.TV_TRANSITION_TIMEOUT", 0.03):
            await asyncio.gather(self.player.async_toggle(), self.player.async_toggle())
        self.assertEqual([wire_code(call["command"]) for call in self.commands], [0xE0E040BF])
        self.assertIsNone(self.player.state)

    async def test_nonpower_services_forward_exact_parameters_only_to_backend(self):
        self.set_native("on")
        await self.player.async_select_source("HDMI")
        await self.player.async_set_volume_level(0.4)
        await self.player.async_mute_volume(True)
        await self.player.async_volume_up()
        await self.player.async_volume_down()
        await self.player.async_media_play()
        await self.player.async_media_pause()
        await self.player.async_media_play_pause()
        await self.player.async_media_stop()
        await self.player.async_media_next_track()
        await self.player.async_media_previous_track()
        await self.player.async_play_media("channel", "7", extra={"title": "Channel"})
        expected = [
            ("select_source", {"source": "HDMI"}),
            ("volume_set", {"volume_level": 0.4}),
            ("volume_mute", {"is_volume_muted": True}),
            ("volume_up", {}), ("volume_down", {}), ("media_play", {}),
            ("media_pause", {}), ("media_play_pause", {}), ("media_stop", {}),
            ("media_next_track", {}), ("media_previous_track", {}),
            ("play_media", {"media_content_type": "channel", "media_content_id": "7", "extra": {"title": "Channel"}}),
        ]
        self.assertEqual(
            self.native_calls,
            [(service, {**data, "entity_id": self.backend.entity_id}) for service, data in expected],
        )
        self.assertEqual(self.commands, [])

    async def test_passive_probe_overrides_stale_native_on_without_optimism(self):
        await self.attach()
        self.set_native("on")
        self.coordinator.async_set_updated_data(False)
        await self.hass.async_block_till_done()
        self.assertEqual(self.player.state, "off")
        self.assertEqual(self.hass.states.get(self.player.entity_id).state, "off")
        self.assertNotIn("volume_level", self.hass.states.get(self.player.entity_id).attributes)
        await self.player.async_turn_off()
        self.assertEqual(self.commands, [])
        self.coordinator.async_set_updated_data(None)
        self.assertIsNone(self.player.state)
        self.assertEqual(self.commands, [])

    async def test_entity_removal_cancels_pending_power_without_late_commands(self):
        await self.attach()
        with patch("custom_components.ekf_appliances.samsung_tv.TV_TRANSITION_TIMEOUT", 0.5):
            task = asyncio.create_task(self.player.async_turn_on())
            await self.sent.wait()
            await self.platform.async_reset()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.native_calls, [])

    async def test_canonical_rename_preserves_same_device_and_probe_refresh(self):
        await self.attach()
        registry = er.async_get(self.hass)
        registry.async_update_entity(self.player.entity_id, new_entity_id="media_player.living_room_tv")
        await self.hass.async_block_till_done()
        self.coordinator.update_method.return_value = True
        await self.coordinator.async_refresh()
        self.coordinator.update_method.assert_awaited_once()
        await self.hass.async_block_till_done()
        registered = registry.async_get("media_player.living_room_tv")
        self.assertEqual(registered.device_id, self.native_device.id)
        self.assertEqual(self.hass.states.get(registered.entity_id).state, "on")
        self.assertEqual(self.commands, [])

    async def test_unknown_probe_keeps_working_native_volume_without_fake_power(self):
        self.set_native("on")
        self.coordinator.async_set_updated_data(None)
        self.hass.states.async_set("remote.ekf_connect_ir", "unavailable")
        self.assertTrue(self.player.available)
        self.assertIsNone(self.player.state)
        await self.player.async_volume_up()
        self.assertEqual(self.native_calls, [("volume_up", {"entity_id": self.backend.entity_id})])
        self.assertEqual(self.commands, [])

    async def test_uses_inherited_media_attribute_builder(self):
        from homeassistant.components.media_player import MediaPlayerEntity
        self.assertIs(SamsungTvMediaPlayer.state_attributes, MediaPlayerEntity.state_attributes)

    async def test_does_not_advertise_or_delegate_unsupported_native_features(self):
        self.set_native("on", {"supported_features": int(Feature.VOLUME_STEP), "source_list": ["TV"]})
        self.assertEqual(
            self.player.supported_features,
            Feature.VOLUME_STEP | Feature.TURN_ON | Feature.TURN_OFF,
        )
        with self.assertRaises(HomeAssistantError):
            await self.player.async_select_source("TV")
        self.assertEqual(self.native_calls, [])


class SamsungMediaRoutingTests(SamsungAdapterTests):
    async def configure_media_sources(self):
        registry = er.async_get(self.hass)
        config = {}
        for key, platform, object_id, unique_id in (
            ("tv_cast_entity_id", "cast", "living_cast", "cast-uuid"),
            ("tv_androidtv_entity_id", "androidtv_remote", "living_remote", "cast-mac"),
            ("tv_music_assistant_entity_id", "music_assistant", "living_music", "cast-uuid"),
            ("tv_kodi_entity_id", "kodi", "living_kodi", "kodi-uuid"),
        ):
            entry = await self.add_entry(platform + "-test", platform, {})
            entity = registry.async_get_or_create(
                "media_player", platform, unique_id,
                suggested_object_id=object_id, config_entry=entry,
            )
            config[key] = entity.entity_id
        config["tv_source_entity_id"] = "input_text.tv_source"
        self.hass.config_entries.async_update_entry(self.entry, data={**CONFIG, **config})
        self.runtime = ApplianceRuntime(self.hass, self.entry)
        with patch(
            "custom_components.ekf_appliances.samsung_tv.SamsungPowerCoordinator",
            return_value=self.coordinator,
        ):
            self.player = SamsungTvMediaPlayer(self.runtime)
        self.set_native("on")
        self.hass.states.async_set("input_text.tv_source", "chromecast")
        self.hass.states.async_set("media_player.living_remote", "on", {"supported_features": NATIVE_FEATURES})
        self.hass.states.async_set("media_player.living_cast", "playing", {
            "supported_features": NATIVE_FEATURES, "app_id": "C35B0678",
            "app_name": "Music Assistant", "media_title": "Cast flow",
            "volume_level": 0.2,
        })
        self.hass.states.async_set("media_player.living_music", "playing", {
            "supported_features": NATIVE_FEATURES, "app_id": "music_assistant",
            "source": "Music Assistant Queue", "active_queue": "cast-uuid",
            "media_title": "Current song", "volume_level": 0.2,
        })
        self.hass.states.async_set("media_player.living_kodi", "off", {"supported_features": NATIVE_FEATURES})
        await self.attach()
        await self.hass.async_block_till_done()

    async def test_music_assistant_owns_controls_and_metadata(self):
        await self.configure_media_sources()
        await self.player.async_media_pause()
        await self.player.async_set_volume_level(0.25)
        self.assertEqual(self.native_calls, [
            ("media_pause", {"entity_id": "media_player.living_music"}),
            ("volume_set", {"entity_id": "media_player.living_music", "volume_level": 0.25}),
        ])
        state = self.hass.states.get(self.player.entity_id)
        self.assertEqual(state.state, "playing")
        self.assertEqual(state.attributes["media_title"], "Current song")
        self.assertEqual(state.attributes["volume_level"], 0.2)
        self.assertEqual(self.commands, [])

    async def test_external_cast_replaces_stale_music_assistant_session(self):
        await self.configure_media_sources()
        self.hass.states.async_set("media_player.living_cast", "paused", {
            "supported_features": NATIVE_FEATURES, "app_id": "YouTube",
            "app_name": "YouTube", "media_title": "A video",
        })
        await self.hass.async_block_till_done()
        await self.player.async_media_play()
        self.assertEqual(self.native_calls, [("media_play", {"entity_id": "media_player.living_cast"})])
        self.assertEqual(self.hass.states.get(self.player.entity_id).state, "paused")
        self.assertEqual(self.hass.states.get(self.player.entity_id).attributes["media_title"], "A video")

    async def test_non_cast_android_app_uses_android_remote(self):
        await self.configure_media_sources()
        self.hass.states.async_set("media_player.living_cast", "off", {"supported_features": NATIVE_FEATURES})
        await self.hass.async_block_till_done()
        await self.player.async_media_pause()
        self.assertEqual(self.native_calls, [("media_pause", {"entity_id": "media_player.living_remote"})])
        self.assertEqual(self.player.state, "on")

    async def test_explicit_pc_source_does_not_control_background_cast(self):
        await self.configure_media_sources()
        self.hass.states.async_set("input_text.tv_source", "pc")
        self.hass.states.async_set("media_player.living_kodi", "playing", {
            "supported_features": NATIVE_FEATURES, "media_title": "Kodi movie",
        })
        await self.hass.async_block_till_done()
        await self.player.async_media_pause()
        self.assertEqual(self.native_calls, [("media_pause", {"entity_id": "media_player.living_kodi"})])
        self.assertEqual(self.hass.states.get(self.player.entity_id).attributes["media_title"], "Kodi movie")

    async def test_tv_off_overrides_background_music_and_blocks_media(self):
        await self.configure_media_sources()
        self.coordinator.async_set_updated_data(False)
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(self.player.entity_id).state, "off")
        self.assertNotIn("media_title", self.hass.states.get(self.player.entity_id).attributes)
        with self.assertRaises(HomeAssistantError):
            await self.player.async_media_pause()
        self.assertEqual(self.native_calls, [])

    async def test_other_music_player_cannot_take_over_cast(self):
        await self.configure_media_sources()
        er.async_get(self.hass).async_update_entity(
            "media_player.living_music", new_unique_id="another-physical-cast"
        )
        await self.hass.async_block_till_done()
        await self.player.async_media_pause()
        self.assertEqual(self.native_calls, [("media_pause", {"entity_id": "media_player.living_cast"})])

    async def test_cast_entity_rename_updates_routing_without_losing_identity(self):
        await self.configure_media_sources()
        before = self.player.unique_id
        er.async_get(self.hass).async_update_entity(
            "media_player.living_cast", new_entity_id="media_player.cast_renamed"
        )
        self.hass.states.async_remove("media_player.living_cast")
        self.hass.states.async_set("media_player.cast_renamed", "playing", {
            "supported_features": NATIVE_FEATURES, "app_id": "YouTube",
        })
        await self.hass.async_block_till_done()
        await self.player.async_media_pause()
        self.assertEqual(self.native_calls, [("media_pause", {"entity_id": "media_player.cast_renamed"})])
        self.assertEqual(self.entry.data["tv_cast_entity_id"], "media_player.cast_renamed")
        self.assertEqual(self.player.unique_id, before)

    async def test_projector_selection_does_not_steal_television_cast_controls(self):
        await self.configure_media_sources()
        self.hass.states.async_set("input_text.tv_source", "projector")
        self.hass.states.async_set("media_player.projector", "on")
        self.hass.states.async_set("media_player.living_kodi", "playing", {
            "supported_features": NATIVE_FEATURES, "media_title": "Projector movie",
        })
        await self.hass.async_block_till_done()
        await self.player.async_media_pause()
        self.assertEqual(self.native_calls, [("media_pause", {"entity_id": "media_player.living_music"})])
        self.assertEqual(self.hass.states.get(self.player.entity_id).attributes["media_title"], "Current song")

    async def test_pause_and_volume_capabilities_survive_all_sources_standby(self):
        await self.configure_media_sources()
        before = self.player.supported_features
        for entity in ("living_cast", "living_remote", "living_music", "living_kodi"):
            self.hass.states.async_set("media_player." + entity, "off", {
                "supported_features": NATIVE_FEATURES,
            })
        self.coordinator.async_set_updated_data(False)
        await self.hass.async_block_till_done()
        self.assertEqual(self.player.supported_features, before)
        required = Feature.PAUSE | Feature.PLAY | Feature.VOLUME_SET | Feature.VOLUME_STEP
        self.assertEqual(self.player.supported_features & required, required)
        with self.assertRaises(HomeAssistantError):
            await self.player.async_media_pause()
        self.assertEqual(self.native_calls, [])
