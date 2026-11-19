"""Kernel-native IR reception: no Tuya learning cycles, no duplicate dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


import asyncio
import base64
import os
import struct
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ekf_appliances.ir_decode import RemoteMapper
from custom_components.ekf_appliances.native_ir import NativeIrReceiver, scancode_frame
from custom_components.ekf_appliances.receiver import ReceiverRouter


def packet(scan, seconds, *, protocol=10, flags=0, keycode=0) -> bytes:
    return struct.pack(
        "=QHHIQ", int(seconds * 1_000_000_000), flags, protocol, keycode, scan
    )


def nec_code(address=0xBF00, command=0xE718) -> str:
    word = address | command << 16
    pulses = [9000, 4500]
    for bit in range(32):
        pulses.extend((560, 1690 if word & 1 << bit else 560))
    pulses.append(560)
    return (
        "b64:"
        + base64.b64encode(struct.pack("<" + "H" * len(pulses), *pulses)).decode()
    )


class KernelScancodes(unittest.TestCase):
    """Verify Linux scancode translation against the saved remote profile."""

    def test_literal_kernel_necx_vectors_reuse_original_esphome_profile(self) -> None:
        for scan, expected in (
            (0xBF18, "right"),
            (0xBF44, "volume_up"),
            (0xBF61, "dash"),
            (0xBA23, "apple_tv"),
            (0x070701, "samsung_ir_source"),
        ):
            with self.subTest(scan=hex(scan)):
                frame = scancode_frame(10, scan)
                self.assertEqual(RemoteMapper().feed([frame], 1), [expected])

    def test_unknown_protocol_or_invalid_width_is_not_a_nec_command(self) -> None:
        for proto, scan in ((1, 0xBF18), (11, 0xBF18), (10, -1), (10, 0x1000000)):
            self.assertIsNone(scancode_frame(proto, scan))


class NativeReception(unittest.IsolatedAsyncioTestCase):
    """Verify native capture, echo rejection and receiver lifecycle behavior."""

    async def asyncSetUp(self) -> None:
        self.loop = asyncio.get_running_loop()

        async def executor(fn, *args: Any) -> Any:
            return fn(*args)

        self.hass = SimpleNamespace(
            loop=self.loop,
            async_add_executor_job=executor,
            async_create_background_task=lambda task, _: asyncio.create_task(task),
        )
        self.reader = NativeIrReceiver(self.hass)
        self.events = []
        self.reader.add_callback(
            lambda name: self.events.append(name) if name else None
        )
        self.reader._accept_after_ns = 0

    async def asyncTearDown(self) -> None:
        await self.reader.async_close()

    def feed(self, scan, seconds, **kwargs: Any) -> None:
        with patch(
            "custom_components.ekf_appliances.native_ir.time.monotonic_ns",
            return_value=int(seconds * 1_000_000_000),
        ):
            self.reader.process_records(packet(scan, seconds, **kwargs))

    async def test_observer_validates_without_dispatch_and_activation_drops_old_input(
        self,
    ) -> None:
        self.feed(0xBF18, 10)
        self.assertTrue(self.reader.validated)
        self.assertEqual(self.events, [])
        self.reader.set_dispatch_enabled(enabled=True)
        cutoff = self.reader._accept_after_ns
        self.reader.process_records(packet(0xBF18, (cutoff - 1_000_000) / 1e9))
        self.assertEqual(self.events, [])
        self.feed(0xBF18, cutoff / 1e9 + 0.01)
        self.assertEqual(self.events, ["right"])

    async def test_kernel_timestamps_preserve_rapid_clicks_t9_and_hold_delete(
        self,
    ) -> None:
        self.reader.set_dispatch_enabled(enabled=True)
        self.reader._accept_after_ns = 0
        for scan, sec, flags in (
            (0xBF18, 10, 0),
            (0xBF18, 10.08, 0),
            (0xBF18, 10.16, 0),
            (0xBF02, 10.24, 0),
            (0xBF02, 10.32, 0),
            (0xBF02, 10.40, 0),
            (0xBF61, 11, 0),
            (0xBF61, 11.108, 2),
            (0xBF61, 11.216, 2),
        ):
            self.feed(scan, sec, flags=flags)
        self.assertEqual(
            self.events,
            ["right"] * 3
            + [
                "t9_add_2_a",
                "t9_cycle_2_b",
                "t9_cycle_2_c",
                "dash",
                "dash",
            ],
        )

    async def test_hold_and_repeat_feedback_identity_preserves_separate_fast_taps(
        self,
    ) -> None:
        self.reader.set_dispatch_enabled(enabled=True)
        self.reader._accept_after_ns = 0
        captures = []
        self.reader.add_callback(
            lambda name: (
                captures.append((name, self.reader.last_press_id)) if name else None
            )
        )
        for scan, sec, flags in (
            (0xBF15, 10, 0),
            (0xBF15, 10.108, 2),
            (0xBF15, 10.216, 2),
            (0xBF18, 11, 0),
            (0xBF18, 11.08, 0),
            (0xBF18, 11.30, 2),
        ):
            self.feed(scan, sec, flags=flags)
        self.assertEqual(
            [name for name, _ in captures], ["ok", "ok_hold", "right", "right", "right"]
        )
        self.assertEqual(captures[0][1], captures[1][1])
        self.assertNotEqual(captures[2][1], captures[3][1])
        self.assertEqual(captures[3][1], captures[4][1])

    async def test_unknown_frame_or_keyboard_mapping_cannot_repeat_previous_button(
        self,
    ) -> None:
        self.reader.set_dispatch_enabled(enabled=True)
        self.reader._accept_after_ns = 0
        self.feed(0xBF18, 10)
        self.feed(0xFF18, 10.1)
        self.feed(0xBF18, 10.3, flags=2)
        self.feed(0xBF44, 11, keycode=115)
        self.feed(0xBF44, 11.3, flags=2, keycode=115)
        self.assertEqual(self.events, ["right"])
        self.assertEqual(self.reader.diagnostics["kernel_key_conflicts"], 2)

    async def test_only_matching_ekf_echo_is_suppressed_without_muting_navigation(
        self,
    ) -> None:
        self.reader.set_dispatch_enabled(enabled=True)
        self.reader._accept_after_ns = 0
        with patch(
            "custom_components.ekf_appliances.native_ir.time.monotonic_ns",
            return_value=10_000_000_000,
        ):
            self.reader.note_transmit(nec_code(command=0xF20D))
        self.feed(0xBF0D, 10.01)
        self.feed(0xBF0D, 10.2, flags=2)
        self.feed(0xBF18, 10.02)
        self.feed(0xBF44, 10.03)
        self.assertEqual(self.events, ["right", "volume_up"])
        self.assertEqual(self.reader.diagnostics["suppressed_echo_count"], 2)

    async def test_stale_queued_kernel_events_are_not_replayed(self) -> None:
        self.reader.set_dispatch_enabled(enabled=True)
        self.reader._accept_after_ns = 0
        with patch(
            "custom_components.ekf_appliances.native_ir.time.monotonic_ns",
            return_value=12_000_000_000,
        ):
            self.reader.process_records(packet(0xBF0D, 10))
        self.assertEqual(self.events, [])
        self.assertFalse(self.reader.validated)

    async def test_fd_callback_closes_and_does_not_replay_on_reconnect(self) -> None:
        read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
        self.addCleanup(os.close, write_fd)
        with patch(
            "custom_components.ekf_appliances.native_ir.open_lirc_device",
            return_value=read_fd,
        ):
            await self.reader.async_start()
        self.reader.set_dispatch_enabled(enabled=True)
        os.write(write_fd, packet(0xBF18, time.monotonic()))
        await asyncio.sleep(0.01)
        self.assertEqual(self.events, ["right"])
        await self.reader.async_close()
        with self.assertRaises(OSError):
            os.fstat(read_fd)
        self.assertIsNone(self.reader._fd)
        self.assertIsNone(self.reader._retry_handle)

    async def test_timer_drain_consumes_ready_kernel_input_before_returning(
        self,
    ) -> None:
        read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
        self.addCleanup(os.close, write_fd)
        with patch(
            "custom_components.ekf_appliances.native_ir.open_lirc_device",
            return_value=read_fd,
        ):
            await self.reader.async_start()
        self.reader.set_dispatch_enabled(enabled=True)
        os.write(write_fd, packet(0xBF12, time.monotonic()))
        self.reader.drain_pending()
        self.assertEqual(self.events, ["input"])


class FakeBackend:
    """Provide an inert receiver with observable lifecycle calls."""

    def __init__(self, *, enabled=False) -> None:
        self.enabled = enabled
        self.validated = True
        self.last_error = self.last_received = self.last_press_id = None
        self.connected = True
        self.remote_id = "remote.ekf"
        self.callbacks = []
        self.mapper = RemoteMapper()
        self.diagnostics = {}
        self.async_load = AsyncMock()
        self.async_start = AsyncMock()
        self.async_close = AsyncMock()
        self.async_before_send = AsyncMock()
        self.async_probe = AsyncMock()
        self.note_transmit = Mock()
        self.set_dispatch_enabled = Mock(
            side_effect=lambda enabled: setattr(self, "enabled", enabled)
        )
        self.async_set_enabled = AsyncMock(side_effect=self.set_enabled)

    def set_enabled(self, *, enabled) -> None:
        self.enabled = enabled

    def add_callback(self, fn) -> Callable[[], None]:
        self.callbacks.append(fn)
        return lambda: self.callbacks.remove(fn)

    def emit(self, value) -> None:
        for fn in self.callbacks:
            fn(value)


class ReceiverSelection(unittest.IsolatedAsyncioTestCase):
    """Verify exclusive source selection and callback delivery."""

    async def test_service_observer_runs_on_actual_ha_event_loop(self) -> None:
        with tempfile.TemporaryDirectory() as path:
            hass = HomeAssistant(path)
            observed = []
            runtime = SimpleNamespace(
                hass=hass,
                entry=SimpleNamespace(
                    entry_id="thread-test", data={"remote_entity_id": "remote.ekf"}
                ),
                projector_off_script="script.projector_turn_off",
                observe_projector_ir=Mock(),
                note_projector_off_requested=lambda: observed.append(
                    threading.get_ident()
                ),
            )
            with (
                patch(
                    "custom_components.ekf_appliances.receiver.IrReceiver",
                    return_value=FakeBackend(),
                ),
                patch(
                    "custom_components.ekf_appliances.receiver.NativeIrReceiver",
                    return_value=FakeBackend(),
                ),
                patch(
                    "custom_components.ekf_appliances.receiver.Store",
                    return_value=self.store,
                ),
            ):
                router = ReceiverRouter(runtime)
            try:
                hass.bus.async_fire(
                    "call_service",
                    {
                        "domain": "script",
                        "service": "projector_turn_off",
                        "service_data": {},
                    },
                )
                await hass.async_block_till_done()
                self.assertEqual(observed, [threading.get_ident()])
            finally:
                await router.async_close()
                await hass.async_stop()

    async def asyncSetUp(self) -> None:
        self.ekf = FakeBackend(enabled=True)
        self.native = FakeBackend()
        self.store = SimpleNamespace(
            async_load=AsyncMock(return_value=None), async_save=AsyncMock()
        )
        self.hass = SimpleNamespace(
            bus=SimpleNamespace(async_listen=lambda *_: lambda: None)
        )
        self.runtime = SimpleNamespace(
            hass=self.hass,
            observe_projector_ir=Mock(),
            entry=SimpleNamespace(
                entry_id="test", data={"remote_entity_id": "remote.ekf"}
            ),
        )
        with (
            patch(
                "custom_components.ekf_appliances.receiver.IrReceiver",
                return_value=self.ekf,
            ),
            patch(
                "custom_components.ekf_appliances.receiver.NativeIrReceiver",
                return_value=self.native,
            ),
            patch(
                "custom_components.ekf_appliances.receiver.Store",
                return_value=self.store,
            ),
        ):
            self.router = ReceiverRouter(self.runtime)
        await self.router.async_load()
        await self.router.async_start()
        self.events = []
        self.router.add_callback(
            lambda name: self.events.append(name) if name else None
        )

    async def asyncTearDown(self) -> None:
        await self.router.async_close()

    async def test_existing_ekf_enable_migrates_and_observer_never_dispatches(
        self,
    ) -> None:
        self.assertEqual(self.router.source, "EKF")
        self.ekf.emit("right")
        self.native.emit("right")
        self.assertEqual(self.events, ["right"])
        self.assertTrue(self.router.enabled)

    async def test_trusted_capture_is_committed_before_public_event_callbacks(
        self,
    ) -> None:
        observed = []
        self.runtime.remote_controller = SimpleNamespace(
            physical_press=lambda name, at, **kwargs: observed.append(
                ("capture", name, at, kwargs.get("press_id"))
            )
        )
        self.router.add_callback(
            lambda name: observed.append(("event", name)) if name else None
        )
        await self.router.async_set_source("Odroid")
        self.native.last_received = 1001.0
        self.native.last_press_id = "input:10.000000000"
        self.ekf.emit("input")
        self.native.emit("input")
        self.assertEqual(
            observed,
            [
                ("capture", "input", 1001.0, "Odroid:input:10.000000000"),
                ("event", "input"),
            ],
        )

    async def test_switch_is_exclusive_resets_t9_and_is_reversible(self) -> None:
        self.ekf.mapper.feed(
            [{"repeat": False, "address": 0xBF00, "command": 0xFD02, "offset": 0}], 1
        )
        await self.router.async_set_source("Odroid")
        self.assertFalse(self.ekf.enabled)
        self.ekf.emit("power")
        self.native.emit("right")
        self.assertEqual(self.events, ["right"])
        await self.router.async_set_source("EKF")
        self.assertIsNone(self.ekf.mapper.digit)
        self.native.emit("power")
        self.ekf.emit("left")
        self.assertEqual(self.events, ["right", "left"])
        self.store.async_save.assert_awaited()

    async def test_disconnected_native_source_cannot_disable_working_ekf(self) -> None:
        for field in ("connected",):
            setattr(self.native, field, False)
            with self.assertRaises(HomeAssistantError):
                await self.router.async_set_source("Odroid")
            self.assertEqual(self.router.source, "EKF")
            self.assertTrue(self.ekf.enabled)
            setattr(self.native, field, True)

    async def test_explicit_connected_selection_does_not_falsely_validate_manual_reception(
        self,
    ) -> None:
        self.native.validated = False
        await self.router.async_set_source("Odroid")
        self.assertEqual(self.router.source, "Odroid")
        self.assertFalse(self.router.validated)

    async def test_chromecast_remote_service_never_suppresses_native_receiver(
        self,
    ) -> None:
        self.router._service_called(
            SimpleNamespace(
                data={
                    "domain": "remote",
                    "service": "send_command",
                    "service_data": {
                        "entity_id": "remote.televizor",
                        "command": "DPAD_RIGHT",
                    },
                }
            )
        )
        self.native.note_transmit.assert_not_called()
        self.router._service_called(
            SimpleNamespace(
                data={
                    "domain": "remote",
                    "service": "send_command",
                    "service_data": {"entity_id": "remote.ekf", "command": nec_code()},
                }
            )
        )
        self.native.note_transmit.assert_called_once()

    async def test_native_transmit_path_has_no_tuya_learning_lock(self) -> None:
        await self.router.async_set_source("Odroid")
        await self.router.async_before_send()
        self.ekf.async_before_send.assert_not_awaited()

    async def test_disabled_router_ignores_all_receivers(self) -> None:
        await self.router.async_set_enabled(enabled=False)
        self.ekf.emit("right")
        self.native.emit("right")
        self.assertEqual(self.events, [])

    async def test_store_failure_rolls_back_both_backends_without_leaving_tuya_learning(
        self,
    ) -> None:
        await self.router.async_set_source("Odroid")
        self.store.async_save.side_effect = OSError("Store unavailable")
        with self.assertRaises(OSError):
            await self.router.async_set_source("EKF")
        self.assertEqual(self.router.source, "Odroid")
        self.assertTrue(self.native.enabled)
        self.assertFalse(self.ekf.enabled)
        self.ekf.emit("power")
        self.native.emit("right")
        self.assertEqual(self.events, ["right"])

    async def test_close_during_source_save_cannot_reenable_old_receiver(self) -> None:
        await self.router.async_set_source("Odroid")
        entered, proceed = asyncio.Event(), asyncio.Event()

        async def save(_data) -> None:
            entered.set()
            await proceed.wait()
            error_message = "Store unavailable"
            raise OSError(error_message)

        self.store.async_save.side_effect = save
        switch = asyncio.create_task(self.router.async_set_source("EKF"))
        await entered.wait()
        await self.router.async_close()
        self.native.set_dispatch_enabled.reset_mock()
        self.ekf.async_set_enabled.reset_mock()
        proceed.set()
        with self.assertRaises(OSError):
            await switch
        self.assertFalse(
            any(
                call.args == (True,)
                for call in self.native.set_dispatch_enabled.call_args_list
            )
        )
        self.assertFalse(
            any(
                call.args == (True,)
                for call in self.ekf.async_set_enabled.call_args_list
            )
        )
        self.ekf.emit("power")
        self.native.emit("power")
        self.assertEqual(self.events, [])
