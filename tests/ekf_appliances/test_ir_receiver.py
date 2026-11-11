"""Regression checks for migration of the existing physical universal remote."""
from __future__ import annotations
import asyncio
import base64
import struct
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import homeassistant  # Install HA's schema compatibility before integration imports.
from custom_components.ekf_appliances.ir_decode import RemoteMapper, decode_frames
from custom_components.ekf_appliances.ir_receiver import IrReceiver, REMOTE_COMPONENT


def code(address=0xBF00, command=0xE916, repeats=0):
    word = address | (command << 16)
    pulses = [9000, 4500]
    for bit in range(32):
        pulses += [560, 1690 if word & (1 << bit) else 560]
    pulses += [560]
    for _ in range(repeats):
        pulses += [40000, 9000, 2250, 560]
    return base64.b64encode(struct.pack("<" + "H" * len(pulses), *pulses)).decode()


class TestDecoder(unittest.TestCase):
    def test_existing_universal_up_and_apple_profile(self):
        frames = decode_frames(code())
        self.assertEqual((frames[0]["address"], frames[0]["command"]), (0xBF00, 0xE916))
        self.assertEqual(RemoteMapper().feed(frames, 10), ["up"])
        self.assertEqual(RemoteMapper().feed(decode_frames(code(0xBA00, 0xDC23)), 10), ["apple_tv"])

    def test_hold_fires_once_after_two_nec_repeats(self):
        events = RemoteMapper().feed(decode_frames(code(command=0x44BB, repeats=4)), 10)
        self.assertEqual(events, ["prime", "prime_hold"])

    def test_first_repeat_does_not_duplicate_short_volume_press(self):
        mapper = RemoteMapper()
        self.assertEqual(mapper.feed([{"repeat": False, "address": 0xBF00,
                                      "command": 0xBB44, "offset": 0}], 10), ["volume_up"])
        self.assertEqual(mapper.feed([{"repeat": True, "offset": 0}], 10.108), [])
        self.assertEqual(mapper.feed([{"repeat": True, "offset": 0}], 10.216), ["volume_up"])

    def test_digit_t9_cycles_and_resets_after_pause(self):
        mapper = RemoteMapper()
        frame = decode_frames(code(command=0xFD02))
        self.assertEqual(mapper.feed(frame, 10), ["t9_add_2_a"])
        self.assertEqual(mapper.feed(frame, 10.5), ["t9_cycle_2_b"])
        self.assertEqual(mapper.feed(frame, 11), ["t9_cycle_2_c"])
        self.assertEqual(mapper.feed(frame, 13), ["t9_add_2_a"])

    def test_wrong_address_invalid_complement_and_corrupt_data_do_nothing(self):
        self.assertEqual(RemoteMapper().feed(decode_frames(code(0x0707)), 10), [])
        self.assertEqual(decode_frames(code(command=0x1111)), [])
        self.assertEqual(decode_frames("not-base64"), [])
        self.assertEqual(decode_frames("A" * 40000), [])

    def test_orphan_repeat_cannot_hold_old_button(self):
        mapper = RemoteMapper()
        mapper.feed(decode_frames(code(command=0x44BB)), 10)
        self.assertEqual(mapper.feed([{"repeat": True, "offset": 0}], 12), [])

    def test_literal_samsung_source_vector_matches_esphome_fields(self):
        # Literal 4480/4480 source frame, wire E0E0807F, independent of NEC helper.
        pulses = [4480,4480,560,1680,560,1680,560,1680,560,560,560,560,560,560,560,560,560,560,560,1680,560,1680,560,1680,560,560,560,560,560,560,560,560,560,560,560,1680,560,560,560,560,560,560,560,560,560,560,560,560,560,560,560,560,560,1680,560,1680,560,1680,560,1680,560,1680,560,1680,560,1680,560]
        value = base64.b64encode(struct.pack("<" + "H" * len(pulses), *pulses)).decode()
        frames = decode_frames(value)
        self.assertEqual((frames[0]["address"], frames[0]["command"]), (0xE0E0, 0x807F))
        self.assertEqual(RemoteMapper().feed(frames, 10), ["samsung_ir_source"])


