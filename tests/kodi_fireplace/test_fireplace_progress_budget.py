"""Share the launch deadline while allowing a cold HD stream to start."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import test_rotation as fixtures


class ProgressBudgetCases(fixtures.FixtureBase):
    """Exercise real opens, proof reads, cleanup and cancellation with a clock."""

    multi = fixtures.RotationCases.multi

    def setUp(self) -> None:
        super().setUp()
        self.now = self.native_start = 100.0
        self.delays: dict[str, float | None] = {}
        self.deadlines: list[tuple[str, float, float]] = []
        clock = SimpleNamespace(monotonic=lambda: self.now)
        for module in (fixtures.fireplace, fixtures.fireplace._rotation):
            clock_patch = patch.object(module, "time", clock)
            clock_patch.start()
            self.addCleanup(clock_patch.stop)
        self.native_rpc = self.kodi.rpc
        self.kodi.rpc = self.clocked_rpc

    def clocked_rpc(self, method, params, deadline) -> Any:
        self.deadlines.append((method, self.now, deadline))
        result = self.native_rpc(method, params, deadline)
        if method == "Player.Open":
            self.native_start = self.now
        if method == "Player.GetProperties":
            name = Path(self.kodi.file).name if self.kodi.file else ""
            delay = self.delays.get(name, 0.0)
            elapsed = (
                0.0 if delay is None else max(0.0, self.now - self.native_start - delay)
            )
            result["time"] = {
                "hours": 0,
                "minutes": 0,
                "seconds": int(elapsed),
                "milliseconds": round(elapsed % 1 * 1000),
            }
        return result

    def advance_wait(self, _timeout: float) -> bool:
        self.now += 0.5
        return False

    def test_cold_video_progress_after_three_seconds_is_not_stopped(self) -> None:
        self.delays = {"0.webm": 4.0}
        controller = self.multi(operation_timeout=50, progress_interval=0.25)
        with patch.object(controller._closing, "wait", side_effect=self.advance_wait):
            proof = controller.start("cold-delayed-progress")
        self.assertEqual("playing", proof["state"])
        self.assertEqual("local-0", proof["source_id"])
        self.assertTrue(proof["owned"] and proof["progressing"])
        self.assertGreater(self.now - 100.0, 3.0)
        self.assertLess(self.now - 100.0, 50.0)
        self.assertEqual(
            1, sum(method == "Player.Open" for method, _ in self.kodi.calls)
        )
        self.assertFalse(any(method == "Player.Stop" for method, _ in self.kodi.calls))

    def test_stalled_first_source_leaves_budget_for_delayed_second(self) -> None:
        self.delays = {"0.webm": None, "1.webm": 4.0}
        controller = self.multi(operation_timeout=50, progress_interval=0.25)
        with patch.object(controller._closing, "wait", side_effect=self.advance_wait):
            proof = controller.start("fallback-with-progress-budget")
        self.assertEqual("playing", proof["state"])
        self.assertEqual("local-1", proof["source_id"])
        self.assertTrue(proof["owned"] and proof["progressing"])
        opened = [
            Path(params["item"]["file"]).name
            for method, params in self.kodi.calls
            if method == "Player.Open"
        ]
        self.assertEqual(["0.webm", "1.webm"], opened)
        self.assertEqual(
            1, sum(method == "Player.Stop" for method, _ in self.kodi.calls)
        )
        self.assertLess(self.now, 150.0)

    def test_final_candidate_uses_remaining_global_progress_budget(self) -> None:
        controller = self.multi(operation_timeout=50, progress_interval=0.25)
        with patch.object(controller._closing, "wait", side_effect=self.advance_wait):
            first = controller.start("seed-previous-success")
            self.assertEqual("local-0", first["source_id"])
            controller.stop()
            self.kodi.calls.clear()
            self.deadlines.clear()
            self.delays = {"1.webm": None, "2.webm": 14.0}
            started = self.now
            try:
                proof = controller.start("last-candidate-cold-progress")
            except fixtures.fireplace.FireplaceError as error:
                self.fail(
                    "Final cold source was stopped before its global deadline: "
                    + str(error)
                )
        self.assertEqual("playing", proof["state"])
        self.assertEqual("local-2", proof["source_id"])
        self.assertTrue(proof["owned"] and proof["progressing"])
        opened = [
            Path(params["item"]["file"]).name
            for method, params in self.kodi.calls
            if method == "Player.Open"
        ]
        self.assertEqual(["1.webm", "2.webm"], opened)
        self.assertEqual(
            1, sum(method == "Player.Stop" for method, _ in self.kodi.calls)
        )
        last_open = [
            observed
            for method, observed, _ in self.deadlines
            if method == "Player.Open"
        ][-1]
        self.assertGreater(self.now - last_open, (started + 50.0 - last_open) / 2)
        self.assertLess(self.now, started + 50.0)

    def test_only_eligible_source_uses_remaining_global_progress_budget(self) -> None:
        self.delays = {"one.webm": 30.0}
        controller = self.multi(
            sources=[
                {
                    "id": "local-only",
                    "kind": "local",
                    "path": str(self.media),
                    "sha256": self.sha,
                    "title": "one.webm",
                }
            ],
            operation_timeout=50,
            progress_interval=0.25,
        )
        with patch.object(controller._closing, "wait", side_effect=self.advance_wait):
            try:
                proof = controller.start("only-candidate-cold-progress")
            except fixtures.fireplace.FireplaceError as error:
                self.fail(
                    "Only cold source was stopped before its global deadline: "
                    + str(error)
                )
        self.assertEqual("playing", proof["state"])
        self.assertEqual("local-only", proof["source_id"])
        self.assertTrue(proof["owned"] and proof["progressing"])
        self.assertGreater(self.now, 125.0)
        self.assertLess(self.now, 150.0)
        self.assertEqual(
            1, sum(method == "Player.Open" for method, _ in self.kodi.calls)
        )
        self.assertFalse(any(method == "Player.Stop" for method, _ in self.kodi.calls))

    def test_stalled_candidates_remain_inside_global_deadline(self) -> None:
        self.delays = dict.fromkeys(("0.webm", "1.webm", "2.webm"))
        controller = self.multi(operation_timeout=8)
        with (
            patch.object(controller._closing, "wait", side_effect=self.advance_wait),
            self.assertRaisesRegex(
                fixtures.fireplace.FireplaceError, "sources_unavailable"
            ),
        ):
            controller.start("bounded-stalls")
        self.assertLessEqual(self.now, 108.0)
        self.assertEqual(
            2, sum(method == "Player.Open" for method, _ in self.kodi.calls)
        )
        for method, observed, deadline in self.deadlines:
            if method == "Player.Open":
                self.assertLess(observed, 108.0)
                self.assertLessEqual(deadline, 108.0)
            elif method == "Player.Stop":
                self.assertLessEqual(deadline, 116.0)
        self.assertIsNone(self.kodi.file)

    def test_new_stop_cancels_during_extended_progress_wait(self) -> None:
        self.delays = {"0.webm": 10.0, "1.webm": 10.0}
        controller = self.multi(operation_timeout=50, progress_interval=0.25)
        stopped, errors = [], []
        stopper = None

        def stop() -> None:
            try:
                stopped.append(controller.stop())
            # Worker failures are asserted after its guaranteed join.
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        def cancel_after_three_seconds(timeout: float) -> bool:
            nonlocal stopper
            result = self.advance_wait(timeout)
            if self.now >= 103.5 and stopper is None:
                stopper = threading.Thread(target=stop)
                stopper.start()
                self.assertTrue(controller._cancelled.wait(1))
            return result

        try:
            with (
                patch.object(
                    controller._closing, "wait", side_effect=cancel_after_three_seconds
                ),
                self.assertRaisesRegex(fixtures.fireplace.FireplaceError, "cancelled"),
            ):
                controller.start("cancel-delayed-progress")
        finally:
            if stopper is not None:
                stopper.join(timeout=3)
        self.assertIsNotNone(stopper)
        self.assertFalse(stopper.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, len(stopped))
        self.assertEqual("idle", stopped[0]["state"])
        self.assertFalse(stopped[0]["owned"] or stopped[0]["progressing"])
        self.assertEqual(
            1, sum(method == "Player.Open" for method, _ in self.kodi.calls)
        )
        self.assertLess(self.now, 150.0)
        self.assertIsNone(self.kodi.file)
