"""Exercise quiet rejection transport and recovery in the real HA runtime."""

from __future__ import annotations

import asyncio
import importlib.util
import time
import unittest
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import patch

from homeassistant.core import SupportsResponse

from custom_components.remote_announcements import rejection


class RejectionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    """Use real state/events/manager; only physical speaker services are simulated."""

    async def asyncSetUp(self) -> None:
        # Keep this fixture available under both unittest and pytest importlib mode.
        spec = importlib.util.spec_from_file_location(
            "remote_announcement_fixture",
            Path(__file__).with_name("test_remote_announcements_ha.py"),
        )
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        self.config = helper.CONFIG
        self.fixture = helper.RuntimeTests()
        await self.fixture.asyncSetUp()
        self.manager = self.fixture.manager
        self.hass = self.fixture.hass
        self.entity = self.fixture.entity

    async def asyncTearDown(self) -> None:
        await self.manager.rejections.stop(0.1)
        await self.fixture.asyncTearDown()

    async def reject(self, press_id="press:1") -> None:
        self.manager.rejections.submit("device_off", press_id)
        await self.manager.rejections.drain()

    async def test_native_cue_is_quiet_and_preserves_playing_music_and_volume(
        self,
    ) -> None:
        await self.reject()
        calls = self.fixture.calls
        self.assertEqual(
            [name for name, _ in calls], ["volume_set", "send_command", "volume_set"]
        )
        self.assertEqual(calls[0][1]["volume_level"], 0.1)
        self.assertEqual(calls[-1][1]["volume_level"], 0.2)
        slot = calls[1][1]["serverActionEventPayload"]["payload"]["form_update"][
            "slots"
        ][0]
        self.assertEqual(
            slot["value"], '<speaker audio="alice-sounds-game-ping-1.opus">'
        )
        state = self.hass.states.get(self.entity)
        self.assertEqual(state.state, "playing")
        self.assertEqual(state.attributes["media_content_id"], "original-track")
        self.assertEqual(state.attributes["volume_level"], 0.2)
        self.assertEqual(self.manager.public_status["rejection_sent"], 1)

    async def test_human_volume_change_during_cue_wins(self) -> None:
        self.fixture.manual_volume = 0.7
        await self.reject()
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.7
        )
        self.assertEqual(
            [name for name, _ in self.fixture.calls], ["volume_set", "send_command"]
        )

    async def test_human_lower_volume_before_dispatch_is_not_raised_or_restored(
        self,
    ) -> None:
        original = self.manager.call

        async def human_changes_before_call(service, entity, **data: Any) -> bool:
            state = self.hass.states.get(entity)
            self.hass.states.async_set(
                entity, state.state, {**state.attributes, "volume_level": 0.05}
            )
            return await original(service, entity, **data)

        with patch.object(self.manager, "call", human_changes_before_call):
            await self.reject()
        self.assertEqual(self.fixture.calls, [])
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.05
        )

    async def test_muted_or_zero_speaker_is_not_raised(self) -> None:
        before = self.hass.states.get(self.entity)
        for index, attrs in enumerate(
            (
                {"volume_level": 0.4, "is_volume_muted": True},
                {"volume_level": 0.0, "is_volume_muted": False},
            )
        ):
            self.hass.states.async_set(
                self.entity, before.state, {**before.attributes, **attrs}
            )
            await self.reject(f"muted:{index}")
        self.assertEqual(self.fixture.calls, [])
        self.assertEqual(self.manager.public_status["rejection_dropped"], 2)

    async def test_unavailable_or_restored_state_is_rejected_before_volume_io(
        self,
    ) -> None:
        before = self.hass.states.get(self.entity)
        for index, (state, attrs) in enumerate(
            (
                ("unknown", {}),
                ("unavailable", {}),
                ("playing", {"restored": True}),
            )
        ):
            self.hass.states.async_set(
                self.entity, state, {**before.attributes, **attrs}
            )
            await self.reject(f"unavailable:{index}")
        self.assertEqual(self.fixture.calls, [])

    async def test_missing_quiet_volume_readback_prevents_sound(self) -> None:
        async def ignore_volume(call) -> None:
            self.fixture.calls.append((call.service, dict(call.data)))

        self.hass.services.async_register("media_player", "volume_set", ignore_volume)
        await self.reject()
        self.assertEqual([name for name, _ in self.fixture.calls], ["volume_set"])
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.2
        )
        self.assertEqual(
            self.manager.public_status["rejection_drop_reason"],
            "quiet_volume_unconfirmed",
        )

    async def test_native_rejection_failure_restores_original_volume(self) -> None:
        async def reject_native(call) -> dict[str, str]:
            self.fixture.calls.append((call.service, dict(call.data)))
            return {"status": "ERROR"}

        self.hass.services.async_register(
            "yandex_station",
            "send_command",
            reject_native,
            supports_response=SupportsResponse.OPTIONAL,
        )
        with patch.object(rejection, "CUE_SETTLE_SECONDS", 0.001):
            await self.reject()
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.2
        )
        self.assertEqual(self.manager.public_status["rejection_dropped"], 1)
        self.assertEqual(
            [name for name, _ in self.fixture.calls],
            ["volume_set", "send_command", "volume_set"],
        )

    async def test_late_native_ack_cannot_consume_reserved_quiet_interval(self) -> None:
        acknowledged = []
        restored = []
        original_media = self.fixture.media

        async def late_ack(_call) -> dict[str, str]:
            # Model a request already accepted by the speaker when its caller
            # tries to cancel the late acknowledgement.
            with suppress(asyncio.CancelledError):
                await asyncio.sleep(0.25)
            acknowledged.append(time.monotonic())
            return {"status": "SUCCESS"}

        async def observe_restore(call) -> None:
            if call.data["volume_level"] == 0.2:
                restored.append(time.monotonic())
            await original_media(call)

        self.hass.services.async_register(
            "yandex_station",
            "send_command",
            late_ack,
            supports_response=SupportsResponse.OPTIONAL,
        )
        self.hass.services.async_register("media_player", "volume_set", observe_restore)
        with (
            patch.object(rejection, "CUE_BUDGET", 1.0),
            patch.object(rejection, "CUE_SETTLE_SECONDS", 0.2),
        ):
            await self.reject()
        self.assertEqual(len(acknowledged), 1)
        self.assertEqual(len(restored), 1)
        self.assertGreaterEqual(restored[0] - acknowledged[0], 0.19)
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.2
        )

    async def test_cue_waits_for_spoken_label_and_its_restore(self) -> None:
        started = asyncio.Event()
        self.fixture.speech_gate = asyncio.Event()
        original_native = self.fixture.native

        async def observe_start(call) -> dict[str, str]:
            started.set()
            return await original_native(call)

        self.hass.services.async_register(
            "yandex_station",
            "send_command",
            observe_start,
            supports_response=SupportsResponse.OPTIONAL,
        )
        speech = asyncio.create_task(self.manager.announce("tv"))
        await asyncio.wait_for(started.wait(), 0.5)
        self.manager.rejections.submit("device_off", "waiting")
        await asyncio.sleep(0)
        self.assertEqual(
            [
                data["volume_level"]
                for name, data in self.fixture.calls
                if name == "volume_set"
            ],
            [0.4],
        )
        self.fixture.speech_gate.set()
        await speech
        await self.manager.rejections.drain()
        self.assertEqual(
            [
                data["volume_level"]
                for name, data in self.fixture.calls
                if name == "volume_set"
            ],
            [0.4, 0.2, 0.1, 0.2],
        )
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["media_content_id"],
            "original-track",
        )

    async def test_stop_retains_active_task_until_its_volume_recovery_finishes(
        self,
    ) -> None:
        started = asyncio.Event()

        async def waiting_native(_call) -> dict[str, str]:
            started.set()
            await asyncio.Event().wait()
            return {"status": "SUCCESS"}

        self.hass.services.async_register(
            "yandex_station",
            "send_command",
            waiting_native,
            supports_response=SupportsResponse.OPTIONAL,
        )
        with patch.object(rejection, "CUE_SETTLE_SECONDS", 0.2):
            self.manager.rejections.submit("device_off", "shutdown")
            await asyncio.wait_for(started.wait(), 0.5)
            task = self.manager.rejections.task
            await self.manager.rejections.stop(0.01)
            self.assertIs(self.manager.rejections.task, task)
            self.assertFalse(task.done())
            with suppress(asyncio.CancelledError):
                await task
        self.assertEqual(
            self.hass.states.get(self.entity).attributes["volume_level"], 0.2
        )
        self.assertFalse(self.manager.services.pending)

    async def test_aisis_uses_existing_native_sound_and_confirms_restore(self) -> None:
        entity = self.config["aisis_entity"]
        self.hass.states.async_set(self.config["backend_entity"], "AISIS")
        self.hass.states.async_set(self.config["aisis_connected_entity"], "on")
        self.hass.states.async_set(
            entity, "playing", {"volume_level": 0.4, "media_content_id": "aisis-track"}
        )

        async def native_sound(call) -> None:
            self.fixture.calls.append((call.service, dict(call.data)))

        self.hass.services.async_register(
            "aisis_speaker", "send_directive", native_sound
        )
        with patch.object(rejection, "CUE_SETTLE_SECONDS", 0.001):
            await self.reject()
        calls = self.fixture.calls
        self.assertEqual(
            [name for name, _ in calls], ["volume_set", "send_directive", "volume_set"]
        )
        self.assertEqual(
            calls[1][1],
            {
                "duid": "M00ZRW3008RAJG",
                "name": "smart_speaker_play_iot_sound",
                "arguments": {"isPowerOn": False},
            },
        )
        state = self.hass.states.get(entity)
        self.assertEqual(state.state, "playing")
        self.assertEqual(state.attributes["media_content_id"], "aisis-track")
        self.assertEqual(state.attributes["volume_level"], 0.4)
        self.assertFalse(self.manager.aisis_volume_recovery)


if __name__ == "__main__":
    unittest.main()
