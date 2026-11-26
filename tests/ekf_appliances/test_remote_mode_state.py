"""Deterministic remote mode contracts; no HA installation or hardware needed."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any

PATH = (
    Path(__file__).resolve().parents[2]
    / "custom_components/ekf_appliances/remote_mode.py"
)
SPEC = importlib.util.spec_from_file_location("remote_mode_under_test", PATH)
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class Clock:
    """Schedule deterministic cancellable callbacks against a controlled clock."""

    def __init__(self) -> None:
        self.now = 1000.977
        self.tasks = []

    def schedule(self, delay, callback) -> Any:
        task = [self.now + delay, callback, False]
        self.tasks.append(task)

        class Handle:
            def cancel(self) -> None:
                task[2] = True

        return Handle()

    def advance(self, seconds) -> None:
        self.now += seconds
        while tasks := [
            task for task in self.tasks if not task[2] and task[0] <= self.now
        ]:
            task = min(tasks, key=lambda item: item[0])
            task[2] = True
            task[1]()


class RemoteModeTests(unittest.TestCase):
    """Verify mode, power and lease transitions at exact physical boundaries."""

    def setUp(self) -> None:
        self.clock = Clock()
        self.targets = []
        self.mode = module.RemoteMode(
            clock=lambda: self.clock.now,
            schedule=self.clock.schedule,
            changed=lambda: self.targets.append(self.mode.target),
        )
        self.observe(tv=False, projector=False)

    def observe(
        self,
        tv,
        projector,
        *,
        shutdown=False,
        audio=False,
        **power: Any,
    ) -> None:
        self.mode.observe(
            module.PowerObservations(
                tv=tv, projector=projector, shutting_down=shutdown, **power
            ),
            audio=audio,
        )

    def input(self, offset=0) -> dict[str, str | bool]:
        self.clock.advance(offset)
        return self.mode.press("input", self.clock.now)

    def double(self, gap=0.150) -> None:
        self.input()
        self.input(gap)

    def test_default_matrix_and_unknown_are_not_off(self) -> None:
        for tv, projector, target in [
            (False, False, "chromecast"),
            (False, True, "kodi"),
            (True, False, "chromecast"),
            (True, True, "chromecast"),
            (None, True, "chromecast"),
            (False, None, "chromecast"),
        ]:
            self.observe(tv, projector)
            self.assertEqual(self.mode.target, target)

    def test_single_switches_group_without_native_or_wake_when_both_off(self) -> None:
        self.input()
        self.assertEqual(self.mode.target, "chromecast")
        self.clock.advance(1.011)
        self.assertEqual(self.mode.target, "kodi")
        self.assertFalse(self.mode.native)

    def test_double_uses_original_pair_including_inclusive_1000ms(self) -> None:
        for gap in (0.150, 0.999, 1.000):
            self.mode.select("tv")
            self.double(gap)
            self.assertEqual(self.mode.target, "tv")
            self.clock.advance(1.1)
            self.assertEqual(self.mode.target, "tv")

    def test_projector_double_toggles_back_to_kodi(self) -> None:
        self.observe(tv=False, projector=True)
        self.double()
        self.assertEqual(self.mode.target, "projector")
        self.clock.advance(1.1)
        self.double()
        self.assertEqual(self.mode.target, "kodi")

    def test_duplicate_input_before_release_never_doubles(self) -> None:
        self.input()
        self.input(0.149)
        self.assertEqual(self.mode.target, "chromecast")
        self.clock.advance(1)
        self.assertEqual(self.mode.target, "kodi")

    def test_epoch_capture_conversion_preserves_inclusive_150ms_double(self) -> None:
        self.clock.now = received = 1794771418.8
        self.mode.press("input", received - 0.8)
        self.mode.press("input", received - 0.65)
        self.assertEqual(self.mode.target, "tv")

    def test_after_1000ms_is_another_single(self) -> None:
        self.input()
        self.input(1.001)
        self.assertEqual(self.mode.target, "kodi")
        self.clock.advance(1.011)
        self.assertEqual(self.mode.target, "chromecast")

    def test_first_press_sets_single_deadline_not_timeout_processing_time(self) -> None:
        first = self.clock.now
        self.input()
        self.clock.advance(1.011)
        self.assertAlmostEqual(self.mode.expires_at, first + 60)

    def test_input_selecting_tv_from_running_projector_never_wakes_tv(self) -> None:
        self.observe(tv=False, projector=True)
        self.input()
        self.clock.advance(1.011)
        self.assertEqual(self.mode.target, "chromecast")
        self.double()
        self.assertEqual(self.mode.target, "tv")

    def test_single_selection_keeps_exact_60_seconds(self) -> None:
        self.observe(
            tv=True, projector=False, revisions={"tv": "t:1", "projector": "p:1"}
        )
        first = self.clock.now
        self.input()
        self.clock.advance(1.011)
        self.assertAlmostEqual(self.mode.expires_at, first + 60)
        self.clock.advance(60 - 1.011)
        self.assertEqual(self.mode.target, "chromecast")

    def check_single_input_after_audio_expiry(
        self, *, observed: bool, from_projector: bool
    ) -> None:
        self.observe(
            tv=not from_projector,
            projector=from_projector,
            revisions={"tv": "t:1", "projector": "p:1"},
        )
        self.mode.set_audio(active=True, at=self.clock.now - 59.5)
        received = self.clock.now
        self.input()
        self.assertTrue(self.mode.audio)
        self.assertIsNotNone(self.mode.pending)

        if observed:
            # Refresh may observe expiry before the scheduled audio timer runs.
            self.clock.now += 0.501
            self.observe(tv=not from_projector, projector=from_projector, audio=True)
        else:
            self.clock.advance(0.501)
        self.assertFalse(self.mode.audio)
        self.assertIsNotNone(self.mode.pending)

        self.clock.advance(0.510)
        self.assertIsNone(self.mode.pending)
        self.assertEqual(
            (
                self.mode.pair,
                self.mode.target,
                self.mode.native,
                self.mode.expires_at,
            ),
            (
                "tv" if from_projector else "projector",
                "chromecast" if from_projector else "kodi",
                False,
                received + 60,
            ),
        )

    def test_single_input_after_audio_timer_expiry_selects_off_projector(
        self,
    ) -> None:
        self.check_single_input_after_audio_expiry(observed=False, from_projector=False)

    def test_single_input_after_observed_audio_expiry_selects_off_projector(
        self,
    ) -> None:
        self.check_single_input_after_audio_expiry(observed=True, from_projector=False)

    def test_single_input_after_audio_timer_expiry_selects_off_tv_without_wake(
        self,
    ) -> None:
        self.check_single_input_after_audio_expiry(observed=False, from_projector=True)

    def test_single_input_after_observed_audio_expiry_selects_off_tv_without_wake(
        self,
    ) -> None:
        self.check_single_input_after_audio_expiry(observed=True, from_projector=True)

    def test_native_expires_proactively_at_fractional_deadline(self) -> None:
        self.observe(tv=False, projector=True)
        self.double()
        self.clock.advance(59.999)
        self.assertEqual(self.mode.target, "projector")
        self.clock.advance(0.001)
        self.assertEqual(self.mode.target, "kodi")
        self.assertFalse(self.mode.native)

    def test_old_timer_cannot_expire_a_refreshed_lease(self) -> None:
        self.mode.select("projector", native=True)
        old_callback = self.clock.tasks[-1][1]
        self.clock.advance(30)
        last = self.clock.now
        self.mode.press("up", last)
        self.clock.advance(30)
        old_callback()
        self.assertEqual(self.mode.target, "projector")
        self.assertEqual(self.mode.expires_at, last + 60)

    def test_every_accepted_universal_key_renews_current_mode(self) -> None:
        self.mode.select("projector", native=True)
        self.clock.advance(30)
        self.mode.press("channel_up", self.clock.now)
        self.assertEqual(self.mode.expires_at, self.clock.now + 60)

    def test_action_captured_before_expiry_renews_when_processing_is_late(self) -> None:
        self.mode.select("projector", native=True)
        deadline = self.mode.expires_at
        self.clock.now = deadline + 0.2
        route = self.mode.press("up", deadline - 0.1)
        self.assertEqual(route, {"pair": "projector", "native": True})
        self.assertAlmostEqual(self.mode.expires_at, deadline - 0.1 + 60)

    def test_selected_on_to_off_expires_even_when_other_is_off(self) -> None:
        self.observe(tv=False, projector=True)
        self.double()
        self.observe(tv=False, projector=False)
        self.assertEqual(self.mode.target, "chromecast")
        self.assertEqual(self.mode.expires_at, 0)

    def test_selecting_already_off_group_is_preserved(self) -> None:
        self.mode.select("projector", native=True)
        self.observe(tv=False, projector=False)
        self.assertEqual(self.mode.target, "projector")
        self.clock.advance(60)
        self.assertEqual(self.mode.target, "chromecast")

    def test_off_after_first_input_cancels_pending_single_and_double(self) -> None:
        self.observe(
            tv=True, projector=True, revisions={"tv": "t:1", "projector": "p:1"}
        )
        self.input()
        self.observe(
            tv=True,
            projector=False,
            revisions={"tv": "t:1", "projector": "p:2"},
            intents={"projector": False},
        )
        self.clock.advance(1.1)
        self.assertEqual(self.mode.target, "chromecast")

    def test_shutdown_forces_tv_and_blocks_manual_and_stale_route(self) -> None:
        self.observe(tv=False, projector=True)
        self.mode.select("projector", native=True)
        self.observe(tv=False, projector=True, shutdown=True)
        self.assertEqual(self.mode.target, "chromecast")
        self.assertFalse(self.mode.native)
        self.mode.select("projector", native=True)
        self.assertEqual(self.mode.target, "chromecast")
        self.assertEqual(
            self.mode.resolve("projector", native=True), {"pair": "tv", "native": False}
        )

    def test_shutdown_invalidates_pending_input_snapshot(self) -> None:
        self.observe(tv=False, projector=True)
        self.input()
        self.observe(tv=False, projector=True, shutdown=True)
        self.clock.advance(1.1)
        self.assertEqual(self.mode.target, "chromecast")

    def test_fresh_tv_double_still_works_while_projector_shuts_down(self) -> None:
        self.observe(tv=True, projector=True, shutdown=True)
        self.input()
        self.observe(tv=True, projector=True, shutdown=True)
        self.input(0.150)
        self.assertEqual(self.mode.target, "tv")
        self.clock.advance(1.1)
        self.input()
        self.clock.advance(1.011)
        self.assertEqual(self.mode.target, "chromecast")

    def test_atomic_target_has_no_intermediate_projector_on_single(self) -> None:
        self.mode.select("tv", native=True)
        self.targets.clear()
        self.input()
        self.clock.advance(1.011)
        self.assertEqual([value for value in self.targets if value != "tv"], ["kodi"])

    def test_speaker_is_effective_target_and_returns_to_current_base(self) -> None:
        self.observe(tv=False, projector=True, audio=True)
        self.assertEqual(self.mode.target, "speaker")
        self.observe(tv=False, projector=True, audio=False)
        self.assertEqual(self.mode.target, "kodi")

    def test_following_key_flushes_single_and_renews_from_that_physical_action(
        self,
    ) -> None:
        self.input()
        self.clock.advance(0.2)
        result = self.mode.press("up", self.clock.now)
        self.assertEqual(result, {"pair": "projector", "native": False})
        self.assertEqual(self.mode.expires_at, self.clock.now + 60)
        self.clock.advance(1)
        self.assertEqual(self.mode.target, "kodi")


if __name__ == "__main__":
    unittest.main()
