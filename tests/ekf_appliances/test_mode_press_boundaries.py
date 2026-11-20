"""Physical INPUT boundaries, independent of Home Assistant and wall-clock lag."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[2]
    / "custom_components/ekf_appliances/ir_decode.py"
)
SPEC = importlib.util.spec_from_file_location("mode_press_decoder", SOURCE)
decoder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(decoder)


def frame(command=0xED12, *, repeat=False, offset=0) -> dict[str, int | float | bool]:
    return {"address": 0xBF00, "command": command, "repeat": repeat, "offset": offset}


class ModePressBoundaries(unittest.TestCase):
    """Verify physical silence and full-frame held-button boundaries."""

    def test_full_frame_hold_has_one_input_and_requires_release_gap(self) -> None:
        mapper = decoder.RemoteMapper()
        events = []
        for index in range(30):
            events += mapper.feed([frame()], 10 + index * 0.110)
        self.assertEqual(events, ["input"])
        self.assertEqual(mapper.feed([frame()], 10 + 29 * 0.110 + 0.150), ["input"])

    def test_nec_repeat_updates_silence_boundary_before_next_full_input(self) -> None:
        mapper = decoder.RemoteMapper()
        self.assertEqual(mapper.feed([frame()], 10), ["input"])
        self.assertEqual(mapper.feed([frame(repeat=True)], 10.108), [])
        self.assertEqual(mapper.feed([frame()], 10.216), [])
        self.assertEqual(mapper.feed([frame()], 10.366), ["input"])

    def test_two_full_inputs_at_150ms_are_distinct_and_publish_capture_times(
        self,
    ) -> None:
        mapper = decoder.RemoteMapper()
        self.assertEqual(
            mapper.feed([frame(), frame(offset=0.150)], 10.150), ["input", "input"]
        )
        self.assertEqual([event[1] for event in mapper.event_times], [10, 10.150])

    def test_zero_and_149ms_without_release_do_not_create_second_input(self) -> None:
        for gap in (0, 0.100, 0.149):
            with self.subTest(gap=gap):
                mapper = decoder.RemoteMapper()
                self.assertEqual(mapper.feed([frame()], 10), ["input"])
                self.assertEqual(mapper.feed([frame()], 10 + gap), [])

    def test_full_frame_audio_hold_does_not_activate_double_audio(self) -> None:
        mapper = decoder.RemoteMapper()
        events = []
        for index in range(12):
            events += mapper.feed([frame(0x9966)], 10 + index * 0.108)
        self.assertEqual(events, ["audio"])

    def test_full_frame_power_hold_emits_one_power_until_release(self) -> None:
        mapper = decoder.RemoteMapper()
        events = []
        for index in range(12):
            events += mapper.feed([frame(0xF20D)], 10 + index * 0.110)
        self.assertEqual(events, ["power"])
        self.assertEqual(
            mapper.feed([frame(0xF20D)], 10 + 11 * 0.110 + 0.150), ["power"]
        )

    def test_full_frames_preserve_single_press_and_one_hold_action(self) -> None:
        for command, name in ((0x44BB, "prime"), (0x956A, "disney"), (0xEA15, "ok")):
            with self.subTest(name=name):
                mapper = decoder.RemoteMapper()
                events = []
                for index in range(12):
                    events += mapper.feed([frame(command)], 10 + index * 0.110)
                self.assertEqual(events, [name, name + "_hold"])
                self.assertEqual(
                    mapper.feed([frame(command)], 10 + 11 * 0.110 + 0.150), [name]
                )

    def test_feedback_identity_follows_logical_presses_without_merging_fast_taps(
        self,
    ) -> None:
        mapper = decoder.RemoteMapper()
        events = mapper.feed(
            [
                frame(0xEA15),
                frame(repeat=True, offset=0.110),
                frame(repeat=True, offset=0.220),
                frame(0xE718, offset=0.370),
                frame(0xE718, offset=0.450),
                frame(repeat=True, offset=0.670),
                frame(0xFD02, offset=0.800),
                frame(0xFD02, offset=0.880),
            ],
            10.880,
        )
        self.assertEqual(
            events,
            ["ok", "ok_hold", "right", "right", "right", "t9_add_2_a", "t9_cycle_2_b"],
        )
        identities = mapper.event_press_ids
        self.assertEqual(identities[0], identities[1])
        self.assertNotEqual(identities[2], identities[3])
        self.assertEqual(identities[3], identities[4])
        self.assertNotEqual(identities[5], identities[6])
        self.assertEqual(len(set(identities)), 5)

    def test_fast_navigation_and_t9_keep_existing_behavior(self) -> None:
        mapper = decoder.RemoteMapper()
        self.assertEqual(mapper.feed([frame(0xE718)], 10), ["right"])
        self.assertEqual(mapper.feed([frame(0xE718)], 10.080), ["right"])
        self.assertEqual(mapper.feed([frame(0xFD02)], 10.160), ["t9_add_2_a"])
        self.assertEqual(mapper.feed([frame(0xFD02)], 10.240), ["t9_cycle_2_b"])


if __name__ == "__main__":
    unittest.main()
