"""Bounded HA runtime tests; services and cloud synthesis are external boundaries."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
from homeassistant import auth, loader
from homeassistant.components.input_select import InputSelect
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import (
    EVENT_STATE_CHANGED,
    Event,
    HomeAssistant,
    State,
    SupportsResponse,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame, restore_state
from homeassistant.setup import async_setup_component

from custom_components import remote_announcements as integration
from custom_components.remote_announcements.providers import ProviderNotReadyError

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = yaml.safe_load((ROOT / "packages/remote_announcements.yaml").read_text())
CONFIG = integration.CONFIG_SCHEMA(PACKAGE)[integration.DOMAIN]


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
        self.hass.states.async_set(CONFIG["target_entity"], "tv", {"initialized": True})
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

    async def test_muted_speaker_gets_floor_then_original_level_and_mute(self) -> None:
        state = self.hass.states.get(self.entity)
        self.hass.states.async_set(
            self.entity, state.state, {**state.attributes, "is_volume_muted": True}
        )
        await self.manager.announce("tv")
        self.assertEqual(
            self.calls[0],
            ("volume_set", {"entity_id": self.entity, "volume_level": 0.5}),
        )
        self.assertEqual(self.calls[-1][0], "volume_mute")
        state = self.hass.states.get(self.entity)
        self.assertTrue(state.attributes["is_volume_muted"])
        self.assertEqual(state.attributes["volume_level"], 0.2)

    async def test_zero_volume_is_restored(self) -> None:
        state = self.hass.states.get(self.entity)
        self.hass.states.async_set(
            self.entity, state.state, {**state.attributes, "volume_level": 0.0}
        )
        await self.manager.announce("tv")
        self.assertEqual(self.calls[0][1]["volume_level"], 0.5)
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
        self.volume_sequence = [0.8, 0.5]
        await self.manager.announce("tv")
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.5
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
        self.hass.states.async_set(
            CONFIG["target_entity"], "kodi", {"initialized": True}
        )
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
                CONFIG["target_entity"], "kodi", {"initialized": True}
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
        self.hass.states.async_set(
            CONFIG["target_entity"], "kodi", {"initialized": True}
        )
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
                        CONFIG["target_entity"], "kodi", {"initialized": True}
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
        self.assertEqual(self.calls, [])

    def prepare_aisis_native_volume(self, volume: float, *, muted: bool = False) -> str:
        """Publish AISIS state through its supported integer-volume boundary."""
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

        async def volume_set(call) -> None:
            self.calls.append((call.service, dict(call.data)))
            self.assertEqual(call.data["entity_id"], entity)
            # Literal supported native levels, not an acknowledgement-only fake.
            level = {0.0: 0, 0.1: 1, 0.5: 5}[call.data["volume_level"]]
            self.aisis_native_levels.append(level)
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
                self.assertEqual(self.aisis_native_levels, [5, native_level])
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

    async def test_aisis_reported_mute_remains_unsupported(self) -> None:
        self.prepare_aisis_native_volume(0.0, muted=True)
        with self.assertRaisesRegex(
            ProviderNotReadyError, "aisis_cannot_restore_muted"
        ):
            await self.manager.announce("tv")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.aisis_native_levels, [])

    async def test_aisis_negative_volume_remains_rejected(self) -> None:
        self.prepare_aisis_native_volume(-0.1)
        with self.assertRaises(ProviderNotReadyError):
            await self.manager.announce("tv")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.aisis_native_levels, [])

    async def test_aisis_human_volume_change_prevents_zero_restore(self) -> None:
        entity = self.prepare_aisis_native_volume(0.0)
        self.manual_volume = 0.8
        await self.manager.announce("tv")
        self.assertEqual(self.aisis_native_levels, [5])
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
