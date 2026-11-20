"""Regression checks for migration of the existing physical universal remote."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ekf_appliances.ir_decode import RemoteMapper, decode_frames
from custom_components.ekf_appliances.ir_receiver import REMOTE_COMPONENT, IrReceiver
from custom_components.ekf_appliances.transport import Transport


def code(address=0xBF00, command=0xE916, repeats=0) -> Any:
    word = address | (command << 16)
    pulses = [9000, 4500]
    for bit in range(32):
        pulses += [560, 1690 if word & (1 << bit) else 560]
    pulses += [560]
    for _ in range(repeats):
        pulses += [40000, 9000, 2250, 560]
    return base64.b64encode(struct.pack("<" + "H" * len(pulses), *pulses)).decode()


class TestDecoder(unittest.TestCase):
    """Exercise test decoder contracts."""

    def test_existing_universal_up_and_apple_profile(self) -> None:
        frames = decode_frames(code())
        self.assertEqual((frames[0]["address"], frames[0]["command"]), (0xBF00, 0xE916))
        self.assertEqual(RemoteMapper().feed(frames, 10), ["up"])
        self.assertEqual(
            RemoteMapper().feed(decode_frames(code(0xBA00, 0xDC23)), 10), ["apple_tv"]
        )

    def test_hold_fires_once_after_two_nec_repeats(self) -> None:
        events = RemoteMapper().feed(decode_frames(code(command=0x44BB, repeats=4)), 10)
        self.assertEqual(events, ["prime", "prime_hold"])

    def test_first_repeat_does_not_duplicate_short_volume_press(self) -> None:
        mapper = RemoteMapper()
        self.assertEqual(
            mapper.feed(
                [{"repeat": False, "address": 0xBF00, "command": 0xBB44, "offset": 0}],
                10,
            ),
            ["volume_up"],
        )
        self.assertEqual(mapper.feed([{"repeat": True, "offset": 0}], 10.108), [])
        self.assertEqual(
            mapper.feed([{"repeat": True, "offset": 0}], 10.216), ["volume_up"]
        )

    def test_held_delete_repeats_after_initial_debounce_and_expires(self) -> None:
        mapper = RemoteMapper()
        self.assertEqual(mapper.feed(decode_frames(code(command=0x9E61)), 10), ["dash"])
        self.assertEqual(mapper.feed([{"repeat": True, "offset": 0}], 10.108), [])
        self.assertEqual(mapper.feed([{"repeat": True, "offset": 0}], 10.216), ["dash"])
        self.assertEqual(mapper.feed([{"repeat": True, "offset": 0}], 10.324), ["dash"])
        self.assertEqual(mapper.feed([{"repeat": True, "offset": 0}], 11), [])

    def test_digit_t9_cycles_and_resets_after_pause(self) -> None:
        mapper = RemoteMapper()
        frame = decode_frames(code(command=0xFD02))
        self.assertEqual(mapper.feed(frame, 10), ["t9_add_2_a"])
        self.assertEqual(mapper.feed(frame, 10.5), ["t9_cycle_2_b"])
        self.assertEqual(mapper.feed(frame, 11), ["t9_cycle_2_c"])
        self.assertEqual(mapper.feed(frame, 13), ["t9_add_2_a"])

    def test_wrong_address_invalid_complement_and_corrupt_data_do_nothing(self) -> None:
        self.assertEqual(RemoteMapper().feed(decode_frames(code(0x0707)), 10), [])
        self.assertEqual(decode_frames(code(command=0x1111)), [])
        self.assertEqual(decode_frames("not-base64"), [])
        self.assertEqual(decode_frames("A" * 40000), [])

    def test_orphan_repeat_cannot_hold_old_button(self) -> None:
        mapper = RemoteMapper()
        mapper.feed(decode_frames(code(command=0x44BB)), 10)
        self.assertEqual(mapper.feed([{"repeat": True, "offset": 0}], 12), [])

    def test_literal_samsung_source_vector_matches_esphome_fields(self) -> None:
        # Literal 4480/4480 source frame, wire E0E0807F, independent of NEC helper.
        pulses = [
            4480,
            4480,
            560,
            1680,
            560,
            1680,
            560,
            1680,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            1680,
            560,
            1680,
            560,
            1680,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            1680,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            560,
            1680,
            560,
            1680,
            560,
            1680,
            560,
            1680,
            560,
            1680,
            560,
            1680,
            560,
            1680,
            560,
        ]
        value = base64.b64encode(struct.pack("<" + "H" * len(pulses), *pulses)).decode()
        frames = decode_frames(value)
        self.assertEqual((frames[0]["address"], frames[0]["command"]), (0xE0E0, 0x807F))
        self.assertEqual(RemoteMapper().feed(frames, 10), ["samsung_ir_source"])


class FakeDevice:
    """Exercise fake device contracts."""

    unique_id = "verified-test-hub"

    def __init__(self) -> None:
        self._pending_updates = {}
        self.listeners = []
        self.commands = []
        self.probe_code = None
        self._api_lock = asyncio.Lock()
        self._api = self
        self.before_write = None
        self.during_write = None
        self.socket = object()
        self._running = self._api_protocol_working = True
        self._refresh_task = SimpleNamespace(done=lambda: False)

    def register_entity(self, entity) -> None:
        self.listeners.append(entity)

    async def async_unregister_entity(self, entity) -> None:
        self.listeners.remove(entity)

    async def _retry_on_failed_connection(self, callback, _error) -> Any:
        async with self._api_lock:
            if self.before_write:
                entered, proceed = self.before_write
                entered.set()
                await proceed.wait()
            return callback()

    def set_multiple_values(self, properties, *, nowait=True) -> None:

        command = json.loads(properties["201"])["control"]
        self.commands.append(command)
        if self.during_write:
            self.during_write()
        if command == "study" and self.probe_code:
            for listener in list(self.listeners):
                listener.on_receive({"202": self.probe_code}, full_poll=False)


class TestReceiver(unittest.IsolatedAsyncioTestCase):
    """Exercise test receiver contracts."""

    async def asyncSetUp(self) -> None:
        settle = patch(
            "custom_components.ekf_appliances.ir_receiver._STUDY_SETTLE_SECONDS", 0
        )
        settle.start()
        self.addCleanup(settle.stop)
        self.device = FakeDevice()
        self.remote = SimpleNamespace(
            _device=self.device,
            _send_dp=SimpleNamespace(id="201"),
            _receive_dp=SimpleNamespace(id="202"),
            _lock=asyncio.Lock(),
        )
        self.store = SimpleNamespace(
            async_load=AsyncMock(return_value={}),
            async_save=AsyncMock(),
            async_delay_save=Mock(),
        )
        self.hass = SimpleNamespace(
            data={REMOTE_COMPONENT: SimpleNamespace(get_entity=lambda _: self.remote)},
            loop=asyncio.get_running_loop(),
            bus=SimpleNamespace(async_listen=lambda *_: lambda: None),
            async_create_background_task=lambda task, _: asyncio.create_task(task),
        )
        runtime = SimpleNamespace(
            hass=self.hass,
            entry=SimpleNamespace(
                entry_id="test", data={"remote_entity_id": "remote.ekf"}
            ),
            transport=Transport(AsyncMock()),
        )
        with patch(
            "custom_components.ekf_appliances.ir_receiver.Store",
            return_value=self.store,
        ):
            self.receiver = IrReceiver(runtime)
        await self.receiver.async_load()
        self.events = []
        self.receiver.add_callback(
            lambda event: self.events.append(event) if event else None
        )

    async def asyncTearDown(self) -> None:
        await self.receiver.async_close()

    async def test_default_disabled_has_no_device_writes(self) -> None:
        await self.receiver.async_start()
        self.assertFalse(self.receiver.enabled)
        self.assertEqual(self.device.commands, [])

    async def test_probe_restores_learning_and_does_not_enable_dispatch(self) -> None:
        self.device.probe_code = code()
        result = await self.receiver.async_probe(0.1)
        self.assertEqual(result["events"], ["up"])
        self.assertTrue(result["validated"])
        self.assertFalse(self.receiver.enabled)
        self.assertEqual(self.device.commands, ["study_exit", "study", "study_exit"])
        self.assertEqual(self.events, [])

    async def test_timeout_also_exits_learning(self) -> None:
        result = await self.receiver.async_probe(0.01)
        self.assertFalse(result["received"])
        self.assertFalse(self.receiver.validated)
        self.assertEqual(self.device.commands, ["study_exit", "study", "study_exit"])

    async def test_cancel_also_exits_learning(self) -> None:
        task = asyncio.create_task(self.receiver.async_probe(10))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.device.commands, ["study_exit", "study", "study_exit"])
        self.assertIsNone(self.receiver.probe_waiter)

    async def test_opt_in_can_listen_until_first_known_press_without_probe(
        self,
    ) -> None:
        await self.receiver.async_set_enabled(enabled=True)
        await asyncio.sleep(0.01)
        self.assertTrue(self.receiver.enabled)
        self.assertFalse(self.receiver.validated)
        self.assertEqual(self.device.commands, ["study_exit", "study"])
        self.assertFalse(self.receiver.runtime.transport._lock.locked())
        self.assertFalse(self.remote._lock.locked())
        self.assertEqual(self.events, [])
        self.store.async_save.assert_awaited_with(
            {"enabled": True, "validated": False, "hub": None}
        )

    async def test_explicit_unvalidated_opt_in_survives_reload(self) -> None:
        self.store.async_load.return_value = {
            "enabled": True,
            "validated": False,
            "hub": None,
        }
        await self.receiver.async_load()
        await self.receiver.async_start()
        await asyncio.sleep(0.01)
        self.assertTrue(self.receiver.enabled)
        self.assertFalse(self.receiver.validated)
        self.assertEqual(self.device.commands, ["study_exit", "study"])

    async def test_bundled_ok_hold_carries_one_logical_feedback_identity(self) -> None:
        await self.receiver.async_set_enabled(enabled=True)
        await asyncio.sleep(0.01)
        captures = []
        self.receiver.add_callback(
            lambda name: (
                captures.append((name, self.receiver.last_press_id)) if name else None
            )
        )
        self.receiver.on_receive(
            {"202": code(command=0xEA15, repeats=4)}, full_poll=False
        )
        self.assertEqual([name for name, _ in captures], ["ok", "ok_hold"])
        self.assertIsNotNone(captures[0][1])
        self.assertEqual(captures[0][1], captures[1][1])

    async def test_first_known_press_validates_and_saves_existing_mapping(self) -> None:
        await self.receiver.async_set_enabled(enabled=True)
        await asyncio.sleep(0.01)
        self.receiver.on_receive({"202": code(command=0xEA15)}, full_poll=False)
        self.assertEqual(self.events, ["ok"])
        self.assertTrue(self.receiver.validated)
        self.assertEqual(self.receiver.validated_hub, self.device.unique_id)
        self.assertGreater(self.receiver.last_received, 0)
        self.store.async_delay_save.assert_called_once()
        snapshot, delay = self.store.async_delay_save.call_args.args
        self.assertEqual(
            snapshot(),
            {"enabled": True, "validated": True, "hub": self.device.unique_id},
        )
        self.assertEqual(delay, 1)
        self.receiver.on_receive({"202": code()}, full_poll=False)
        self.store.async_delay_save.assert_called_once()

    async def test_unknown_full_poll_and_reflection_cannot_validate_listener(
        self,
    ) -> None:
        await self.receiver.async_set_enabled(enabled=True)
        await asyncio.sleep(0.01)
        self.receiver.on_receive({"202": code(address=0x0707)}, full_poll=False)
        self.receiver.on_receive({"202": code()}, full_poll=True)
        self.receiver._service_called(
            SimpleNamespace(
                data={
                    "domain": "remote",
                    "service": "send_command",
                    "service_data": {"entity_id": "remote.ekf", "command": "known"},
                }
            )
        )
        self.receiver.on_receive({"202": code()}, full_poll=False)
        self.assertFalse(self.receiver.validated)
        self.assertIsNone(self.receiver.last_received)
        self.assertEqual(self.events, [])
        self.store.async_delay_save.assert_not_called()

    async def test_queued_native_command_cannot_be_overwritten_by_rearm(self) -> None:
        await self.receiver._attach()
        self.receiver.enabled = self.receiver.validated = True
        self.device._pending_updates["201"] = {
            "value": '{"control":"send_ir"}',
            "sent": False,
            "updated_at": time.time(),
        }
        await self.receiver.async_start()
        await asyncio.sleep(0.01)
        self.assertEqual(self.device.commands, [])

    async def test_external_remote_service_suppresses_reflections(self) -> None:
        await self.receiver._attach()
        self.receiver.enabled = True
        event = SimpleNamespace(
            data={
                "domain": "remote",
                "service": "send_command",
                "service_data": {"entity_id": "remote.ekf", "command": "b64:" + code()},
            }
        )
        self.receiver._service_called(event)
        self.receiver.on_receive({"202": code()}, full_poll=False)
        self.assertEqual(self.events, [])
        self.receiver.ignore_until = 0
        self.receiver.on_receive({"202": code()}, full_poll=False)
        self.assertEqual(self.events, ["up"])

    async def test_real_service_bus_status_subscriber_runs_on_homeassistant_loop(
        self,
    ) -> None:

        with tempfile.TemporaryDirectory() as config_dir:
            hass = HomeAssistant(config_dir)
            runtime = SimpleNamespace(
                hass=hass,
                entry=SimpleNamespace(
                    entry_id="test-loop", data={"remote_entity_id": "remote.ekf"}
                ),
                transport=Transport(AsyncMock()),
            )
            with patch(
                "custom_components.ekf_appliances.ir_receiver.Store",
                return_value=self.store,
            ):
                receiver = IrReceiver(runtime)
            owner = threading.get_ident()
            threads = []
            receiver.add_callback(lambda _event: threads.append(threading.get_ident()))
            try:
                hass.bus.async_fire(
                    "call_service",
                    {
                        "domain": "remote",
                        "service": "send_command",
                        "service_data": {
                            "entity_id": "remote.ekf",
                            "command": "b64:" + code(),
                        },
                    },
                )
                await hass.async_block_till_done()
                self.assertEqual(receiver.status, "paused_transmit")
                self.assertEqual(threads, [owner])
            finally:
                await receiver.async_close()
                await hass.async_block_till_done()

    async def test_full_status_snapshot_never_replays_old_code(self) -> None:
        self.receiver.enabled = True
        self.receiver.on_receive({"202": code()}, full_poll=True)
        self.assertEqual(self.events, [])

    async def test_other_hub_invalidates_previous_confirmation(self) -> None:
        self.receiver.validated = self.receiver.enabled = True
        self.receiver.validated_hub = "different-hub"
        with self.assertRaises(HomeAssistantError):
            await self.receiver._attach()
        self.assertFalse(self.receiver.validated)
        self.assertFalse(self.receiver.enabled)

    async def test_rearm_checks_native_pending_after_api_lock_wait(self) -> None:
        await self.receiver._attach()
        await self.device._api_lock.acquire()
        pending = {"value": '{"control":"send_ir","key":"keep-me"}', "sent": False}
        task = asyncio.create_task(self.receiver._mode("study"))
        await asyncio.sleep(0)
        self.device._pending_updates["201"] = pending
        self.device._api_lock.release()
        self.assertFalse(await task)
        self.assertIs(self.device._pending_updates["201"], pending)
        self.assertEqual(self.device.commands, [])

    async def test_native_send_queued_during_mode_write_is_preserved(self) -> None:
        await self.receiver._attach()
        pending = {"value": '{"control":"send_ir","key":"keep-me"}', "sent": False}

        def native_send() -> None:
            self.device._pending_updates["201"] = pending
            self.receiver._service_called(
                SimpleNamespace(
                    data={
                        "domain": "remote",
                        "service": "send_command",
                        "service_data": {"entity_id": "remote.ekf", "command": "known"},
                    }
                )
            )

        self.device.during_write = native_send
        self.assertTrue(await self.receiver._mode("study"))
        self.assertIs(self.device._pending_updates["201"], pending)
        self.assertEqual(self.device.commands, ["study"])
        self.assertFalse(self.receiver.armed)

    async def test_close_cancels_probe_before_unregistering(self) -> None:
        task = asyncio.create_task(self.receiver.async_probe(10))
        await asyncio.sleep(0.01)
        await self.receiver.async_close()
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.receiver.probe_waiter)
        self.assertIsNone(self.receiver._probe_task)
        self.assertFalse(self.remote._lock.locked())
        self.assertFalse(self.receiver.runtime.transport._lock.locked())
        self.assertEqual(self.device.listeners, [])
        self.assertEqual(self.device.commands, ["study_exit", "study", "study_exit"])

    async def test_cancel_during_native_write_keeps_lock_until_executor_finishes(
        self,
    ) -> None:
        entered, proceed = asyncio.Event(), asyncio.Event()
        self.device.before_write = (entered, proceed)
        task = asyncio.create_task(self.receiver.async_probe(10))
        await entered.wait()
        closer = asyncio.create_task(self.receiver.async_close())
        await asyncio.sleep(0)
        self.assertFalse(closer.done())
        self.assertTrue(self.device._api_lock.locked())
        proceed.set()
        await closer
        self.assertTrue(task.cancelled())
        self.assertFalse(self.device._api_lock.locked())
        self.assertEqual(self.device.listeners, [])

    async def test_close_never_overwrites_a_queued_native_send(self) -> None:
        await self.receiver._attach()
        self.receiver.armed = True
        pending = {"value": '{"control":"send_ir","key":"keep-me"}', "sent": False}
        self.device._pending_updates["201"] = pending
        await self.receiver.async_close()
        self.assertIs(self.device._pending_updates["201"], pending)
        self.assertEqual(self.device.commands, [])

    async def test_probe_does_not_validate_self_reflection_after_direct_send(
        self,
    ) -> None:
        task = asyncio.create_task(self.receiver.async_probe(0.03))
        await asyncio.sleep(0.01)
        self.receiver._service_called(
            SimpleNamespace(
                data={
                    "domain": "remote",
                    "service": "send_command",
                    "service_data": {"entity_id": "remote.ekf", "command": "known"},
                }
            )
        )
        self.receiver.on_receive({"202": code()}, full_poll=False)
        result = await task
        self.assertFalse(result["received"])
        self.assertFalse(result["validated"])

    async def test_rearm_waits_for_native_learn_lock(self) -> None:
        await self.receiver._attach()
        self.receiver.enabled = self.receiver.validated = True
        await self.remote._lock.acquire()
        await self.receiver.async_start()
        await asyncio.sleep(0.01)
        self.assertEqual(self.device.commands, [])
        self.receiver.manual_until = time.monotonic() + 30
        self.remote._lock.release()
        await asyncio.sleep(0.01)
        self.assertEqual(self.device.commands, [])


class SingleShotDevice(FakeDevice):
    """Tuya study session: capture/expiry consumes it until explicit study_exit."""

    def __init__(self) -> None:
        super().__init__()
        self.session_open = False
        self.listening = False
        self.socket = object()
        self._running = True
        self._api_protocol_working = True
        self._refresh_task = SimpleNamespace(done=lambda: False)

    def set_multiple_values(self, properties, *, nowait=True) -> None:

        super().set_multiple_values(properties, nowait=nowait)
        command = json.loads(properties["201"])["control"]
        if command == "study_exit":
            self.session_open = self.listening = False
        elif command == "study" and not self.session_open:
            self.session_open = self.listening = True

    def press(self, value) -> Any:
        if not self.listening or self.socket is None:
            return False
        self.listening = False
        for listener in list(self.listeners):
            listener.on_receive({"202": value}, full_poll=False)
        return True


class TestContinuousReceiver(TestReceiver):
    """Exercise test continuous receiver contracts."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.device = SingleShotDevice()
        self.remote._device = self.device

    async def wait_for(self, predicate, deadline_seconds=1) -> None:
        async with asyncio.timeout(deadline_seconds):
            while not predicate():  # noqa: ASYNC110 - Deadline-bounded fixture polls native state without an event.
                await asyncio.sleep(0.01)

    async def test_two_separate_identical_presses_reach_event_after_rearm(self) -> None:
        await self.receiver.async_set_enabled(enabled=True)
        await self.wait_for(lambda: self.device.listening)
        self.assertTrue(self.device.press(code(command=0xE41B)))
        await asyncio.sleep(0.4)
        self.device.press(code(command=0xE41B))
        self.assertEqual(self.events, ["smart", "smart"])

    async def test_idle_expiry_does_not_leave_an_optimistic_but_deaf_receiver(
        self,
    ) -> None:
        await self.receiver.async_set_enabled(enabled=True)
        await self.wait_for(lambda: self.device.listening)
        self.device.listening = False  # The firmware's learning timeout.
        self.receiver.last_arm -= 16
        await asyncio.sleep(0.4)
        self.device.press(code(command=0xF20D))
        self.assertEqual(self.events, ["power"])

    async def test_native_reconnect_renews_the_hardware_learning_session(self) -> None:
        await self.receiver.async_set_enabled(enabled=True)
        await self.wait_for(lambda: self.device.listening)
        self.device.socket = None
        self.device.listening = False
        await asyncio.sleep(0.25)
        self.device.socket = object()  # The existing native reader reconnects.
        await asyncio.sleep(0.4)
        self.device.press(code(command=0xE41B))
        self.assertEqual(self.events, ["smart"])

    async def test_unknown_capture_consumes_session_but_next_known_press_still_arrives(
        self,
    ) -> None:
        await self.receiver.async_set_enabled(enabled=True)
        await self.wait_for(lambda: self.device.listening)
        self.assertTrue(self.device.press("not-base64"))
        await asyncio.sleep(0.4)
        self.device.press(code(command=0xE41B))
        self.assertEqual(self.events, ["smart"])
        self.assertEqual(self.receiver.capture_count, 2)
        self.assertEqual(self.receiver.event_count, 1)

    async def test_periodic_snapshot_cannot_replay_code_or_suppress_next_press(
        self,
    ) -> None:
        await self.receiver.async_set_enabled(enabled=True)
        await self.wait_for(lambda: self.device.listening)
        self.receiver.on_receive(
            {"201": '{"control":"send_ir"}', "202": code(command=0xE41B)},
            full_poll=True,
        )
        self.assertEqual(self.events, [])
        self.device.press(code(command=0xE41B))
        self.assertEqual(self.events, ["smart"])
        self.assertEqual(self.receiver.cached_capture_count, 1)
        self.assertEqual(self.receiver.capture_count, 1)

    async def test_no_reset_flood_between_captures_or_renewal_deadline(self) -> None:
        await self.receiver.async_set_enabled(enabled=True)
        await self.wait_for(lambda: self.device.listening)
        await asyncio.sleep(0.65)
        self.assertEqual(self.receiver.study_sessions, 1)
        self.assertEqual(self.device.commands, ["study_exit", "study"])

    async def test_send_pause_drops_reflection_and_resumes_for_same_code(self) -> None:
        await self.receiver.async_set_enabled(enabled=True)
        await self.wait_for(lambda: self.device.listening)
        self.device.press(code(command=0xE41B))
        async with self.receiver.runtime.transport._lock:
            await self.receiver.async_before_send()
            self.receiver._service_called(
                SimpleNamespace(
                    data={
                        "domain": "remote",
                        "service": "send_command",
                        "service_data": {"entity_id": "remote.ekf", "command": "known"},
                    }
                )
            )
        self.receiver.on_receive({"202": code(command=0xE41B)}, full_poll=False)
        self.assertEqual(self.events, ["smart"])
        self.assertFalse(self.device.listening)
        self.receiver.ignore_until = 0
        await self.wait_for(lambda: self.device.listening)
        self.device.press(code(command=0xE41B))
        self.assertEqual(self.events, ["smart", "smart"])
        self.assertEqual(self.receiver.suppressed_capture_count, 1)

    async def test_send_queued_between_exit_and_start_is_not_overwritten(self) -> None:
        await self.receiver._attach()
        self.receiver.enabled = True

        async def renew() -> Any:
            async with self.receiver.runtime.transport._lock, self.remote._lock:
                return await self.receiver._renew_study()

        with patch(
            "custom_components.ekf_appliances.ir_receiver._STUDY_SETTLE_SECONDS", 0.05
        ):
            task = asyncio.create_task(renew())
            await self.wait_for(lambda: "study_exit" in self.device.commands)
            pending = {"value": '{"control":"send_ir","key":"keep-me"}', "sent": False}
            self.device._pending_updates["201"] = pending
            self.receiver._service_called(
                SimpleNamespace(
                    data={
                        "domain": "remote",
                        "service": "send_command",
                        "service_data": {"entity_id": "remote.ekf", "command": "known"},
                    }
                )
            )
            self.assertFalse(await task)
        self.assertIs(self.device._pending_updates["201"], pending)
        self.assertEqual(self.device.commands, ["study_exit"])
        self.assertFalse(self.receiver.armed)

    async def test_disconnection_and_recovery_are_reported_without_false_listening(
        self,
    ) -> None:
        changes = []
        self.receiver.add_callback(
            lambda _: changes.append((self.receiver.status, self.receiver.last_error))
        )
        await self.receiver.async_set_enabled(enabled=True)
        await self.wait_for(lambda: self.device.listening)
        self.device.socket = None
        self.device.listening = False
        await self.wait_for(lambda: self.receiver.status == "disconnected")
        self.assertFalse(self.receiver.diagnostics["study_requested"])
        self.assertFalse(self.receiver.diagnostics["native_socket_connected"])
        self.assertIsNotNone(self.receiver.last_error)
        self.device.socket = object()
        await self.wait_for(lambda: self.device.listening)
        self.assertIsNone(self.receiver.last_error)
        self.assertIn(("study_requested", None), changes)
        self.assertTrue(self.receiver.diagnostics["listener_running"])

    async def test_failed_write_does_not_leave_study_requested_true(self) -> None:
        await self.receiver._attach()
        self.receiver.armed = True
        with (
            patch.object(
                self.device, "_retry_on_failed_connection", AsyncMock(return_value=None)
            ),
            self.assertRaisesRegex(Exception, "mode write failed"),
        ):
            await self.receiver._mode("study")
        self.assertFalse(self.receiver.armed)


if __name__ == "__main__":
    unittest.main()