class FakeDevice:
    unique_id = "verified-test-hub"
    def __init__(self):
        self._pending_updates = {}
        self.listeners = []
        self.commands = []
        self.probe_code = None
        self._api_lock = asyncio.Lock()
        self._api = self
        self.before_write = None
        self.during_write = None

    def register_entity(self, entity):
        self.listeners.append(entity)

    async def async_unregister_entity(self, entity):
        self.listeners.remove(entity)

    async def _retry_on_failed_connection(self, callback, _error):
        async with self._api_lock:
            if self.before_write:
                entered, proceed = self.before_write
                entered.set()
                await proceed.wait()
            return callback()

    def set_multiple_values(self, properties, nowait=True):
        import json
        command = json.loads(properties["201"])["control"]
        self.commands.append(command)
        if self.during_write:
            self.during_write()
        if command == "study" and self.probe_code:
            for listener in list(self.listeners):
                listener.on_receive({"202": self.probe_code}, False)
        return None



class TestReceiver(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.device = FakeDevice()
        self.remote = SimpleNamespace(_device=self.device, _send_dp=SimpleNamespace(id="201"),
                                      _receive_dp=SimpleNamespace(id="202"), _lock=asyncio.Lock())
        self.store = SimpleNamespace(async_load=AsyncMock(return_value={}), async_save=AsyncMock())
        self.hass = SimpleNamespace(
            data={REMOTE_COMPONENT: SimpleNamespace(get_entity=lambda _: self.remote)},
            loop=asyncio.get_running_loop(),
            bus=SimpleNamespace(async_listen=lambda *_: lambda: None),
            async_create_background_task=lambda task, _: asyncio.create_task(task),
        )
        runtime = SimpleNamespace(hass=self.hass, entry=SimpleNamespace(entry_id="test",
                                  data={"remote_entity_id": "remote.ekf"}),
                                  transport=SimpleNamespace(_lock=asyncio.Lock()))
        with patch("custom_components.ekf_appliances.ir_receiver.Store", return_value=self.store):
            self.receiver = IrReceiver(runtime)
        await self.receiver.async_load()
        self.events = []
        self.receiver.add_callback(lambda event: self.events.append(event) if event else None)

    async def asyncTearDown(self):
        await self.receiver.async_close()

    async def test_default_disabled_has_no_device_writes(self):
        await self.receiver.async_start()
        self.assertFalse(self.receiver.enabled)
        self.assertEqual(self.device.commands, [])

    async def test_probe_restores_learning_and_does_not_enable_dispatch(self):
        self.device.probe_code = code()
        result = await self.receiver.async_probe(0.1)
        self.assertEqual(result["events"], ["up"])
        self.assertTrue(result["validated"])
        self.assertFalse(self.receiver.enabled)
        self.assertEqual(self.device.commands, ["study", "study_exit"])
        self.assertEqual(self.events, [])

    async def test_timeout_also_exits_learning(self):
        result = await self.receiver.async_probe(0.01)
        self.assertFalse(result["received"])
        self.assertFalse(self.receiver.validated)
        self.assertEqual(self.device.commands, ["study", "study_exit"])

    async def test_cancel_also_exits_learning(self):
        task = asyncio.create_task(self.receiver.async_probe(10))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.device.commands, ["study", "study_exit"])
        self.assertIsNone(self.receiver.probe_waiter)

    async def test_enable_without_physical_verification_is_rejected(self):
        with self.assertRaises(Exception):
            await self.receiver.async_set_enabled(True)
        self.assertFalse(self.receiver.enabled)
        self.assertEqual(self.device.commands, [])

    async def test_queued_native_command_cannot_be_overwritten_by_rearm(self):
        await self.receiver._attach()
        self.receiver.enabled = self.receiver.validated = True
        self.device._pending_updates["201"] = {"value": '{"control":"send_ir"}', "sent": False,
                                                "updated_at": time.time()}
        await self.receiver.async_start()
        await asyncio.sleep(0.01)
        self.assertEqual(self.device.commands, [])

    async def test_external_remote_service_suppresses_reflections(self):
        await self.receiver._attach()
        self.receiver.enabled = True
        event = SimpleNamespace(data={"domain": "remote", "service": "send_command",
                                      "service_data": {"entity_id": "remote.ekf", "command": "b64:" + code()}})
        self.receiver._service_called(event)
        self.receiver.on_receive({"202": code()}, False)
        self.assertEqual(self.events, [])
        self.receiver.ignore_until = 0
        self.receiver.on_receive({"202": code()}, False)
        self.assertEqual(self.events, ["up"])

    async def test_full_status_snapshot_never_replays_old_code(self):
        self.receiver.enabled = True
        self.receiver.on_receive({"202": code()}, True)
        self.assertEqual(self.events, [])

    async def test_other_hub_invalidates_previous_confirmation(self):
        self.receiver.validated = self.receiver.enabled = True
        self.receiver.validated_hub = "different-hub"
        with self.assertRaises(Exception):
            await self.receiver._attach()
        self.assertFalse(self.receiver.validated)
        self.assertFalse(self.receiver.enabled)

    async def test_rearm_checks_native_pending_after_api_lock_wait(self):
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

    async def test_native_send_queued_during_mode_write_is_preserved(self):
        await self.receiver._attach()
        pending = {"value": '{"control":"send_ir","key":"keep-me"}', "sent": False}
        def native_send():
            self.device._pending_updates["201"] = pending
            self.receiver._service_called(SimpleNamespace(data={
                "domain": "remote", "service": "send_command",
                "service_data": {"entity_id": "remote.ekf", "command": "known"}
            }))
        self.device.during_write = native_send
        self.assertTrue(await self.receiver._mode("study"))
        self.assertIs(self.device._pending_updates["201"], pending)
        self.assertEqual(self.device.commands, ["study"])
        self.assertFalse(self.receiver.armed)

    async def test_close_cancels_probe_before_unregistering(self):
        task = asyncio.create_task(self.receiver.async_probe(10))
        await asyncio.sleep(0.01)
        await self.receiver.async_close()
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.receiver.probe_waiter)
        self.assertIsNone(self.receiver._probe_task)
        self.assertFalse(self.remote._lock.locked())
        self.assertFalse(self.receiver.runtime.transport._lock.locked())
        self.assertEqual(self.device.listeners, [])
        self.assertEqual(self.device.commands, ["study", "study_exit"])

    async def test_cancel_during_native_write_keeps_lock_until_executor_finishes(self):
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

    async def test_close_never_overwrites_a_queued_native_send(self):
        await self.receiver._attach()
        self.receiver.armed = True
        pending = {"value": '{"control":"send_ir","key":"keep-me"}', "sent": False}
        self.device._pending_updates["201"] = pending
        await self.receiver.async_close()
        self.assertIs(self.device._pending_updates["201"], pending)
        self.assertEqual(self.device.commands, [])

    async def test_probe_does_not_validate_self_reflection_after_direct_send(self):
        task = asyncio.create_task(self.receiver.async_probe(0.03))
        await asyncio.sleep(0.01)
        self.receiver._service_called(SimpleNamespace(data={
            "domain": "remote", "service": "send_command",
            "service_data": {"entity_id": "remote.ekf", "command": "known"}
        }))
        self.receiver.on_receive({"202": code()}, False)
        result = await task
        self.assertFalse(result["received"])
        self.assertFalse(result["validated"])

    async def test_rearm_waits_for_native_learn_lock(self):
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


if __name__ == "__main__":
    unittest.main()
