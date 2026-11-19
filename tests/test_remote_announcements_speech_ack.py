"""Exercise native speech acknowledgements against the real HA service registry."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from typing import Any
from unittest.mock import patch

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame

from custom_components import remote_announcements as integration
from custom_components.remote_announcements.providers import ProviderNotReadyError
from custom_components.remote_announcements.service_calls import ServiceTimeoutError

ENTITY = "media_player.fixture_mini"
TARGET = "sensor.fixture_remote_target"
SELECTOR = "input_select.fixture_announcement_backend"
SKILL = "11111111-1111-1111-1111-111111111111"
SOUND = "22222222-2222-2222-2222-222222222222"


class NativeSpeechAckTests(unittest.IsolatedAsyncioTestCase):
    """Keep the HA call path real and replace only the physical speaker boundary."""

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
        config = integration.CONFIG_SCHEMA(
            {
                integration.DOMAIN: {
                    "target_entity": TARGET,
                    "backend_entity": SELECTOR,
                    "yandex_entity": ENTITY,
                    "aisis_entity": "media_player.fixture_aisis",
                    "aisis_connected_entity": "binary_sensor.fixture_aisis_connected",
                    "area": "fixture_room",
                }
            }
        )[integration.DOMAIN]
        self.manager = integration.Manager(self.hass, config)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.response: dict[str, Any] = {"status": "SUCCESS"}
        self.delay = 0.0
        self.signal_completion = True
        self.ignore_cancellation = False
        self.cancelled = asyncio.Event()
        self.hold: asyncio.Event | None = None
        self.response_requested: list[bool] = []
        self.hass.states.async_set(TARGET, "kodi", {"initialized": True})
        self.hass.states.async_set(SELECTOR, "Яндекс")
        registry = er.async_get(self.hass)
        entity = registry.async_get_or_create(
            "media_player",
            "yandex_station",
            "fixture-mini",
            suggested_object_id="fixture_mini",
        )
        registry.async_update_entity(entity.entity_id, area_id="fixture_room")
        self.hass.states.async_set(
            ENTITY,
            "playing",
            {
                "volume_level": 0.2,
                "is_volume_muted": False,
                "media_content_id": "original-track",
                "alice_state": "IDLE",
            },
        )

        async def generate(_text: str) -> tuple[bytes, float]:
            return b"prepared-fixture-clip", 0.02

        await self.manager.cache.prepare(generate)
        self.manager.settings = {
            "dialogs": {
                "skill_id": SKILL,
                "sounds": {
                    clip.key: SOUND for clip in self.manager.cache.clips.values()
                },
            }
        }
        for name in (
            "volume_set",
            "volume_mute",
            "media_pause",
            "media_play",
            "play_media",
        ):
            self.hass.services.async_register("media_player", name, self.media)
        self.hass.services.async_register(
            "yandex_station",
            "send_command",
            self.native,
            supports_response=SupportsResponse.OPTIONAL,
        )

    async def asyncTearDown(self) -> None:
        if self.hold is not None:
            self.hold.set()
        await asyncio.gather(*self.manager.services.pending, return_exceptions=True)
        await self.manager.close()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def response_from_speaker(self) -> dict[str, Any]:
        """Simulate acknowledgement and device events, with explicit cancellation."""
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.hold is not None:
                while not self.hold.is_set():
                    try:
                        await self.hold.wait()
                    except asyncio.CancelledError:
                        self.cancelled.set()
                        if not self.ignore_cancellation:
                            raise
            if self.signal_completion and self.response == {"status": "SUCCESS"}:
                state = self.hass.states.get(ENTITY)
                self.hass.states.async_set(
                    ENTITY, state.state, {**state.attributes, "alice_state": "SPEAKING"}
                )
                await asyncio.sleep(0)
                self.hass.states.async_set(
                    ENTITY, state.state, {**state.attributes, "alice_state": "IDLE"}
                )
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        else:
            return self.response

    async def native(self, call: ServiceCall) -> dict[str, Any]:
        """Return the exact native result through HA's optional-response contract."""
        self.calls.append((call.domain, call.service, dict(call.data)))
        self.response_requested.append(call.return_response)
        return await self.response_from_speaker()

    async def media(self, call: ServiceCall) -> dict[str, Any] | None:
        """Apply real fixture states for controls and expose the legacy speech path."""
        self.calls.append((call.domain, call.service, dict(call.data)))
        if call.service == "play_media":
            return await self.response_from_speaker()
        state = self.hass.states.get(ENTITY)
        attrs = dict(state.attributes)
        value = state.state
        if call.service == "volume_set":
            attrs["volume_level"] = call.data["volume_level"]
        elif call.service == "volume_mute":
            attrs["is_volume_muted"] = call.data["is_volume_muted"]
        elif call.service == "media_pause":
            value = "paused"
        elif call.service == "media_play":
            value = "playing"
        self.hass.states.async_set(ENTITY, value, attrs)
        return None

    async def test_half_second_native_success_uses_remaining_work_deadline(
        self,
    ) -> None:
        """A valid acknowledgement beyond 400 ms must not be cancelled early."""
        self.delay = 0.5
        start = time.monotonic()
        error = None
        try:
            await self.manager.announce("kodi")
        except ServiceTimeoutError as exc:
            error = exc
        elapsed = time.monotonic() - start
        self.assertIsNone(error, "The valid 500 ms native acknowledgement was rejected")
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertLess(elapsed, 1.4)
        self.assertEqual(self.response_requested, [True])
        self.assertEqual(
            [(domain, name) for domain, name, _data in self.calls],
            [
                ("media_player", "volume_set"),
                ("yandex_station", "send_command"),
                ("media_player", "volume_set"),
            ],
        )
        self.assertEqual(
            self.calls[1][2],
            {
                "entity_id": ENTITY,
                "command": "serverAction",
                "serverActionEventPayload": {
                    "type": "server_action",
                    "name": "update_form",
                    "payload": {
                        "form_update": {
                            "name": "personal_assistant.scenarios.quasar.iot.repeat_phrase",
                            "slots": [
                                {
                                    "type": "string",
                                    "name": "phrase_to_repeat",
                                    "value": '<speaker audio="dialogs-upload/11111111-1111-1111-1111-111111111111/22222222-2222-2222-2222-222222222222.opus">',
                                }
                            ],
                        },
                        "resubmit": True,
                    },
                },
            },
        )
        self.assertEqual(self.manager.status, "ready")
        self.assertEqual(self.hass.states.get(ENTITY).attributes["volume_level"], 0.2)
        self.assertEqual(
            self.hass.states.get(ENTITY).attributes["media_content_id"],
            "original-track",
        )

    async def test_native_error_and_malformed_responses_are_explicit(self) -> None:
        """A successful HA call must not conceal a rejected native command."""
        cases = [
            ({"status": "ERROR"}, "yandex_native_response_rejected"),
            ({"error": "TimeoutError()"}, "yandex_native_ack_timeout"),
            (
                {"error": "ConnectionResetError('private detail')"},
                "yandex_native_command_failed",
            ),
            ({"status": "SUCCESS", "error": ""}, "yandex_native_command_failed"),
            ({}, "yandex_native_response_invalid"),
            ({"status": []}, "yandex_native_response_invalid"),
        ]
        for response, expected in cases:
            with self.subTest(response=response):
                self.response = response
                with self.assertRaisesRegex(ProviderNotReadyError, expected) as caught:
                    await self.manager.announce("kodi")
                self.manager.failed(caught.exception)
                self.assertEqual(self.manager.public_status["reason"], expected)
                self.assertEqual(
                    self.hass.states.get(ENTITY).attributes["volume_level"], 0.2
                )
                self.assertFalse(self.manager.services.pending)

    async def test_missing_native_response_service_refuses_before_controls(
        self,
    ) -> None:
        """Missing capability must not pause music or raise the speaker volume."""
        self.manager.config["music_mode"] = "pause"
        self.hass.services.async_remove("yandex_station", "send_command")
        with self.assertRaisesRegex(
            ProviderNotReadyError, "yandex_native_response_service_unavailable"
        ):
            await self.manager.announce("kodi")
        self.assertEqual(self.calls, [])

    async def test_response_incapable_service_refuses_before_controls(self) -> None:
        """A legacy service without responses is not a fallback speech route."""
        self.hass.services.async_register("yandex_station", "send_command", self.native)
        with self.assertRaisesRegex(
            ProviderNotReadyError, "yandex_native_response_service_unavailable"
        ):
            await self.manager.announce("kodi")
        self.assertEqual(self.calls, [])

    async def test_missing_ack_keeps_recovery_inside_original_total_budget(
        self,
    ) -> None:
        """A native stall must leave the original recovery reserve available."""
        self.hold = asyncio.Event()
        start = time.monotonic()
        with self.assertRaises(ServiceTimeoutError):
            await asyncio.wait_for(self.manager.announce("kodi"), 2.1)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 1.3)
        self.assertLess(elapsed, 2.0)
        self.assertTrue(self.cancelled.is_set())
        self.assertFalse(self.manager.services.pending)
        self.assertEqual(self.hass.states.get(ENTITY).attributes["volume_level"], 0.2)
        self.assertEqual(self.manager.public_status["controller_budget_seconds"], 2.0)
        self.assertFalse(self.manager.public_status["controller_deadline_missed"])

    async def test_cancel_resistant_call_retains_ownership_and_blocks_replay(
        self,
    ) -> None:
        """A late physical provider must remain owned after the bounded caller exits."""
        self.hold = asyncio.Event()
        self.ignore_cancellation = True
        self.signal_completion = False
        with (
            patch.object(integration, "ATTEMPT_BUDGET", 0.18),
            patch.object(integration, "RECOVERY_RESERVE", 0.06),
            self.assertRaises(ServiceTimeoutError),
        ):
            await self.manager.announce("kodi")
        self.assertEqual(len(self.manager.services.pending), 1)
        with self.assertRaisesRegex(
            ProviderNotReadyError, "speaker_service_recovery_pending"
        ):
            await self.manager.announce("kodi")
        self.assertEqual(
            sum(name == "send_command" for _domain, name, _data in self.calls), 1
        )
        self.hold.set()
        await asyncio.gather(*self.manager.services.pending, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertFalse(self.manager.services.pending)

    async def test_native_success_without_speech_events_remains_unconfirmed(
        self,
    ) -> None:
        """An acknowledgement alone cannot establish that speech completed."""
        self.signal_completion = False
        await self.manager.announce("kodi")
        self.assertEqual(self.manager.status, "sent_unconfirmed")
        self.assertEqual(
            self.manager.public_status["reason"], "speaker_completion_not_observed"
        )
        self.assertFalse(self.manager.public_status["physical_latency_verified"])

    async def test_control_acknowledgement_limit_does_not_expand(self) -> None:
        """Speech's deadline must not give ordinary controls a longer timeout."""

        async def stalled_control(_call: ServiceCall) -> None:
            await asyncio.Event().wait()

        self.hass.services.async_register("media_player", "volume_set", stalled_control)
        start = time.monotonic()
        with self.assertRaises(ServiceTimeoutError):
            await self.manager.call(
                "volume_set", ENTITY, volume_level=0.5, deadline=start + 2.0
            )
        self.assertGreaterEqual(time.monotonic() - start, 0.39)
        self.assertLess(time.monotonic() - start, 0.6)
        self.assertFalse(self.manager.services.pending)
