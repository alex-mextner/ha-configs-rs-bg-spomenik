"""Kernel capture timing and OEM observation without hardware or an HA runtime."""

from __future__ import annotations

import importlib
import struct
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

PACKAGE = "native_mode_observation_test"
package = ModuleType(PACKAGE)
package.__path__ = [
    str(Path(__file__).resolve().parents[2] / "custom_components/ekf_appliances")
]
exceptions = ModuleType("homeassistant.exceptions")
exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
with patch.dict(
    sys.modules, {PACKAGE: package, "homeassistant.exceptions": exceptions}
):
    native = importlib.import_module(PACKAGE + ".native_ir")


def packet(scan, seconds, flags=0) -> bytes:
    return struct.pack("=QHHIQ", int(seconds * 1e9), flags, 10, 0, scan)


class NativeModeObservationTests(unittest.TestCase):
    """Verify physical timestamps, protected bursts and OEM power observation."""

    def setUp(self) -> None:
        self.reader = native.NativeIrReceiver(SimpleNamespace())
        self.reader.enabled = True
        self.events = []
        self.frames = []
        self.reader.add_callback(
            lambda name: (
                self.events.append((name, self.reader.last_received)) if name else None
            )
        )
        self.reader.frame_observer = lambda frame, stamp: self.frames.append(
            (frame, stamp)
        )

    def feed(self, payload, received=10.8) -> None:
        with (
            patch.object(native.time, "monotonic_ns", return_value=int(received * 1e9)),
            patch.object(native.time, "time", return_value=1000.8),
        ):
            self.reader.process_records(payload)

    def test_buffered_double_keeps_two_physical_timestamps(self) -> None:
        self.feed(packet(0xBF12, 10.0) + packet(0xBF12, 10.15))
        self.assertEqual([name for name, stamp in self.events], ["input", "input"])
        self.assertAlmostEqual(self.events[1][1] - self.events[0][1], 0.150)
        self.assertAlmostEqual(self.events[0][1], 1000.0)

    def test_oem_projector_power_is_observed_before_universal_filter(self) -> None:
        self.feed(packet(0xBD01, 10.2))
        self.assertEqual(len(self.frames), 1)
        self.assertEqual(self.frames[0][0]["address"], 0xBD00)
        self.assertEqual(self.frames[0][0]["command"], 0xFE01)
        self.assertEqual(self.events, [])

    def test_outgoing_projector_echo_is_not_a_physical_off_request(self) -> None:
        self.reader._echo_until[(0xBD00, 0xFE01)] = 11_000_000_000
        self.feed(packet(0xBD01, 10.2))
        self.assertEqual(self.frames, [])

    def test_stale_oem_capture_is_not_an_off_request(self) -> None:
        self.feed(packet(0xBD01, 8))
        self.assertEqual(self.frames, [])

    def test_unrelated_frame_does_not_create_silence_in_a_held_input(self) -> None:
        held = b"".join(packet(0xBF12, 10 + index * 0.110) for index in range(7))
        self.feed(held + packet(0xFF18, 10.70) + packet(0xBF12, 10.77))
        self.assertEqual([name for name, stamp in self.events], ["input"])
        self.feed(packet(0xBF12, 10.92), received=10.92)
        self.assertEqual([name for name, stamp in self.events], ["input", "input"])

    def test_own_ir_echo_does_not_create_another_held_mode_press(self) -> None:
        self.reader._echo_until[(0xBD00, 0xFE01)] = 11_000_000_000
        held = b"".join(packet(0xBF12, 10 + index * 0.110) for index in range(7))
        self.feed(held + packet(0xBD01, 10.70) + packet(0xBF12, 10.77))
        self.assertEqual([name for name, stamp in self.events], ["input"])

    def test_own_ir_echo_does_not_retrigger_held_power_or_prime(self) -> None:
        for scan, expected in ((0xBF0D, ["power"]), (0xBFBB, ["prime", "prime_hold"])):
            with self.subTest(scan=scan):
                self.setUp()
                self.reader._echo_until[(0xBD00, 0xFE01)] = 11_000_000_000
                held = b"".join(packet(scan, 10 + index * 0.110) for index in range(7))
                self.feed(held + packet(0xBD01, 10.70) + packet(scan, 10.77))
                self.assertEqual([name for name, stamp in self.events], expected)
                self.feed(packet(scan, 10.92), received=10.92)
                self.assertEqual(
                    [name for name, stamp in self.events], [*expected, expected[0]]
                )

    def test_protected_press_still_ends_the_previous_t9_cycle(self) -> None:
        self.feed(packet(0xBF02, 10.0) + packet(0xBFBB, 10.1) + packet(0xBF02, 10.2))
        self.assertEqual(
            [name for name, stamp in self.events], ["t9_add_2_a", "prime", "t9_add_2_a"]
        )

    def test_orphan_repeat_preserves_power_silence_without_a_new_press(self) -> None:
        self.feed(
            packet(0xBF0D, 10.0)
            + packet(0xFF18, 10.05)
            + packet(0xBF0D, 10.11, flags=2)
            + packet(0xBF0D, 10.22)
        )
        self.assertEqual([name for name, stamp in self.events], ["power"])

    def test_orphan_repeat_still_updates_mode_button_physical_silence(self) -> None:
        held = b"".join(packet(0xBF12, 10 + index * 0.110) for index in range(6))
        self.feed(
            held
            + packet(0xFF18, 10.60)
            + packet(0xBF12, 10.66, flags=2)
            + packet(0xBF12, 10.77)
        )
        self.assertEqual([name for name, stamp in self.events], ["input"])


if __name__ == "__main__":
    unittest.main()
