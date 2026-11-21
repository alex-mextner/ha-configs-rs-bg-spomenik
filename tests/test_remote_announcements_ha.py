"""Bounded HA runtime tests; services and cloud synthesis are external boundaries."""

import asyncio
import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml
from homeassistant import auth, loader
from homeassistant.components.input_select import InputSelect
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import (
    EVENT_STATE_CHANGED,
    Context,
    Event,
    HomeAssistant,
    State,
    SupportsResponse,
)
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.exceptions import Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame, restore_state
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.script import Script
from homeassistant.setup import async_setup_component

from custom_components import remote_announcements as integration
from custom_components.ekf_appliances.sensor import UniversalRemoteTarget
from custom_components.remote_announcements.providers import ProviderNotReadyError

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = yaml.safe_load((ROOT / "packages/remote_announcements.yaml").read_text())
CONFIG = integration.CONFIG_SCHEMA(PACKAGE)[integration.DOMAIN]
ROUTE_ATTRIBUTES = {
    "initialized": True,
    **PACKAGE["homeassistant"]["customize"][CONFIG["target_entity"]],
}


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the actual Home Assistant runtime with simulated speaker services."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.hass.config.skip_pip = True
        self.hass.config_entries = ConfigEntries(self.hass, {})
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        await restore_state.async_load(self.hass, load_empty=True)
        self.manager = integration.Manager(self.hass, CONFIG.copy())
        self.calls = []
        self.manual_volume = None
        self.fail_speech = False
        self.fail_pause_ack = False
        self.replace_content = False
        self.change_during_restore = False
        self.volume_sequence = []
        self.missing_speech_events = False
        self.hold_ack = {}
        self.ack_started = {}
        self.hold_restore_ack = None
        self.speech_started = []
        self.ignore_cancellation = False
        self.speech_gate = None
        self.entity = CONFIG["yandex_entity"]
        self.hass.states.async_set(CONFIG["target_entity"], "tv", ROUTE_ATTRIBUTES)
        self.hass.states.async_set(CONFIG["backend_entity"], "Яндекс")
        registry = er.async_get(self.hass)
        entry = registry.async_get_or_create(
            "media_player",
            "yandex_station",
            "fixture-mini",
            suggested_object_id=self.entity.split(".", 1)[1],
        )
        registry.async_update_entity(entry.entity_id, area_id=CONFIG["area"])
        self.hass.states.async_set(
            self.entity,
            "playing",
            {
                "volume_level": 0.2,
                "is_volume_muted": False,
                "media_content_id": "original-track",
                "alice_state": "IDLE",
            },
        )

        async def generate(_text) -> tuple[bytes, float]:
            return b"fixture-prepared-audio", 0.01

        await self.manager.cache.prepare(generate)
        self.manager.settings = {
            "dialogs": {
                "skill_id": "11111111-1111-1111-1111-111111111111",
                "sounds": {
                    clip.key: "22222222-2222-2222-2222-222222222222"
                    for clip in self.manager.cache.clips.values()
                },
            }
        }

        for service in (
            "volume_set",
            "volume_mute",
            "media_pause",
            "media_play",
            "play_media",
        ):
            self.hass.services.async_register("media_player", service, self.media)
        self.hass.services.async_register(
            "yandex_station",
            "send_command",
            self.native,
            supports_response=SupportsResponse.OPTIONAL,
        )

    def apply_volume(self, attrs, volume) -> None:
        attrs["volume_level"] = volume
        attrs["is_volume_muted"] = volume == 0
        if self.change_during_restore and volume == 0.2:
            attrs["volume_level"] = 0.8
            attrs["is_volume_muted"] = False

    async def simulate_speech(self, entity, value, attrs) -> bool:
        if self.speech_gate is not None:
            await self.speech_gate.wait()
        self.speech_started.append(entity)
        if self.fail_speech:
            message = "simulated transport failure"
            raise RuntimeError(message)
        if self.missing_speech_events:
            return False
        attrs["alice_state"] = "SPEAKING"
        self.hass.states.async_set(entity, value, attrs)
        await asyncio.sleep(0)
        for level in self.volume_sequence:
            attrs["volume_level"] = level
            self.hass.states.async_set(entity, value, attrs)
            await asyncio.sleep(0)
        attrs["alice_state"] = "IDLE"
        if self.manual_volume is not None:
            attrs["volume_level"] = self.manual_volume
        if self.replace_content:
            attrs["media_content_id"] = "new-user-track"
        return True

    async def acknowledge(self, call) -> None:
        hold = self.hold_ack.get(call.service)
        if call.service == "volume_set" and call.data["volume_level"] == 0.2:
            hold = self.hold_restore_ack or hold
        if hold:
            self.ack_started.setdefault(call.service, asyncio.Event()).set()
            while not hold.is_set():
                try:
                    await hold.wait()
                except asyncio.CancelledError:
                    if not self.ignore_cancellation:
                        raise

    async def media(self, call) -> None:
        self.calls.append((call.service, dict(call.data)))
        entity = call.data["entity_id"]
        state = self.hass.states.get(entity)
        attrs, value = dict(state.attributes), state.state
        if call.service == "volume_set":
            self.apply_volume(attrs, call.data["volume_level"])
        elif call.service == "volume_mute":
            attrs["is_volume_muted"] = call.data["is_volume_muted"]
        elif call.service == "media_pause":
            value = "paused"
            if self.fail_pause_ack:
                self.hass.states.async_set(entity, value, attrs)
                message = "simulated lost pause acknowledgement"
                raise TimeoutError(message)
        elif call.service == "media_play":
            value = "playing"
        elif call.service == "play_media" and not await self.simulate_speech(
            entity, value, attrs
        ):
            return
        self.hass.states.async_set(entity, value, attrs)
        await self.acknowledge(call)

    async def native(self, call) -> dict[str, str]:
        self.calls.append((call.service, dict(call.data)))
        entity = call.data["entity_id"]
        state = self.hass.states.get(entity)
        attrs, value = dict(state.attributes), state.state
        if await self.simulate_speech(entity, value, attrs):
            self.hass.states.async_set(entity, value, attrs)
        await self.acknowledge(call)
        return {"status": "SUCCESS"}

    async def asyncTearDown(self) -> None:
        await self.manager.queue.stop()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    def music_script(self, owner: str, output: str) -> Script:
        self.hass.states.async_set(
            "sensor.remote_announcements", "ready", self.manager.public_status
        )

        async def route(_call) -> dict:
            return {
                "entity_id": output,
                "speaker_entity_id": output,
                "owner": owner,
                "speaker_backend": "yandex",
            }

        async def no_op(_call) -> None:
            pass

        self.hass.services.async_register(
            "script",
            "music_remote_resolve",
            route,
            supports_response=SupportsResponse.ONLY,
        )
        self.hass.services.async_register("input_text", "set_value", no_op)
        self.manager.register_volume_service()
        music = yaml.safe_load((ROOT / "packages/music_remote.yaml").read_text())
        return Script(
            self.hass,
            cv.SCRIPT_SCHEMA(music["script"]["music_remote_command"]["sequence"]),
            "Music command fixture",
            "script",
        )

    async def check_absolute_digit_during_label(
        self, *, playing: bool, owner: str = "speaker", other_output: bool = False
    ) -> None:
        desired = 0.6 if playing else 0.4
        output = "media_player.other_output" if other_output else self.entity
        attrs = dict(
            self.hass.states.get(self.entity).attributes,
            volume_level=0.8,
            supported_features=4,
        )
        self.hass.states.async_set(
            self.entity, "playing" if playing else "paused", attrs
        )
        if other_output:
            self.hass.states.async_set(
                output, "idle", dict(attrs, volume_level=desired)
            )
        script = self.music_script(owner, output)

        async def digit_during_native(call) -> dict[str, str]:
            await script.async_run(
                {"command": "volume_set", "volume_level": desired}, context=Context()
            )
            return await self.native(call)

        self.hass.services.async_register(
            "yandex_station",
            "send_command",
            digit_during_native,
            supports_response=SupportsResponse.OPTIONAL,
        )
        await self.manager.announce("tv")
        speaker_levels = [
            d["volume_level"]
            for n, d in self.calls
            if n == "volume_set" and d["entity_id"] == self.entity
        ]
        self.assertEqual(speaker_levels, [desired, 0.8] if other_output else [desired])
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"],
            0.8 if other_output else desired,
        )
        if other_output:
            self.assertIn(
                ("volume_set", {"entity_id": output, "volume_level": desired}),
                self.calls,
            )

    async def test_digit_four_adopts_current_idle_label_level(self) -> None:
        await self.check_absolute_digit_during_label(playing=False)

    async def test_digit_six_adopts_current_playing_label_level(self) -> None:
        await self.check_absolute_digit_during_label(playing=True)

    async def test_digit_to_ma_output_preserves_speaker_restore(self) -> None:
        await self.check_absolute_digit_during_label(
            playing=False, owner="ma", other_output=True
        )

    async def test_digit_to_other_speaker_preserves_label_restore(self) -> None:
        await self.check_absolute_digit_during_label(playing=False, other_output=True)

    async def test_local_native_clip_preserves_queue_and_restores_volume(self) -> None:
        await self.manager.announce("tv")
        self.assertEqual(
            [name for name, data in self.calls],
            ["volume_set", "send_command", "volume_set"],
        )
        data = self.calls[1][1]
        self.assertEqual(data["command"], "serverAction")
        slot = data["serverActionEventPayload"]["payload"]["form_update"]["slots"][0]
        self.assertIn("dialogs-upload/", slot["value"])
        state = self.hass.states.get(self.entity)
        self.assertEqual(state.attributes["media_content_id"], "original-track")
        self.assertEqual(state.attributes["volume_level"], 0.2)
        self.assertEqual(self.manager.status, "ready")

    async def test_mode_labels_follow_actual_playback_and_restore_louder_volume(
        self,
    ) -> None:
        tv = "media_player.ue48j5500_ue48j5500"
        projector = "media_player.projector"
        # Orphan playback behind an off display and unrelated media do not count.
        self.hass.states.async_set("media_player.home", "playing")
        self.hass.states.async_set(CONFIG["aisis_entity"], "playing")
        self.hass.states.async_set("media_player.gostinaia", "playing")
        original = dict(self.hass.states.get(self.entity).attributes, volume_level=0.8)
        for tv_state, projector_state, speaker_state, expected in (
            ("playing", "paused", "idle", 0.6),
            ("on", "playing", "paused", 0.6),
            ("off", "unavailable", "playing", 0.6),
            ("on", "paused", "idle", 0.4),
            ("off", "off", "idle", 0.4),
            ("paused", "idle", "paused", 0.4),
            ("buffering", "unknown", "idle", 0.4),
        ):
            with self.subTest(
                tv=tv_state, projector=projector_state, speaker=speaker_state
            ):
                self.hass.states.async_set(tv, tv_state)
                self.hass.states.async_set(projector, projector_state)
                self.hass.states.async_set(self.entity, speaker_state, original)
                self.calls.clear()
                await self.manager.announce("tv")
                self.assertEqual(
                    [name for name, _ in self.calls],
                    [
                        "volume_set",
                        "send_command",
                        "volume_set",
                    ],
                )
                self.assertEqual(self.calls[0][1]["volume_level"], expected)
                self.assertEqual(self.calls[-1][1]["volume_level"], 0.8)
                self.assertEqual(self.hass.states.get(self.entity).state, speaker_state)

    async def test_stale_backing_player_and_assumed_yandex_are_not_playback(
        self,
    ) -> None:
        tv = "media_player.ue48j5500_ue48j5500"
        projector = "media_player.projector"
        for display, owner_key, owner in (
            (tv, "media_control_entity_id", "media_player.gostinaia_2"),
            (projector, "paired_player_entity_id", "media_player.home"),
        ):
            with self.subTest(display=display):
                self.hass.states.async_set(
                    self.entity, "playing", {"assumed_state": True}
                )
                self.hass.states.async_set(display, "playing", {owner_key: owner})
                for state, attrs in (
                    ("playing", {"restored": True}),
                    ("paused", {}),
                    ("off", {}),
                ):
                    self.hass.states.async_set(owner, state, attrs)
                    self.assertEqual(self.manager.label_volume(0.8), 0.4)
                self.hass.states.async_set(owner, "playing")
                self.assertEqual(self.manager.label_volume(0.8), 0.6)
                self.hass.states.async_set(display, "off")

    async def test_real_route_publishes_customized_sources_on_each_update(self) -> None:
        self.hass.states.async_remove(CONFIG["target_entity"])
        await async_process_ha_core_config(self.hass, PACKAGE["homeassistant"])
        mode = SimpleNamespace(
            target="tv", pair="tv", native=False, expires_at=None, shutting_down=False
        )
        controller = SimpleNamespace(
            mode=mode, initialized=True, subscribe=lambda _listener: lambda: None
        )
        entry = SimpleNamespace(
            entry_id="fixture",
            runtime_data=SimpleNamespace(remote_controller=controller),
        )
        route = UniversalRemoteTarget(entry)
        component = EntityComponent(logging.getLogger(__name__), "sensor", self.hass)
        await component.async_add_entities([route])
        for target in ("tv", "kodi"):
            mode.target = target
            route.async_write_ha_state()
            state = self.hass.states.get(CONFIG["target_entity"])
            self.assertEqual(state.state, target)
            self.assertEqual(
                state.attributes["remote_announcement_playback_entities"],
                ROUTE_ATTRIBUTES["remote_announcement_playback_entities"],
            )
            self.assertEqual(self.manager.label_volume(0.8), 0.6)

    async def test_unconfigured_legacy_package_keeps_its_speech_floor(self) -> None:
        self.hass.states.async_set(CONFIG["target_entity"], "tv", {"initialized": True})
        self.assertEqual(self.manager.label_volume(0.2), 0.5)
        self.assertEqual(self.manager.label_volume(0.8), 0.8)

    async def test_malformed_playback_sources_use_quiet_level(self) -> None:
        for sources in (
            None,
            "playing",
            {},
            [10],
            ["light.kitchen"],
            ["media_player.bad id"],
        ):
            self.hass.states.async_set(
                CONFIG["target_entity"],
                "tv",
                {"remote_announcement_playback_entities": sources},
            )
            self.assertEqual(self.manager.label_volume(0.8), 0.4)

    async def test_restored_playback_cannot_raise_idle_label(self) -> None:
        self.hass.states.async_set(
            "media_player.projector", "playing", {"restored": True}
        )
        state = self.hass.states.get(self.entity)
        self.hass.states.async_set(self.entity, "paused", state.attributes)
        await self.manager.announce("tv")
        self.assertEqual(self.calls[0][1]["volume_level"], 0.4)

    async def test_queued_label_resamples_music_and_ignores_speech_only_echo(
        self,
    ) -> None:
        self.manager.queue.debounce = 0
        for track, expected in ((None, 0.4), ("new-track", 0.6)):
            with self.subTest(track=track):
                self.calls.clear()
                self.hass.states.async_set(
                    CONFIG["target_entity"], "tv", ROUTE_ATTRIBUTES
                )
                self.hass.states.async_set(
                    self.entity,
                    "idle",
                    {
                        "alice_state": "IDLE",
                        "volume_level": 0.2,
                    },
                )

                async def native(call, content=track) -> dict[str, str]:
                    result = await self.native(call)
                    if self.manager.current_target() == "tv":
                        self.hass.states.async_set(
                            CONFIG["target_entity"], "kodi", ROUTE_ATTRIBUTES
                        )
                        self.manager.queue.submit("kodi")
                        state = self.hass.states.get(self.entity)
                        self.hass.states.async_set(
                            self.entity,
                            "playing",
                            {
                                **state.attributes,
                                "alice_state": "SPEAKING",
                                "media_content_id": content,
                            },
                        )
                    return result

                self.hass.services.async_register(
                    "yandex_station",
                    "send_command",
                    native,
                    supports_response=SupportsResponse.OPTIONAL,
                )
                self.manager.queue.submit("tv")
                await self.manager.queue.drain()
                self.assertEqual(
                    [
                        data["volume_level"]
                        for name, data in self.calls
                        if name == "volume_set"
                    ],
                    [0.4, 0.2, expected, 0.2],
                )

    async def test_muted_speaker_gets_label_volume_then_original_level_and_mute(
        self,
    ) -> None:
        state = self.hass.states.get(self.entity)
        self.hass.states.async_set(
            self.entity, state.state, {**state.attributes, "is_volume_muted": True}
        )
        await self.manager.announce("tv")
        self.assertEqual(
            self.calls[0],
            (
                "volume_set",
                {
                    "entity_id": self.entity,
                    "volume_level": 0.6,
                },
            ),
        )
        self.assertEqual(self.calls[-1][0], "volume_mute")
        state = self.hass.states.get(self.entity)
        self.assertTrue(state.attributes["is_volume_muted"])
        self.assertEqual(state.attributes["volume_level"], 0.2)

    async def test_next_label_cannot_capture_unconfirmed_yandex_restore(self) -> None:
        async def delayed_restore(call) -> None:
            if call.data["volume_level"] == 0.2:
                self.calls.append((call.service, dict(call.data)))
                return
            await self.media(call)

        self.hass.services.async_register("media_player", "volume_set", delayed_restore)
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.15),
            patch.object(integration, "RECOVERY_RESERVE", 0.08),
        ):
            await self.manager.announce("tv")
        state = self.hass.states.get(self.entity)
        self.hass.states.async_set(self.entity, "paused", state.attributes)
        self.hass.states.async_set(CONFIG["target_entity"], "kodi", ROUTE_ATTRIBUTES)
        calls_after_restore = list(self.calls)
        with self.assertRaisesRegex(
            ProviderNotReadyError, "^yandex_volume_recovery_pending$"
        ):
            await self.manager.announce("kodi")
        self.assertEqual(self.calls, calls_after_restore)
        # A real human level, different from our pending restore and override,
        # becomes the next baseline without replaying the earlier restore.
        self.hass.states.async_set(
            self.entity, "paused", {**state.attributes, "volume_level": 0.8}
        )
        self.hass.services.async_register("media_player", "volume_set", self.media)
        await self.manager.announce("kodi")
        self.assertEqual(
            [d["volume_level"] for n, d in self.calls if n == "volume_set"],
            [0.6, 0.2, 0.4, 0.8],
        )

    async def test_yandex_restore_waits_for_echo_before_queued_label(self) -> None:
        first_restore, waiting = asyncio.Event(), asyncio.Event()
        original_check = self.manager.confirm_restored_volume

        async def observe_check(playback, expected) -> None:
            waiting.set()
            await original_check(playback, expected)

        async def delayed_restore(call) -> None:
            if call.data["volume_level"] == 0.2 and not first_restore.is_set():
                self.calls.append((call.service, dict(call.data)))
                first_restore.set()
                return
            await self.media(call)

        self.hass.services.async_register("media_player", "volume_set", delayed_restore)
        with patch.object(self.manager, "confirm_restored_volume", observe_check):
            self.manager.queue.debounce = 0
            self.manager.queue.submit("tv")
            await asyncio.wait_for(first_restore.wait(), 0.3)
            self.hass.states.async_set(
                CONFIG["target_entity"], "kodi", ROUTE_ATTRIBUTES
            )
            self.manager.queue.submit("kodi")
            await asyncio.wait_for(waiting.wait(), 0.3)
            state = self.hass.states.get(self.entity)
            self.hass.states.async_set(
                self.entity, "paused", {**state.attributes, "volume_level": 0.2}
            )
            await self.manager.queue.drain()
        self.assertEqual(
            [d["volume_level"] for n, d in self.calls if n == "volume_set"],
            [0.6, 0.2, 0.4, 0.2],
        )
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.2
        )

    async def start_delayed_restore(self, *, muted: bool = True) -> dict:
        attrs = dict(
            self.hass.states.get(self.entity).attributes,
            volume_level=0.8,
            is_volume_muted=muted,
            supported_features=4,
        )
        self.hass.states.async_set(self.entity, "paused", attrs)

        async def delayed(call) -> None:
            if self.calls:
                self.calls.append((call.service, dict(call.data)))
                return
            await self.media(call)

        self.hass.services.async_register("media_player", "volume_set", delayed)
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.1),
            patch.object(integration, "RECOVERY_RESERVE", 0.06),
        ):
            await self.manager.announce("tv")
        self.assertEqual(
            [n for n, _ in self.calls], ["volume_set", "send_command", "volume_set"]
        )
        return attrs

    async def check_digit_before_old_echo(
        self, *, muted: bool, lost_ack: bool = False
    ) -> None:
        attrs = await self.start_delayed_restore(muted=muted)
        if lost_ack:

            async def manual_ack_lost(call) -> None:
                self.calls.append((call.service, dict(call.data)))
                message = "manual-volume-ack-lost"
                raise RuntimeError(message)

            self.hass.services.async_register(
                "media_player", "volume_set", manual_ack_lost
            )
        script = self.music_script("speaker", self.entity)
        entered = asyncio.Event()
        handoff = self.manager.handoff_volume

        async def heard(*args: object) -> bool:
            entered.set()
            return await handoff(*args)

        with patch.object(self.manager, "handoff_volume", heard):
            command = asyncio.create_task(
                script.async_run(
                    {"command": "volume_set", "volume_level": 0.4}, context=Context()
                )
            )
            await asyncio.wait_for(entered.wait(), 0.5)
            self.assertEqual(len(self.calls), 3)
            self.hass.states.async_set(
                self.entity, "paused", {**attrs, "is_volume_muted": False}
            )
            if lost_ack:
                with self.assertRaisesRegex(RuntimeError, "manual-volume-ack-lost"):
                    await asyncio.wait_for(command, 0.5)
            else:
                await asyncio.wait_for(command, 0.5)
        if pending := self.manager.pending_remutes.get(self.entity):
            await asyncio.gather(pending[1], return_exceptions=True)
        levels = [d["volume_level"] for n, d in self.calls if n == "volume_set"]
        self.assertEqual(levels, [0.4, 0.8, 0.4])
        self.assertNotIn("volume_mute", [n for n, _ in self.calls])
        with self.assertRaisesRegex(
            ProviderNotReadyError, "^yandex_volume_recovery_pending$"
        ):
            await self.manager.announce("tv")
        self.hass.states.async_set(
            self.entity,
            "paused",
            {**attrs, "volume_level": 0.4, "is_volume_muted": False},
        )
        await self.manager.announce("tv")
        self.assertEqual(
            [d["volume_level"] for n, d in self.calls if n == "volume_set"], levels
        )

    async def test_digit_waits_for_old_echo_then_protects_new_choice(self) -> None:
        await self.check_digit_before_old_echo(muted=True)

    async def test_lost_digit_ack_cannot_allow_stale_restore_or_remute(self) -> None:
        await self.check_digit_before_old_echo(muted=True, lost_ack=True)

    async def test_digit_after_distinct_manual_level_reaches_native(self) -> None:
        attrs = await self.start_delayed_restore(muted=False)
        self.hass.states.async_set(
            self.entity, "paused", {**attrs, "volume_level": 0.7}
        )
        self.hass.services.async_register("media_player", "volume_set", self.media)
        script = self.music_script("speaker", self.entity)
        await script.async_run(
            {"command": "volume_set", "volume_level": 0.9}, context=Context()
        )
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.9
        )
        self.assertFalse(self.manager.volume_recovery)
        self.assertEqual(
            [d["volume_level"] for n, d in self.calls if n == "volume_set"],
            [0.4, 0.8, 0.9],
        )

    async def test_digit_after_unadmitted_plain_restore_reaches_native(self) -> None:
        attrs = dict(
            self.hass.states.get(self.entity).attributes,
            volume_level=0.8,
            is_volume_muted=False,
            supported_features=4,
        )
        self.hass.states.async_set(self.entity, "paused", attrs)
        dispatch = self.manager.call

        async def expire_restore(service: str, entity: str, **kwargs: object) -> bool:
            if service == "volume_set" and kwargs.get("volume_level") == 0.8:
                kwargs["deadline"] = 0
            return await dispatch(service, entity, **kwargs)

        with patch.object(self.manager, "call", expire_restore):
            await self.manager.announce("tv")
        self.assertEqual([n for n, _ in self.calls], ["volume_set", "send_command"])
        script = self.music_script("speaker", self.entity)
        await script.async_run(
            {"command": "volume_set", "volume_level": 0.7}, context=Context()
        )
        self.assertEqual(
            [d["volume_level"] for n, d in self.calls if n == "volume_set"], [0.4, 0.7]
        )
        self.assertFalse(self.manager.volume_recovery)

    async def test_new_digits_survive_lost_restore_and_selection_echoes(self) -> None:
        attrs = await self.start_delayed_restore(muted=False)
        script = self.music_script("speaker", self.entity)
        for level in (0.7, 0.5):
            await asyncio.wait_for(
                script.async_run(
                    {"command": "volume_set", "volume_level": level}, context=Context()
                ),
                0.8,
            )
        levels = [d["volume_level"] for n, d in self.calls if n == "volume_set"]
        self.assertEqual(levels, [0.4, 0.8, 0.7, 0.5])
        for stale in (0.8, 0.4, 0.7):
            self.hass.states.async_set(
                self.entity, "paused", {**attrs, "volume_level": stale}
            )
            await self.hass.async_block_till_done()
            with self.assertRaisesRegex(ProviderNotReadyError, "recovery_pending"):
                self.manager.check_aisis_volume_recovery(
                    self.hass.states.get(self.entity)
                )
        # There is no intervening label or digit to sample the requested echo.
        self.hass.states.async_set(
            self.entity, "paused", {**attrs, "volume_level": 0.5}
        )
        self.hass.states.async_set(
            self.entity,
            "paused",
            {**attrs, "volume_level": 0.8, "is_volume_muted": True},
        )
        await self.hass.async_block_till_done()
        self.assertFalse(self.manager.volume_recovery)
        self.assertEqual(len(self.calls), 5)

    async def test_different_digit_before_restore_admission_has_priority(self) -> None:
        attrs = dict(
            self.hass.states.get(self.entity).attributes,
            volume_level=0.8,
            supported_features=4,
            is_volume_muted=True,
        )
        self.hass.states.async_set(self.entity, "paused", attrs)
        script = self.music_script("speaker", self.entity)

        async def silent_manual(call) -> None:
            if call.data["volume_level"] == 0.7:
                self.calls.append((call.service, dict(call.data)))
                return
            await self.media(call)

        async def before_recovery(call) -> dict[str, str]:
            await script.async_run(
                {"command": "volume_set", "volume_level": 0.7}, context=Context()
            )
            return await self.native(call)

        self.hass.services.async_register("media_player", "volume_set", silent_manual)
        self.hass.services.async_register(
            "yandex_station",
            "send_command",
            before_recovery,
            supports_response=SupportsResponse.OPTIONAL,
        )
        await self.manager.announce("tv")
        self.assertEqual(
            [d["volume_level"] for n, d in self.calls if n == "volume_set"], [0.4, 0.7]
        )
        self.assertNotIn("volume_mute", [n for n, _ in self.calls])

    async def test_stalled_native_digit_ack_allows_next_explicit_digit(self) -> None:
        await self.start_delayed_restore(muted=False)
        release = asyncio.Event()

        async def stalled(call) -> None:
            if call.data["volume_level"] != 0.7:
                await self.media(call)
                return
            self.calls.append((call.service, dict(call.data)))
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        script = self.music_script("speaker", self.entity)
        self.hass.services.async_register("media_player", "volume_set", stalled)
        try:
            with patch.object(integration, "ATTEMPT_BUDGET", 0.05):
                with self.assertRaises(integration.ServiceTimeoutError):
                    await script.async_run(
                        {"command": "volume_set", "volume_level": 0.7},
                        context=Context(),
                    )
                await asyncio.wait_for(
                    script.async_run(
                        {"command": "volume_set", "volume_level": 0.5},
                        context=Context(),
                    ),
                    0.5,
                )
        finally:
            release.set()
        self.assertEqual(
            [d["volume_level"] for n, d in self.calls if n == "volume_set"],
            [0.4, 0.8, 0.7, 0.5],
        )
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.5
        )

    async def check_digit_preempts_scheduled_label_level(self, selected: float) -> None:
        attrs = dict(
            self.hass.states.get(self.entity).attributes,
            volume_level=0.8,
            is_volume_muted=True,
            supported_features=4,
        )
        self.hass.states.async_set(self.entity, "paused", attrs)
        script = self.music_script("speaker", self.entity)
        scheduled, release = asyncio.Event(), asyncio.Event()
        create_task = asyncio.create_task

        def schedule(coro, **kwargs: object) -> asyncio.Task:
            # Delay only the first actual ServiceCalls.invoke task before it runs.
            if (
                kwargs.get("name") != "remote-announcement:media_player.volume_set"
                or scheduled.is_set()
            ):
                return create_task(coro, **kwargs)
            scheduled.set()

            async def after_digit() -> bool:
                await release.wait()
                return await coro

            return create_task(after_digit(), **kwargs)

        with patch.object(asyncio, "create_task", schedule):
            label = create_task(self.manager.announce("tv"))
            try:
                await asyncio.wait_for(scheduled.wait(), 0.5)
                playback = self.manager.active_label
                self.assertFalse(self.calls)
                await script.async_run(
                    {"command": "volume_set", "volume_level": selected},
                    context=Context(),
                )
            finally:
                release.set()
                await asyncio.wait_for(label, 0.8)
        self.assertEqual(
            [d["volume_level"] for n, d in self.calls if n == "volume_set"], [selected]
        )
        self.assertNotIn("volume_mute", [n for n, _ in self.calls])
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], selected
        )
        self.assertEqual(
            [n for n, _ in self.calls].count("send_command"), int(selected == 0.4)
        )
        self.assertFalse(playback.raised)
        self.assertFalse(self.manager.volume_recovery)

    async def test_digit_during_owned_pause_keeps_volume_and_recovers_music(
        self,
    ) -> None:
        self.manager.config["music_mode"] = "pause"
        state = self.hass.states.get(self.entity)
        self.hass.states.async_set(
            self.entity, "playing", {**state.attributes, "supported_features": 4}
        )
        release, entered = asyncio.Event(), asyncio.Event()
        self.hold_ack["media_pause"] = release
        self.ack_started["media_pause"] = entered
        script = self.music_script("speaker", self.entity)
        label = asyncio.create_task(self.manager.announce("tv"))
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            await script.async_run(
                {"command": "volume_set", "volume_level": 0.7}, context=Context()
            )
        finally:
            release.set()
            await asyncio.wait_for(label, 0.8)
        self.assertEqual(
            [n for n, _ in self.calls], ["media_pause", "volume_set", "media_play"]
        )
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.7
        )
        self.assertEqual(self.hass.states.get(self.entity).state, "playing")

    async def test_distinct_digit_preempts_scheduled_label_level(self) -> None:
        await self.check_digit_preempts_scheduled_label_level(0.7)

    async def test_matching_digit_preempts_scheduled_label_level(self) -> None:
        await self.check_digit_preempts_scheduled_label_level(0.4)

    async def test_same_digit_before_label_level_echo_dispatches_once(self) -> None:
        attrs = dict(
            self.hass.states.get(self.entity).attributes,
            volume_level=0.8,
            supported_features=4,
        )
        self.hass.states.async_set(self.entity, "paused", attrs)
        script = self.music_script("speaker", self.entity)

        async def no_echo(call) -> None:
            self.calls.append((call.service, dict(call.data)))

        async def digit(call) -> dict[str, str]:
            await script.async_run(
                {"command": "volume_set", "volume_level": 0.4}, context=Context()
            )
            return await self.native(call)

        self.hass.services.async_register("media_player", "volume_set", no_echo)
        self.hass.services.async_register(
            "yandex_station",
            "send_command",
            digit,
            supports_response=SupportsResponse.OPTIONAL,
        )
        await self.manager.announce("tv")
        self.assertEqual(
            [d["volume_level"] for n, d in self.calls if n == "volume_set"], [0.4, 0.4]
        )
        self.assertTrue(self.manager.volume_recovery)

    async def test_aisis_remote_digit_supersedes_discarded_native_restore(self) -> None:
        entity = self.prepare_aisis_native_volume(0.0)
        self.aisis_restore_ack_only = True
        state = self.hass.states.get(entity)
        self.hass.states.async_set(
            entity, state.state, {**state.attributes, "supported_features": 4}
        )
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.15),
            patch.object(integration, "RECOVERY_RESERVE", 0.08),
        ):
            await self.manager.announce("tv")
        await self.music_script("speaker", entity).async_run(
            {"command": "volume_set", "volume_level": 0.8}, context=Context()
        )
        self.assertEqual(self.aisis_native_levels, [4, 0, 8])
        self.assertFalse(self.manager.volume_recovery)

    async def test_volume_service_optional_response_schema_and_permission(self) -> None:
        self.manager.register_volume_service()
        data = {"entity_id": self.entity, "volume_level": 0.7}

        async def call(*, response=False, context=None, payload=None) -> dict | None:
            return await self.hass.services.async_call(
                integration.DOMAIN,
                "adopt_label_volume",
                data if payload is None else payload,
                blocking=True,
                return_response=response,
                context=context,
            )

        self.assertIsNone(await call())
        self.assertEqual(await call(response=True), {"status": "not_applicable"})
        with self.assertRaises(cv.vol.Invalid):
            await call(payload={**data, "volume_level": 1.1})
        self.hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=None))
        context = Context(user_id="missing-user")
        with self.assertRaises(UnknownUser):
            await call(context=context)
        self.hass.auth.async_get_user.return_value = SimpleNamespace(
            is_admin=False, permissions=SimpleNamespace(check_entity=lambda *_: False)
        )
        with self.assertRaises(Unauthorized):
            await call(context=context)
        self.assertFalse(self.calls)
        playback = self.manager.playback("yandex", self.entity)
        state = self.hass.states.get(self.entity)
        self.hass.states.async_set(
            self.entity,
            state.state,
            {**state.attributes, "volume_level": playback.applied},
        )
        playback.raised = True
        self.manager.active_label = playback
        self.assertEqual(
            await call(
                response=True, payload={**data, "volume_level": playback.applied}
            ),
            {"status": "kept"},
        )
        self.assertFalse(self.calls)
        self.assertEqual(await call(response=True), {"status": "sent"})
        self.assertEqual(
            [d["volume_level"] for n, d in self.calls if n == "volume_set"], [0.7]
        )

    async def test_digit_waiting_on_remute_lock_fences_restore_admitted_inside(
        self,
    ) -> None:
        attrs = dict(
            self.hass.states.get(self.entity).attributes,
            volume_level=0.8,
            is_volume_muted=True,
            supported_features=4,
        )
        self.hass.states.async_set(self.entity, "paused", attrs)
        before_admission, entered, release = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        dispatch = self.manager.call
        handoff = self.manager.handoff_volume
        script = self.music_script("speaker", self.entity)

        async def delay_admission(service, entity, **kwargs: object) -> bool:
            if service == "volume_set" and kwargs.get("volume_level") == 0.8:
                before_admission.set()
                await release.wait()
            return await dispatch(service, entity, **kwargs)

        async def selected(*args: object) -> None:
            entered.set()
            await handoff(*args)

        async def native(call) -> None:
            if call.data["volume_level"] == 0.4:
                await self.media(call)
                return
            self.calls.append((call.service, dict(call.data)))

        self.hass.services.async_register("media_player", "volume_set", native)
        with (
            patch.object(self.manager, "call", delay_admission),
            patch.object(self.manager, "handoff_volume", selected),
        ):
            label = asyncio.create_task(self.manager.announce("tv"))
            await asyncio.wait_for(before_admission.wait(), 0.5)
            self.assertFalse(self.manager.volume_recovery)
            digit = asyncio.create_task(
                script.async_run(
                    {"command": "volume_set", "volume_level": 0.7}, context=Context()
                )
            )
            await asyncio.wait_for(entered.wait(), 0.5)
            release.set()
            await asyncio.wait_for(asyncio.gather(digit, label), 0.8)
        self.assertEqual(
            [d["volume_level"] for n, d in self.calls if n == "volume_set"],
            [0.4, 0.8, 0.7],
        )
        self.hass.states.async_set(
            self.entity, "paused", {**attrs, "is_volume_muted": False}
        )
        await self.hass.async_block_till_done()
        with self.assertRaisesRegex(ProviderNotReadyError, "recovery_pending"):
            self.manager.check_aisis_volume_recovery(self.hass.states.get(self.entity))
        self.hass.states.async_set(
            self.entity,
            "paused",
            {**attrs, "volume_level": 0.7, "is_volume_muted": False},
        )
        await self.hass.async_block_till_done()
        self.assertFalse(self.manager.volume_recovery)

    async def test_digit_protects_new_choice_after_unmuted_restore(self) -> None:
        await self.check_digit_before_old_echo(muted=False)

    async def test_manual_level_during_late_restore_yields(self) -> None:
        attrs = await self.start_delayed_restore()
        task = self.manager.pending_remutes[self.entity][1]
        self.hass.states.async_set(
            self.entity,
            "paused",
            {**attrs, "volume_level": 0.7, "is_volume_muted": False},
        )
        await asyncio.wait_for(task, 0.5)
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(self.manager.volume_recovery)

    async def test_manual_mute_during_late_restore_yields(self) -> None:
        attrs = await self.start_delayed_restore()
        task = self.manager.pending_remutes[self.entity][1]
        self.hass.states.async_set(
            self.entity,
            "paused",
            {**attrs, "volume_level": 0.4, "is_volume_muted": True},
        )
        await asyncio.wait_for(task, 0.5)
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(self.manager.volume_recovery)

    async def test_shutdown_cancels_late_remute_before_echo(self) -> None:
        attrs = await self.start_delayed_restore()
        task = self.manager.pending_remutes[self.entity][1]
        await self.manager.close()
        self.hass.states.async_set(
            self.entity, "paused", {**attrs, "is_volume_muted": False}
        )
        await self.hass.async_block_till_done()
        self.assertTrue(task.done())
        self.assertFalse(self.manager.pending_remutes)
        self.assertFalse(self.manager.volume_waiters)
        self.assertEqual(len(self.calls), 3)

    async def test_late_echo_after_continuation_expiry_never_remutes(self) -> None:
        with patch.object(integration, "REMUTE_GRACE", 0.04):
            attrs = await self.start_delayed_restore()
            task = self.manager.pending_remutes[self.entity][1]
            with self.assertRaisesRegex(
                ProviderNotReadyError, "yandex_volume_recovery_unconfirmed"
            ):
                await asyncio.wait_for(task, 0.5)
        self.hass.states.async_set(
            self.entity, "paused", {**attrs, "is_volume_muted": False}
        )
        self.manager.check_aisis_volume_recovery(self.hass.states.get(self.entity))
        self.assertFalse(self.manager.pending_remutes)
        self.assertFalse(self.manager.volume_waiters)
        self.assertEqual(len(self.calls), 3)

    async def test_delayed_volume_echo_cannot_skip_original_mute(self) -> None:
        state = self.hass.states.get(self.entity)
        self.hass.states.async_set(
            self.entity,
            "paused",
            {**state.attributes, "volume_level": 0.8, "is_volume_muted": True},
        )

        mute_sent = asyncio.Event()
        remembered_at_mute = []

        async def echo_later(call) -> None:
            if call.service == "volume_mute" or call.data["volume_level"] == 0.8:
                if call.service == "volume_mute":
                    remembered_at_mute.append(
                        self.hass.states.get(self.entity).attributes["volume_level"]
                    )
                    mute_sent.set()
                self.calls.append((call.service, dict(call.data)))
                return
            await self.media(call)

        self.hass.services.async_register("media_player", "volume_set", echo_later)
        self.hass.services.async_register("media_player", "volume_mute", echo_later)
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.15),
            patch.object(integration, "RECOVERY_RESERVE", 0.08),
        ):
            await self.manager.announce("tv")
        self.assertEqual(
            [name for name, _ in self.calls],
            ["volume_set", "send_command", "volume_set"],
        )
        deadline_task = self.manager.pending_remutes[self.entity][1]
        self.hass.states.async_set(
            self.entity,
            "paused",
            {**state.attributes, "volume_level": 0.8, "is_volume_muted": False},
        )
        await asyncio.wait_for(mute_sent.wait(), 0.5)
        services_settled = asyncio.Event()

        def after_ack(_task) -> None:
            if not self.manager.services.pending:
                services_settled.set()

        for pending in tuple(self.manager.services.pending):
            pending.add_done_callback(after_ack)
        if self.manager.services.pending:
            await asyncio.wait_for(services_settled.wait(), 0.5)
        self.assertEqual(remembered_at_mute, [0.8])
        for level, muted in ((0.4, False), (0.8, False), (0.4, True)):
            self.hass.states.async_set(
                self.entity,
                "paused",
                {**state.attributes, "volume_level": level, "is_volume_muted": muted},
            )
            with self.assertRaisesRegex(
                ProviderNotReadyError, "^yandex_volume_recovery_pending$"
            ):
                await self.manager.announce("tv")
        self.hass.states.async_set(
            self.entity,
            "paused",
            {**state.attributes, "volume_level": 0.8, "is_volume_muted": True},
        )
        await asyncio.wait_for(deadline_task, 0.5)
        self.manager.check_aisis_volume_recovery(self.hass.states.get(self.entity))
        self.assertNotIn(self.entity, self.manager.volume_recovery)
        self.assertEqual(len(self.calls), 4)

    async def test_unconfirmed_final_mute_keeps_next_label_off_stale_state(
        self,
    ) -> None:
        state = self.hass.states.get(self.entity)
        self.hass.states.async_set(
            self.entity, state.state, {**state.attributes, "is_volume_muted": True}
        )

        async def mute_ack_only(call) -> None:
            self.calls.append((call.service, dict(call.data)))

        self.hass.services.async_register("media_player", "volume_mute", mute_ack_only)
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.15),
            patch.object(integration, "RECOVERY_RESERVE", 0.08),
        ):
            await self.manager.announce("tv")
        with self.assertRaisesRegex(
            ProviderNotReadyError, "^yandex_volume_recovery_pending$"
        ):
            await self.manager.announce("tv")
        deadline_task = self.manager.pending_remutes[self.entity][1]
        self.hass.states.async_set(
            self.entity, "paused", {**state.attributes, "is_volume_muted": True}
        )
        await asyncio.wait_for(deadline_task, 0.5)
        self.manager.check_aisis_volume_recovery(self.hass.states.get(self.entity))
        self.assertNotIn(self.entity, self.manager.volume_recovery)

    async def check_unsent_remute(self, *, expired: bool) -> None:
        state = self.hass.states.get(self.entity)
        attrs = {**state.attributes, "volume_level": 0.8, "is_volume_muted": True}
        self.hass.states.async_set(self.entity, "paused", attrs)

        dispatch = self.manager.call

        async def change_at_dispatch(
            service: str, entity: str, **kwargs: object
        ) -> bool:
            if service == "volume_mute":
                if expired:
                    kwargs["deadline"] = 0
                else:
                    self.hass.states.async_set(entity, "unavailable", {})
            return await dispatch(service, entity, **kwargs)

        with (
            patch.object(self.manager, "call", change_at_dispatch),
            patch.object(integration, "REMUTE_GRACE", 0.05),
            patch.object(integration, "ATTEMPT_BUDGET", 0.15),
            patch.object(integration, "RECOVERY_RESERVE", 0.08),
        ):
            await self.manager.announce("tv")
            if pending := self.manager.pending_remutes.get(self.entity):
                await asyncio.gather(pending[1], return_exceptions=True)
        self.assertEqual(
            [n for n, _ in self.calls], ["volume_set", "send_command", "volume_set"]
        )
        self.hass.services.async_register("media_player", "volume_set", self.media)
        self.hass.states.async_set(
            self.entity,
            "paused",
            {**attrs, "volume_level": 0.4, "is_volume_muted": False},
        )
        with self.assertRaisesRegex(
            ProviderNotReadyError, "^yandex_volume_recovery_pending$"
        ):
            await self.manager.announce("tv")
        self.assertEqual(len(self.calls), 3)
        # Only the accepted volume command can echo. No mute was dispatched.
        self.hass.states.async_set(
            self.entity, "paused", {**attrs, "is_volume_muted": False}
        )
        await self.manager.announce("tv")
        self.assertEqual(
            [d["volume_level"] for n, d in self.calls if n == "volume_set"],
            [0.4, 0.8, 0.4, 0.8],
        )
        self.assertFalse(
            self.hass.states.get(self.entity).attributes["is_volume_muted"]
        )

    async def test_deadline_before_remute_does_not_wait_for_unsent_mute(self) -> None:
        await self.check_unsent_remute(expired=True)

    async def test_refused_remute_preserves_already_sent_volume_restore(self) -> None:
        await self.check_unsent_remute(expired=False)

    async def test_timeout_after_remute_dispatch_still_requires_mute_echo(self) -> None:
        state = self.hass.states.get(self.entity)
        attrs = {**state.attributes, "volume_level": 0.8, "is_volume_muted": True}
        self.hass.states.async_set(self.entity, "paused", attrs)

        async def lost_ack(call) -> None:
            self.calls.append((call.service, dict(call.data)))
            await asyncio.Event().wait()

        self.hass.services.async_register("media_player", "volume_mute", lost_ack)
        with patch.object(self.manager.services, "limit", 0.05):
            await self.manager.announce("tv")
        calls_after = list(self.calls)
        self.assertEqual(calls_after[-1][0], "volume_mute")
        with self.assertRaisesRegex(
            ProviderNotReadyError, "^yandex_volume_recovery_pending$"
        ):
            await self.manager.announce("tv")
        self.assertEqual(self.calls, calls_after)
        self.hass.states.async_set(self.entity, "paused", attrs)
        self.hass.services.async_register("media_player", "volume_mute", self.media)
        await self.manager.announce("tv")
        self.assertTrue(self.hass.states.get(self.entity).attributes["is_volume_muted"])

    async def test_titled_music_without_id_during_alice_is_active(self) -> None:
        self.hass.states.async_set(
            self.entity,
            "playing",
            {"alice_state": "SPEAKING", "media_title": "Fixture radio"},
        )
        self.assertEqual(self.manager.label_volume(0.8), 0.6)

    async def test_zero_volume_is_restored(self) -> None:
        state = self.hass.states.get(self.entity)
        self.hass.states.async_set(
            self.entity, state.state, {**state.attributes, "volume_level": 0.0}
        )
        await self.manager.announce("tv")
        self.assertEqual(self.calls[0][1]["volume_level"], 0.6)
        self.assertEqual(self.calls[-1][1]["volume_level"], 0.0)

    async def test_manual_volume_change_wins(self) -> None:
        self.manual_volume = 0.8
        await self.manager.announce("tv")
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.8
        )
        self.assertEqual([name for name, _ in self.calls].count("volume_set"), 1)

    async def test_failure_still_restores_volume(self) -> None:
        self.fail_speech = True
        with self.assertRaises(RuntimeError):
            await self.manager.announce("tv")
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.2
        )

    async def test_user_change_during_restore_is_not_remuted(self) -> None:
        state = self.hass.states.get(self.entity)
        self.hass.states.async_set(
            self.entity, state.state, {**state.attributes, "is_volume_muted": True}
        )
        self.change_during_restore = True
        await self.manager.announce("tv")
        state = self.hass.states.get(self.entity)
        self.assertEqual(state.attributes["volume_level"], 0.8)
        self.assertFalse(state.attributes["is_volume_muted"])

    async def test_user_volume_away_and_back_is_preserved(self) -> None:
        self.volume_sequence = [0.8, 0.6]
        await self.manager.announce("tv")
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.6
        )
        self.assertEqual([name for name, _ in self.calls].count("volume_set"), 1)

    async def test_pause_fallback_resumes_only_original_queue(self) -> None:
        self.manager.config["music_mode"] = "pause"
        await self.manager.announce("tv")
        self.assertIn(
            "media_play", [name for name, _ in self.calls], self.manager.public_status
        )
        self.assertEqual(self.hass.states.get(self.entity).state, "playing")
        self.calls.clear()
        self.replace_content = True
        await self.manager.announce("tv")
        self.assertNotIn("media_play", [name for name, _ in self.calls])

    async def test_pause_recovers_when_completion_signal_never_arrives(self) -> None:
        self.manager.config["music_mode"] = "pause"
        self.missing_speech_events = True
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.4),
            patch.object(integration, "RECOVERY_RESERVE", 0.2),
        ):
            await asyncio.wait_for(self.manager.announce("tv"), 0.8)
        self.assertIn(
            "media_play", [name for name, _ in self.calls], self.manager.public_status
        )
        self.assertEqual(self.hass.states.get(self.entity).state, "playing")
        self.assertEqual(self.manager.status, "sent_unconfirmed")

    async def test_native_missing_completion_uses_the_total_budget(self) -> None:
        self.missing_speech_events = True
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.4),
            patch.object(integration, "RECOVERY_RESERVE", 0.2),
        ):
            await asyncio.wait_for(self.manager.announce("tv"), 0.8)
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.2
        )
        self.assertEqual(self.manager.status, "sent_unconfirmed")
        self.assertLess(self.manager.public_status["controller_elapsed_ms"], 800)

    async def test_lost_pause_ack_cannot_delay_recovery_indefinitely(self) -> None:
        self.manager.config["music_mode"] = "pause"
        hold = self.hold_ack["media_pause"] = asyncio.Event()
        started = time.monotonic()
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.2),
            patch.object(integration, "RECOVERY_RESERVE", 0.08),
            patch.object(self.manager.services, "limit", 0.03),
            self.assertRaises(TimeoutError),
        ):
            await asyncio.wait_for(self.manager.announce("tv"), 0.3)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(self.hass.states.get(self.entity).state, "playing")
        hold.set()
        await asyncio.sleep(0)
        self.assertEqual(self.speech_started, [])

    async def test_lost_speech_ack_cannot_delay_volume_or_music_recovery(self) -> None:
        self.manager.config["music_mode"] = "pause"
        hold = self.hold_ack["send_command"] = asyncio.Event()
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.2),
            patch.object(integration, "RECOVERY_RESERVE", 0.08),
            patch.object(self.manager.services, "limit", 0.03),
            self.assertRaises(TimeoutError),
        ):
            await asyncio.wait_for(self.manager.announce("tv"), 0.3)
        state = self.hass.states.get(self.entity)
        self.assertEqual(
            (state.state, state.attributes["volume_level"]), ("playing", 0.2)
        )
        hold.set()
        await asyncio.sleep(0)
        self.assertEqual(len(self.speech_started), 1)

    async def test_lost_restore_ack_does_not_block_music_recovery(self) -> None:
        self.manager.config["music_mode"] = "pause"
        self.hold_restore_ack = asyncio.Event()
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.2),
            patch.object(integration, "RECOVERY_RESERVE", 0.08),
            patch.object(self.manager.services, "limit", 0.03),
        ):
            await asyncio.wait_for(self.manager.announce("tv"), 0.3)
        self.assertEqual(self.hass.states.get(self.entity).state, "playing")
        self.assertEqual(self.manager.status, "recovery_unconfirmed")
        self.assertFalse(self.hold_restore_ack.is_set())

    async def test_target_change_during_volume_rpc_suppresses_stale_speech(
        self,
    ) -> None:
        hold = self.hold_ack["volume_set"] = asyncio.Event()
        started = self.ack_started["volume_set"] = asyncio.Event()
        attempt = asyncio.create_task(self.manager.announce("tv"))
        await asyncio.wait_for(started.wait(), 0.2)
        self.hass.states.async_set(CONFIG["target_entity"], "kodi", ROUTE_ATTRIBUTES)
        hold.set()
        await attempt
        self.assertEqual(self.speech_started, [])
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.2
        )

    async def test_uncancellable_provider_is_tracked_and_close_stays_bounded(
        self,
    ) -> None:
        self.ignore_cancellation = True
        hold = self.hold_ack["send_command"] = asyncio.Event()
        try:
            with (
                patch.object(integration, "ATTEMPT_BUDGET", 0.2),
                patch.object(integration, "RECOVERY_RESERVE", 0.08),
                patch.object(self.manager.services, "limit", 0.03),
                self.assertRaises(TimeoutError),
            ):
                await asyncio.wait_for(self.manager.announce("tv"), 0.3)
            self.assertEqual(len(self.manager.services.pending), 1)
            self.hass.states.async_set(
                CONFIG["target_entity"], "kodi", ROUTE_ATTRIBUTES
            )
            with self.assertRaisesRegex(
                ProviderNotReadyError, "speaker_service_recovery_pending"
            ):
                await self.manager.announce("kodi")
            started = time.monotonic()
            await self.manager.close()
            self.assertLess(time.monotonic() - started, 0.15)
            self.assertEqual(len(self.manager.services.pending), 1)
        finally:
            hold.set()
            await asyncio.gather(*self.manager.services.pending, return_exceptions=True)
        self.assertEqual(len(self.speech_started), 1)
        self.assertEqual(self.manager.services.pending, set())

    async def test_target_away_and_back_invalidates_the_older_attempt(self) -> None:
        self.manager.gate.changed("tv")
        hold = self.hold_ack["volume_set"] = asyncio.Event()
        started = self.ack_started["volume_set"] = asyncio.Event()
        unsubscribe = integration.async_track_state_change_event(
            self.hass, [CONFIG["target_entity"]], self.manager.target_changed
        )
        with patch.object(self.manager.queue, "submit") as submit:
            attempt = asyncio.create_task(self.manager.announce("tv"))
            try:
                await asyncio.wait_for(started.wait(), 0.2)
                for target in ("kodi", "tv"):
                    self.hass.states.async_set(
                        CONFIG["target_entity"], target, {"initialized": True}
                    )
                    await asyncio.sleep(0)
            finally:
                hold.set()
                await attempt
                unsubscribe()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(self.speech_started, [])
        self.assertEqual(self.manager.status, "superseded")

    async def test_timed_out_speech_cannot_dispatch_after_a_newer_target(self) -> None:
        self.manager.config["music_mode"] = "pause"
        self.speech_gate = asyncio.Event()
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.2),
            patch.object(integration, "RECOVERY_RESERVE", 0.08),
            patch.object(self.manager.services, "limit", 0.03),
            self.assertRaises(TimeoutError),
        ):
            await asyncio.wait_for(self.manager.announce("tv"), 0.3)
        self.hass.states.async_set(CONFIG["target_entity"], "kodi", ROUTE_ATTRIBUTES)
        self.speech_gate.set()
        await asyncio.sleep(0)
        self.assertEqual(self.speech_started, [])
        await self.manager.announce("kodi")
        self.assertEqual(len(self.speech_started), 1)
        self.assertEqual(self.manager.public_status["target"], "kodi")
        self.assertEqual(self.hass.states.get(self.entity).state, "playing")

    async def test_pause_ack_timeout_still_recovers_original_music(self) -> None:
        self.manager.config["music_mode"] = "pause"
        self.fail_pause_ack = True
        with self.assertRaises(asyncio.TimeoutError):
            await self.manager.announce("tv")
        self.assertEqual(
            [name for name, _ in self.calls], ["media_pause", "media_play"]
        )
        self.assertEqual(self.hass.states.get(self.entity).state, "playing")

    async def test_missing_credentials_never_changes_speaker(self) -> None:
        self.manager.settings = {}
        with self.assertRaisesRegex(ProviderNotReadyError, "needs_dialogs_credentials"):
            await self.manager.announce("tv")
        self.assertEqual(self.calls, [])

    async def test_dedicated_selector_does_not_change_music_selector(self) -> None:
        self.hass.states.async_set("input_select.music_remote_speaker_backend", "AISIS")
        self.assertEqual(self.manager.speaker(), ("yandex", self.entity))
        self.hass.states.async_set(CONFIG["backend_entity"], "AISIS")
        with self.assertRaisesRegex(
            ProviderNotReadyError, "selected_aisis_speaker_unavailable"
        ):
            self.manager.speaker()
        self.assertEqual(
            self.hass.states.get("input_select.music_remote_speaker_backend").state,
            "AISIS",
        )

    async def test_backend_selection_restores_aisis_across_restart(self) -> None:
        entity = InputSelect.from_yaml(
            {
                **PACKAGE["input_select"]["remote_announcement_backend"],
                "id": "remote_announcement_backend",
            }
        )
        entity.hass = self.hass
        with patch.object(
            entity,
            "async_get_last_state",
            AsyncMock(return_value=State(CONFIG["backend_entity"], "AISIS")),
        ):
            await entity.async_added_to_hass()
        self.assertEqual(entity.current_option, "AISIS")

    async def track_target_lease(
        self, *, lease: float | None, initialized: bool = True
    ) -> None:
        self.hass.states.async_set(
            CONFIG["target_entity"],
            "chromecast",
            {"initialized": initialized, "lease_until": lease},
        )
        await self.hass.async_block_till_done()
        self.manager.gate.changed("chromecast")
        unsubscribe = integration.async_track_state_change_event(
            self.hass, [CONFIG["target_entity"]], self.manager.target_changed
        )
        self.addCleanup(unsubscribe)

    async def test_lease_expiry_announces_unchanged_effective_target_once(self) -> None:
        await self.track_target_lease(lease=30.0)
        generation = self.manager.generation
        with patch.object(self.manager.queue, "submit") as submit:
            self.hass.states.async_set(
                CONFIG["target_entity"],
                "chromecast",
                {"initialized": True, "lease_until": None},
            )
            await self.hass.async_block_till_done()
            self.hass.states.async_set(
                CONFIG["target_entity"],
                "chromecast",
                {"initialized": True, "lease_until": None, "pair": "kodi"},
            )
            await self.hass.async_block_till_done()
        submit.assert_called_once_with("chromecast")
        self.assertEqual(self.manager.generation, generation + 1)

    async def test_lease_renewals_do_not_announce_or_invalidate_attempt(self) -> None:
        await self.track_target_lease(lease=30.0)
        generation = self.manager.generation
        with patch.object(self.manager.queue, "submit") as submit:
            for lease in (60.0, 90.0):
                self.hass.states.async_set(
                    CONFIG["target_entity"],
                    "chromecast",
                    {"initialized": True, "lease_until": lease},
                )
                await self.hass.async_block_till_done()
        submit.assert_not_called()
        self.assertEqual(self.manager.generation, generation)

    async def test_audio_mode_ignores_input_lease_end_and_announces_exit(self) -> None:
        await self.track_target_lease(lease=30.0)
        generation = self.manager.generation
        # Hold the debounce worker while inspecting the actual pending entry.
        with (
            patch.object(self.manager.queue, "_run", AsyncMock()),
            patch.object(
                self.manager.queue, "submit", wraps=self.manager.queue.submit
            ) as submit,
        ):
            self.hass.states.async_set(
                CONFIG["target_entity"],
                "speaker",
                {"initialized": True, "lease_until": 30.0},
            )
            await self.hass.async_block_till_done()
            submit.assert_called_once_with("speaker")
            self.assertEqual(self.manager.generation, generation + 1)
            self.assertEqual(self.manager.queue.pending, "speaker")
            self.hass.states.async_set(
                CONFIG["target_entity"],
                "speaker",
                {"initialized": True, "lease_until": None},
            )
            await self.hass.async_block_till_done()
            submit.assert_called_once_with("speaker")
            self.assertEqual(self.manager.generation, generation + 1)
            self.assertEqual(self.manager.queue.pending, "speaker")
            self.hass.states.async_set(
                CONFIG["target_entity"],
                "kodi",
                {"initialized": True, "lease_until": None},
            )
            await self.hass.async_block_till_done()
        self.assertEqual(submit.call_count, 2)
        submit.assert_called_with("kodi")
        self.assertEqual(self.manager.queue.pending, "kodi")
        self.assertEqual(self.manager.generation, generation + 2)

    async def test_lease_startup_and_entity_creation_stay_quiet(self) -> None:
        await self.track_target_lease(lease=30.0, initialized=False)
        with patch.object(self.manager.queue, "submit") as submit:
            self.hass.states.async_set(
                CONFIG["target_entity"],
                "chromecast",
                {"initialized": True, "lease_until": None},
            )
            await self.hass.async_block_till_done()
            self.hass.states.async_remove(CONFIG["target_entity"])
            await self.hass.async_block_till_done()
            self.hass.states.async_set(
                CONFIG["target_entity"],
                "kodi",
                {"initialized": True, "lease_until": None},
            )
            await self.hass.async_block_till_done()
        submit.assert_not_called()
        self.assertEqual(self.manager.gate.last, "kodi")

    async def test_target_change_with_lease_expiry_queues_only_once(self) -> None:
        await self.track_target_lease(lease=30.0)
        generation = self.manager.generation
        with patch.object(self.manager.queue, "submit") as submit:
            self.hass.states.async_set(
                CONFIG["target_entity"],
                "kodi",
                {"initialized": True, "lease_until": None},
            )
            await self.hass.async_block_till_done()
        submit.assert_called_once_with("kodi")
        self.assertEqual(self.manager.generation, generation + 1)

    async def test_startup_controller_reconciliation_is_quiet(self) -> None:
        self.manager.gate.changed("chromecast")
        self.manager.target_changed(
            Event(
                EVENT_STATE_CHANGED,
                {
                    "old_state": State(
                        CONFIG["target_entity"], "chromecast", {"initialized": False}
                    ),
                    "new_state": State(
                        CONFIG["target_entity"], "kodi", ROUTE_ATTRIBUTES
                    ),
                },
            )
        )
        await self.manager.queue.drain()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.gate.last, "kodi")

    async def test_explicit_aisis_uses_prepared_marker_in_another_room(self) -> None:
        entity = CONFIG["aisis_entity"]
        self.hass.states.async_set(CONFIG["backend_entity"], "AISIS")
        self.hass.states.async_set(CONFIG["aisis_connected_entity"], "on")
        self.hass.states.async_set(entity, "idle", {"volume_level": 0.7})
        self.manager.settings["aisis"] = {
            "ready": True,
            "keys": [clip.key for clip in self.manager.cache.clips.values()],
        }
        spoken = []

        async def speak(call) -> None:
            spoken.append(dict(call.data))

        self.hass.services.async_register("aisis_speaker", "speak", speak)
        await self.manager.announce("tv")
        self.assertEqual(
            spoken,
            [{"duid": "M00ZRW3008RAJG", "text": self.manager.cache.get("tv").marker}],
        )
        self.assertEqual(
            [(name, data["volume_level"]) for name, data in self.calls],
            [("volume_set", 0.6), ("volume_set", 0.7)],
        )

    def prepare_aisis_native_volume(self, volume: float, *, muted: bool = False) -> str:
        """Publish studio playback while the living-room speaker is paused."""
        room_speaker = self.hass.states.get(self.entity)
        self.hass.states.async_set(self.entity, "paused", room_speaker.attributes)
        entity = CONFIG["aisis_entity"]
        self.hass.states.async_set(CONFIG["backend_entity"], "AISIS")
        self.hass.states.async_set(CONFIG["aisis_connected_entity"], "on")
        attributes = {
            "volume_level": volume,
            "media_content_id": "aisis-original-track",
        }
        if muted:
            attributes["is_volume_muted"] = True
        self.hass.states.async_set(entity, "playing", attributes)
        self.manager.settings["aisis"] = {
            "ready": True,
            "keys": [clip.key for clip in self.manager.cache.clips.values()],
        }
        self.aisis_native_levels = []
        self.aisis_restore_ack_only = False
        self.aisis_restore_requested = asyncio.Event()

        async def volume_set(call) -> None:
            self.calls.append((call.service, dict(call.data)))
            self.assertEqual(call.data["entity_id"], entity)
            # Literal supported native levels, not an acknowledgement-only fake.
            level = {0.0: 0, 0.1: 1, 0.4: 4, 0.8: 8}[call.data["volume_level"]]
            self.aisis_native_levels.append(level)
            if level == 0 and self.aisis_restore_ack_only:
                self.aisis_restore_requested.set()
                return
            state = self.hass.states.get(entity)
            self.hass.states.async_set(
                entity, state.state, {**state.attributes, "volume_level": level / 10}
            )

        async def speak(call) -> None:
            self.calls.append(("speak", dict(call.data)))
            if self.manual_volume is not None:
                state = self.hass.states.get(entity)
                self.hass.states.async_set(
                    entity,
                    state.state,
                    {**state.attributes, "volume_level": self.manual_volume},
                )
                await asyncio.sleep(0)

        self.hass.services.async_register("media_player", "volume_set", volume_set)
        self.hass.services.async_register("aisis_speaker", "speak", speak)
        return entity

    async def test_aisis_zero_and_low_native_volume_are_restored(self) -> None:
        for original, native_level in ((0.0, 0), (0.1, 1)):
            with self.subTest(original=original):
                self.calls.clear()
                entity = self.prepare_aisis_native_volume(original)
                await self.manager.announce("tv")
                self.assertEqual(self.aisis_native_levels, [4, native_level])
                self.assertEqual(
                    [name for name, _ in self.calls],
                    ["volume_set", "speak", "volume_set"],
                )
                self.assertEqual(
                    self.calls[1][1],
                    {
                        "duid": "M00ZRW3008RAJG",
                        "text": self.manager.cache.get("tv").marker,
                    },
                )
                state = self.hass.states.get(entity)
                self.assertEqual(state.attributes["volume_level"], original)
                self.assertEqual(
                    state.attributes["media_content_id"], "aisis-original-track"
                )
                self.assertNotIn("is_volume_muted", state.attributes)
                self.assertEqual(state.state, "playing")
                self.assertEqual(
                    self.hass.states.get(CONFIG["backend_entity"]).state, "AISIS"
                )
                self.assertEqual(self.manager.status, "sent_unconfirmed")

    async def test_aisis_restore_ack_without_readback_is_unconfirmed(self) -> None:
        entity = self.prepare_aisis_native_volume(0.0)
        self.aisis_restore_ack_only = True
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.4),
            patch.object(integration, "RECOVERY_RESERVE", 0.2),
        ):
            await asyncio.wait_for(self.manager.announce("tv"), 0.8)
        self.assertEqual(self.aisis_native_levels, [4, 0])
        self.assertEqual(
            self.hass.states.get(entity).attributes["volume_level"],
            0.4,
        )
        self.assertEqual(self.manager.status, "recovery_unconfirmed")
        self.assertEqual(
            self.manager.public_status["reason"], "aisis_volume_recovery_unconfirmed"
        )
        self.assertEqual(self.manager.public_status["pending_service_count"], 0)
        self.assertLess(self.manager.public_status["controller_elapsed_ms"], 800)
        calls_after_restore = self.calls.copy()
        for attributes in ({}, {"media_position": 17}):
            with self.subTest(attributes=attributes):
                state = self.hass.states.get(entity)
                self.hass.states.async_set(
                    entity, state.state, {**state.attributes, **attributes}
                )
                with self.assertRaisesRegex(
                    ProviderNotReadyError, "^aisis_volume_recovery_pending$"
                ):
                    await self.manager.announce("tv")
                self.assertEqual(self.calls, calls_after_restore)
                self.assertEqual(self.aisis_native_levels, [4, 0])

        state = self.hass.states.get(entity)
        self.hass.states.async_set(
            entity, state.state, {**state.attributes, "volume_level": 0.0}
        )
        self.aisis_restore_ack_only = False
        await self.manager.announce("tv")
        self.assertEqual(self.aisis_native_levels, [4, 0, 4, 0])
        self.assertEqual(
            [name for name, _ in self.calls],
            ["volume_set", "speak", "volume_set"] * 2,
        )
        self.assertEqual(self.hass.states.get(entity).attributes["volume_level"], 0.0)

    async def test_aisis_human_volume_after_unconfirmed_restore_is_preserved(
        self,
    ) -> None:
        entity = self.prepare_aisis_native_volume(0.0)
        self.aisis_restore_ack_only = True
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.4),
            patch.object(integration, "RECOVERY_RESERVE", 0.2),
        ):
            await asyncio.wait_for(self.manager.announce("tv"), 0.8)
        state = self.hass.states.get(entity)
        self.hass.states.async_set(
            entity, state.state, {**state.attributes, "volume_level": 0.8}
        )
        await self.manager.announce("tv")
        self.assertEqual(self.aisis_native_levels, [4, 0, 4, 8])
        self.assertEqual(
            [name for name, _ in self.calls],
            ["volume_set", "speak", "volume_set"] * 2,
        )
        self.assertEqual(self.hass.states.get(entity).attributes["volume_level"], 0.8)

    async def test_aisis_human_mute_after_unconfirmed_restore_is_preserved(
        self,
    ) -> None:
        entity = self.prepare_aisis_native_volume(0.0)
        self.aisis_restore_ack_only = True
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.4),
            patch.object(integration, "RECOVERY_RESERVE", 0.2),
        ):
            await asyncio.wait_for(self.manager.announce("tv"), 0.8)
        state = self.hass.states.get(entity)
        self.hass.states.async_set(
            entity, state.state, {**state.attributes, "is_volume_muted": True}
        )
        calls_after_restore = self.calls.copy()
        with self.assertRaisesRegex(
            ProviderNotReadyError, "^aisis_cannot_restore_muted_or_invalid_volume$"
        ):
            await self.manager.announce("tv")
        self.assertEqual(self.calls, calls_after_restore)
        self.assertEqual(self.aisis_native_levels, [4, 0])
        self.assertIs(
            self.hass.states.get(entity).attributes["is_volume_muted"], expr2=True
        )

        state = self.hass.states.get(entity)
        self.hass.states.async_set(
            entity, state.state, {**state.attributes, "is_volume_muted": False}
        )
        await self.manager.announce("tv")
        self.assertEqual(self.aisis_native_levels, [4, 0])
        self.assertEqual(
            [name for name, _ in self.calls],
            ["volume_set", "speak", "volume_set", "speak"],
        )

    async def test_aisis_restore_waits_for_delayed_native_readback(self) -> None:
        entity = self.prepare_aisis_native_volume(0.0)
        self.aisis_restore_ack_only = True

        async def publish_readback() -> None:
            await self.aisis_restore_requested.wait()
            await asyncio.sleep(0.05)
            state = self.hass.states.get(entity)
            self.hass.states.async_set(
                entity, state.state, {**state.attributes, "volume_level": 0.0}
            )

        readback = asyncio.create_task(publish_readback())
        try:
            await asyncio.wait_for(self.manager.announce("tv"), 0.8)
            volume_at_return = self.hass.states.get(entity).attributes["volume_level"]
            self.assertEqual(volume_at_return, 0.0)
            self.assertEqual(self.manager.status, "sent_unconfirmed")
            self.assertEqual(self.aisis_native_levels, [4, 0])
        finally:
            readback.cancel()
            await asyncio.gather(readback, return_exceptions=True)

    async def test_aisis_reported_mute_remains_unsupported(self) -> None:
        self.prepare_aisis_native_volume(0.0, muted=True)
        with self.assertRaisesRegex(
            ProviderNotReadyError, "^aisis_cannot_restore_muted_or_invalid_volume$"
        ):
            await self.manager.announce("tv")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.aisis_native_levels, [])

    async def test_aisis_negative_volume_remains_rejected(self) -> None:
        self.prepare_aisis_native_volume(-0.1)
        with self.assertRaisesRegex(
            ProviderNotReadyError, "^aisis_cannot_restore_muted_or_invalid_volume$"
        ):
            await self.manager.announce("tv")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.aisis_native_levels, [])

    async def test_aisis_human_volume_change_prevents_zero_restore(self) -> None:
        entity = self.prepare_aisis_native_volume(0.0)
        self.manual_volume = 0.8
        await self.manager.announce("tv")
        self.assertEqual(self.aisis_native_levels, [4])
        self.assertEqual([name for name, _ in self.calls], ["volume_set", "speak"])
        state = self.hass.states.get(entity)
        self.assertEqual(state.attributes["volume_level"], 0.8)
        self.assertEqual(state.attributes["media_content_id"], "aisis-original-track")
        self.assertEqual(state.state, "playing")

    async def test_real_ha_component_and_sensor_registration_with_prefilled_cache(
        self,
    ) -> None:
        await asyncio.to_thread(
            Path(self.temp.name, "custom_components").symlink_to,
            ROOT / "custom_components",
            target_is_directory=True,
        )
        self.hass.auth = await auth.auth_manager_from_config(self.hass, [], [])
        # Interface discovery is an OS boundary unavailable in this isolated runner.
        with (
            patch(
                "homeassistant.components.network.async_get_source_ip",
                AsyncMock(return_value="127.0.0.1"),
            ),
            patch(
                "homeassistant.components.network.async_get_adapters",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                integration,
                "async_get_media_source_audio",
                AsyncMock(side_effect=AssertionError("cache must prevent synthesis")),
            ),
        ):
            self.assertTrue(
                await async_setup_component(self.hass, integration.DOMAIN, PACKAGE)
            )
            self.assertTrue(
                await async_setup_component(self.hass, "input_select", PACKAGE)
            )
            await self.hass.async_block_till_done()
            manager = self.hass.data[integration.DOMAIN]

            def raise_error(error) -> None:
                raise error

            with patch.object(manager, "failed", raise_error):
                await manager.prepare()
            await self.hass.async_block_till_done()
        self.assertTrue(self.hass.services.has_service(integration.DOMAIN, "prepare"))
        sensor = self.hass.states.get("sensor.remote_announcements")
        self.assertIsNotNone(sensor)
        self.assertEqual(
            sensor.state, "needs_dialogs_credentials", str(sensor.attributes)
        )
        self.assertEqual(sensor.attributes["clips_ready"], 5)
        self.assertEqual(self.hass.states.get(CONFIG["backend_entity"]).state, "Яндекс")


if __name__ == "__main__":
    unittest.main()
