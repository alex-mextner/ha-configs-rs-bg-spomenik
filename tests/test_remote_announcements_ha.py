"""Bounded HA runtime tests; services and cloud synthesis are external boundaries."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import yaml
from homeassistant import auth, loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant, State, Event, EVENT_STATE_CHANGED
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame, restore_state
from homeassistant.setup import async_setup_component

from custom_components import remote_announcements as integration
from custom_components.remote_announcements.core import ClipCache, VOICE_ID
from custom_components.remote_announcements.providers import ProviderNotReady

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = yaml.safe_load((ROOT / "packages/remote_announcements.yaml").read_text())
CONFIG = integration.CONFIG_SCHEMA(PACKAGE)[integration.DOMAIN]


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
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
        self.replace_content = False
        self.change_during_restore = False
        self.volume_sequence = []
        self.missing_speech_events = False
        self.entity = CONFIG["yandex_entity"]
        self.hass.states.async_set(CONFIG["target_entity"], "tv", {"initialized": True})
        self.hass.states.async_set(CONFIG["backend_entity"], "Яндекс")
        registry = er.async_get(self.hass)
        entry = registry.async_get_or_create("media_player", "yandex_station", "fixture-mini",
            suggested_object_id=self.entity.split(".", 1)[1])
        registry.async_update_entity(entry.entity_id, area_id=CONFIG["area"])
        self.hass.states.async_set(self.entity, "playing", {
            "volume_level": 0.2, "is_volume_muted": False,
            "media_content_id": "original-track", "alice_state": "IDLE"})

        async def generate(text):
            return b"fixture-prepared-audio", 0.01
        await self.manager.cache.prepare(generate)
        self.manager.settings = {"dialogs": {"skill_id": "11111111-1111-1111-1111-111111111111",
            "sounds": {clip.key: "22222222-2222-2222-2222-222222222222"
                for clip in self.manager.cache.clips.values()}}}

        for service in ("volume_set", "volume_mute", "media_pause", "media_play", "play_media"):
            self.hass.services.async_register("media_player", service, self.media)

    def apply_volume(self, attrs, volume):
        attrs["volume_level"] = volume
        attrs["is_volume_muted"] = volume == 0
        if self.change_during_restore and volume == 0.2:
            attrs["volume_level"] = 0.8
            attrs["is_volume_muted"] = False

    async def simulate_speech(self, entity, value, attrs):
        if self.fail_speech:
            raise RuntimeError("simulated transport failure")
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

    async def media(self, call):
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
        elif call.service == "media_play":
            value = "playing"
        elif call.service == "play_media":
            if not await self.simulate_speech(entity, value, attrs):
                return
        self.hass.states.async_set(entity, value, attrs)

    async def asyncTearDown(self):
        await self.manager.queue.stop()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def test_local_native_clip_preserves_queue_and_restores_volume(self):
        await self.manager.announce("tv")
        self.assertEqual([name for name, data in self.calls], ["volume_set", "play_media", "volume_set"])
        data = self.calls[1][1]
        self.assertEqual(data["media_content_type"], "text")
        self.assertIn("dialogs-upload/", data["media_content_id"])
        state = self.hass.states.get(self.entity)
        self.assertEqual(state.attributes["media_content_id"], "original-track")
        self.assertEqual(state.attributes["volume_level"], 0.2)
        self.assertEqual(self.manager.status, "ready")

    async def test_muted_speaker_gets_floor_then_original_level_and_mute(self):
        state = self.hass.states.get(self.entity)
        self.hass.states.async_set(self.entity, state.state,
            {**state.attributes, "is_volume_muted": True})
        await self.manager.announce("tv")
        self.assertEqual(self.calls[0], ("volume_set", {"entity_id": self.entity, "volume_level": 0.5}))
        self.assertEqual(self.calls[-1][0], "volume_mute")
        state = self.hass.states.get(self.entity)
        self.assertTrue(state.attributes["is_volume_muted"])
        self.assertEqual(state.attributes["volume_level"], 0.2)

    async def test_zero_volume_is_restored(self):
        state = self.hass.states.get(self.entity)
        self.hass.states.async_set(self.entity, state.state, {**state.attributes, "volume_level": 0.0})
        await self.manager.announce("tv")
        self.assertEqual(self.calls[0][1]["volume_level"], 0.5)
        self.assertEqual(self.calls[-1][1]["volume_level"], 0.0)

    async def test_manual_volume_change_wins(self):
        self.manual_volume = 0.8
        await self.manager.announce("tv")
        self.assertEqual(self.hass.states.get(self.entity).attributes["volume_level"], 0.8)
        self.assertEqual([name for name, _ in self.calls].count("volume_set"), 1)

    async def test_failure_still_restores_volume(self):
        self.fail_speech = True
        with self.assertRaises(RuntimeError):
            await self.manager.announce("tv")
        self.assertEqual(self.hass.states.get(self.entity).attributes["volume_level"], 0.2)

    async def test_user_change_during_restore_is_not_remuted(self):
        state = self.hass.states.get(self.entity)
        self.hass.states.async_set(self.entity, state.state,
            {**state.attributes, "is_volume_muted": True})
        self.change_during_restore = True
        await self.manager.announce("tv")
        state = self.hass.states.get(self.entity)
        self.assertEqual(state.attributes["volume_level"], 0.8)
        self.assertFalse(state.attributes["is_volume_muted"])

    async def test_user_volume_away_and_back_is_preserved(self):
        self.volume_sequence = [0.8, 0.5]
        await self.manager.announce("tv")
        self.assertEqual(self.hass.states.get(self.entity).attributes["volume_level"], 0.5)
        self.assertEqual([name for name, _ in self.calls].count("volume_set"), 1)

    async def test_pause_fallback_resumes_only_original_queue(self):
        self.manager.config["music_mode"] = "pause"
        await self.manager.announce("tv")
        self.assertEqual(self.calls[-1][0], "media_play")
        self.calls.clear()
        self.replace_content = True
        await self.manager.announce("tv")
        self.assertNotIn("media_play", [name for name, _ in self.calls])

    async def test_pause_recovers_when_completion_signal_never_arrives(self):
        self.manager.config["music_mode"] = "pause"
        self.missing_speech_events = True
        with patch.object(integration, "PAUSE_COMPLETION_BUDGET", 0.03):
            await asyncio.wait_for(self.manager.announce("tv"), 0.5)
        self.assertEqual(self.calls[-1][0], "media_play")
        self.assertEqual(self.manager.status, "sent_unconfirmed")

    async def test_missing_credentials_never_changes_speaker(self):
        self.manager.settings = {}
        with self.assertRaisesRegex(ProviderNotReady, "needs_dialogs_credentials"):
            await self.manager.announce("tv")
        self.assertEqual(self.calls, [])

    async def test_dedicated_selector_does_not_change_music_selector(self):
        self.hass.states.async_set("input_select.music_remote_speaker_backend", "AISIS")
        self.assertEqual(self.manager.speaker(), ("yandex", self.entity))
        self.hass.states.async_set(CONFIG["backend_entity"], "AISIS")
        with self.assertRaisesRegex(ProviderNotReady, "selected_aisis_speaker_unavailable"):
            self.manager.speaker()
        self.assertEqual(self.hass.states.get("input_select.music_remote_speaker_backend").state, "AISIS")

    async def test_backend_selection_restores_aisis_across_restart(self):
        from homeassistant.components.input_select import InputSelect
        entity = InputSelect.from_yaml({
            **PACKAGE["input_select"]["remote_announcement_backend"],
            "id": "remote_announcement_backend"})
        entity.hass = self.hass
        with patch.object(entity, "async_get_last_state",
                AsyncMock(return_value=State(CONFIG["backend_entity"], "AISIS"))):
            await entity.async_added_to_hass()
        self.assertEqual(entity.current_option, "AISIS")

    async def test_startup_controller_reconciliation_is_quiet(self):
        self.manager.gate.changed("chromecast")
        self.manager.target_changed(Event(EVENT_STATE_CHANGED, {
            "old_state": State(CONFIG["target_entity"], "chromecast", {"initialized": False}),
            "new_state": State(CONFIG["target_entity"], "kodi", {"initialized": True})}))
        await self.manager.queue.drain()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.gate.last, "kodi")

    async def test_explicit_aisis_uses_prepared_marker_in_another_room(self):
        entity = CONFIG["aisis_entity"]
        self.hass.states.async_set(CONFIG["backend_entity"], "AISIS")
        self.hass.states.async_set(CONFIG["aisis_connected_entity"], "on")
        self.hass.states.async_set(entity, "idle", {"volume_level": 0.7})
        self.manager.settings["aisis"] = {"ready": True,
            "keys": [clip.key for clip in self.manager.cache.clips.values()]}
        spoken = []
        async def speak(call):
            spoken.append(dict(call.data))
        self.hass.services.async_register("aisis_speaker", "speak", speak)
        await self.manager.announce("tv")
        self.assertEqual(spoken, [{"duid": "M00ZRW3008RAJG",
            "text": self.manager.cache.get("tv").marker}])
        self.assertEqual(self.calls, [])

    async def test_aisis_zero_volume_reports_exact_round_trip_limitation(self):
        entity = CONFIG["aisis_entity"]
        self.hass.states.async_set(CONFIG["backend_entity"], "AISIS")
        self.hass.states.async_set(CONFIG["aisis_connected_entity"], "on")
        self.hass.states.async_set(entity, "idle", {"volume_level": 0.0})
        self.manager.settings["aisis"] = {"ready": True,
            "keys": [clip.key for clip in self.manager.cache.clips.values()]}
        with self.assertRaisesRegex(ProviderNotReady, "aisis_cannot_restore_muted_or_zero_volume"):
            await self.manager.announce("tv")
        self.assertEqual(self.calls, [])

    async def test_real_ha_component_and_sensor_registration_with_prefilled_cache(self):
        Path(self.temp.name, "custom_components").symlink_to(ROOT / "custom_components", target_is_directory=True)
        self.hass.auth = await auth.auth_manager_from_config(self.hass, [], [])
        # Interface discovery is an OS boundary unavailable in this isolated runner.
        with patch("homeassistant.components.network.async_get_source_ip", AsyncMock(return_value="127.0.0.1")), \
                patch("homeassistant.components.network.async_get_adapters", AsyncMock(return_value=[])), \
                patch.object(integration, "async_get_media_source_audio", AsyncMock(side_effect=AssertionError("cache must prevent synthesis"))):
            self.assertTrue(await async_setup_component(self.hass, integration.DOMAIN, PACKAGE))
            self.assertTrue(await async_setup_component(self.hass, "input_select", PACKAGE))
            await self.hass.async_block_till_done()
            manager = self.hass.data[integration.DOMAIN]
            def raise_error(error):
                raise error
            with patch.object(manager, "failed", raise_error):
                await manager.prepare()
            await self.hass.async_block_till_done()
        self.assertTrue(self.hass.services.has_service(integration.DOMAIN, "prepare"))
        sensor = self.hass.states.get("sensor.remote_announcements")
        self.assertIsNotNone(sensor)
        self.assertEqual(sensor.state, "needs_dialogs_credentials", str(sensor.attributes))
        self.assertEqual(sensor.attributes["clips_ready"], 5)
        self.assertEqual(self.hass.states.get(CONFIG["backend_entity"]).state, "Яндекс")


if __name__ == "__main__":
    unittest.main()
