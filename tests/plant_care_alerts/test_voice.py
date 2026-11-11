"""Regression checks for the plant speaker migration and options."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[2]
path = ROOT / "custom_components/plant_care_alerts/voice.py"
spec = importlib.util.spec_from_file_location("plant_voice", path)
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)


class TestVoice(unittest.IsolatedAsyncioTestCase):
    def state(self, entity_id, state):
        return SimpleNamespace(entity_id=entity_id, state=state, attributes={"supported_features": 512 if "yandex_station" in entity_id else 0})

    async def test_aisis_notify_unknown_is_available_and_uses_notify(self):
        target = "notify.aisis_speaker_local_notify"
        hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda _: self.state(target, "unknown")),
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        await voice.async_announce(hass, target, "Проверка")
        hass.services.async_call.assert_awaited_once_with(
            "notify", "send_message",
            {"entity_id": target, "message": "Проверка"}, blocking=True,
        )

    async def test_yandex_station_uses_text_media(self):
        target = "media_player.yandex_station_livingroom"
        hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda _: self.state(target, "idle")),
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        await voice.async_announce(hass, target, "Полив")
        hass.services.async_call.assert_awaited_once_with(
            "media_player", "play_media",
            {"entity_id": target, "media_content_type": "text", "media_content_id": "Полив"},
            blocking=True,
        )

    async def test_unavailable_target_cannot_send(self):
        target = "notify.speaker"
        hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda _: self.state(target, "unavailable")),
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        with self.assertRaises(Exception):
            await voice.async_announce(hass, target, "Полив")
        hass.services.async_call.assert_not_called()

    async def test_aisis_media_player_rejected_no_play_media_support(self):
        target = "media_player.aisis_speaker_local"
        hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda _: self.state(target, "idle")),
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        with self.assertRaises(Exception):
            await voice.async_announce(hass, target, "Полив")
        hass.services.async_call.assert_not_called()

    async def test_empty_message_does_not_speak(self):
        hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: None),
                               services=SimpleNamespace(async_call=AsyncMock()))
        await voice.async_announce(hass, "notify.speaker", "  ")
        hass.services.async_call.assert_not_called()

    def test_missing_unknown_player_and_unavailable_notify_not_available(self):
        self.assertFalse(voice.target_available(None))
        self.assertFalse(voice.target_available(self.state("media_player.yandex_station_x", "unknown")))
        self.assertFalse(voice.target_available(self.state("notify.x", "unavailable")))

    def test_saved_options_override_initial_data(self):
        entry = SimpleNamespace(data={"voice_target": "notify.initial"},
                                options={"voice_target": "notify.selected"})
        self.assertEqual(voice.configured_target(entry, "notify.fallback"), "notify.selected")

    def test_no_entry_preserves_yaml_fallback(self):
        self.assertEqual(voice.configured_target(None, "notify.fallback"), "notify.fallback")


if __name__ == "__main__":
    unittest.main()
