"""Regression checks for the plant speaker migration and options."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError

ROOT = Path(__file__).resolve().parents[2]
path = ROOT / "custom_components/plant_care_alerts/voice.py"
spec = importlib.util.spec_from_file_location("plant_voice", path)
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)


class TestVoice(unittest.IsolatedAsyncioTestCase):
    """Verify supported speaker selection and delivery behavior."""

    def state(self, entity_id, state) -> SimpleNamespace:
        return SimpleNamespace(
            entity_id=entity_id,
            state=state,
            attributes={
                "supported_features": 512 if "yandex_station" in entity_id else 0
            },
        )

    async def test_aisis_notify_unknown_is_available_and_uses_notify(self) -> None:
        target = "notify.aisis_speaker_local_notify"
        hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda _: self.state(target, "unknown")),
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        await voice.async_announce(hass, target, "Проверка")
        hass.services.async_call.assert_awaited_once_with(
            "notify",
            "send_message",
            {"entity_id": target, "message": "Проверка"},
            blocking=True,
            context=None,
        )

    async def test_yandex_station_uses_text_media(self) -> None:
        target = "media_player.yandex_station_livingroom"
        hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda _: self.state(target, "idle")),
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        await voice.async_announce(hass, target, "Полив")
        hass.services.async_call.assert_awaited_once_with(
            "media_player",
            "play_media",
            {
                "entity_id": target,
                "media_content_type": "text",
                "media_content_id": "Полив",
            },
            blocking=True,
            context=None,
        )

    async def test_both_transports_preserve_the_authenticated_caller(self) -> None:
        context = Context(user_id="speaker-observer")
        for target in (
            "notify.aisis_speaker_local_notify",
            "media_player.yandex_station_livingroom",
        ):
            with self.subTest(target=target):
                hass = SimpleNamespace(
                    states=SimpleNamespace(
                        get=lambda _, target=target: self.state(target, "idle")
                    ),
                    services=SimpleNamespace(async_call=AsyncMock()),
                )
                await voice.async_announce(hass, target, "Полив", context=context)
                self.assertIs(
                    hass.services.async_call.call_args.kwargs["context"], context
                )

    async def test_unavailable_target_cannot_send(self) -> None:
        target = "notify.speaker"
        hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda _: self.state(target, "unavailable")),
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        with self.assertRaises(HomeAssistantError):
            await voice.async_announce(hass, target, "Полив")
        hass.services.async_call.assert_not_called()

    async def test_aisis_media_player_rejected_no_play_media_support(self) -> None:
        target = "media_player.aisis_speaker_local"
        hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda _: self.state(target, "idle")),
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        with self.assertRaises(HomeAssistantError):
            await voice.async_announce(hass, target, "Полив")
        hass.services.async_call.assert_not_called()

    async def test_empty_message_does_not_speak(self) -> None:
        hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda _: None),
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        await voice.async_announce(hass, "notify.speaker", "  ")
        hass.services.async_call.assert_not_called()

    def test_missing_unknown_player_and_unavailable_notify_not_available(self) -> None:
        self.assertFalse(voice.target_available(None))
        self.assertFalse(
            voice.target_available(
                self.state("media_player.yandex_station_x", "unknown")
            )
        )
        self.assertFalse(voice.target_available(self.state("notify.x", "unavailable")))

    def test_saved_options_override_initial_data(self) -> None:
        entry = SimpleNamespace(
            data={"voice_target": "notify.initial"},
            options={"voice_target": "notify.selected"},
        )
        self.assertEqual(
            voice.configured_target(entry, "notify.fallback"), "notify.selected"
        )

    def test_no_entry_preserves_yaml_fallback(self) -> None:
        self.assertEqual(
            voice.configured_target(None, "notify.fallback"), "notify.fallback"
        )


if __name__ == "__main__":
    unittest.main()
