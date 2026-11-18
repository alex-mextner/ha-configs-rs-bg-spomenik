"""Late native progress must settle only the still-current fireplace intent."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import test_rotation as fixtures
import test_rotation_recovery as recovery


class LatePromotionCases(fixtures.FixtureBase):
    """Use the real timeout, repeat owner and monitor settlement paths."""

    multi = fixtures.RotationCases.multi
    selection = recovery.RecoveryCases.selection
    generation = recovery.RecoveryCases.generation
    isolated = recovery.RecoveryCases.isolated

    def setUp(self) -> None:
        super().setUp()
        self.now = self.native_start = 100.0
        clock = SimpleNamespace(monotonic=lambda: self.now)
        for module in (fixtures.fireplace, fixtures.fireplace._rotation):
            clock_patch = patch.object(module, "time", clock)
            clock_patch.start()
            self.addCleanup(clock_patch.stop)
        self.native_rpc = self.kodi.rpc
        self.kodi.rpc = self.clocked_rpc
        self.reject_stops = False

    def clocked_rpc(self, method, params, deadline) -> Any:
        result = self.native_rpc(method, params, deadline)
        if method == "Player.Open":
            self.native_start = self.now
        if method == "Player.GetProperties":
            elapsed = self.now - self.native_start
            result["time"] = {
                "hours": 0,
                "minutes": 0,
                "seconds": int(elapsed),
                "milliseconds": round(elapsed % 1 * 1000),
            }
        return result

    def expired_wait(self, _timeout: float) -> bool:
        self.now += 4.0
        return False

    def stalled(self) -> Any:
        controller = self.multi(operation_timeout=8)
        native = controller._rpc

        def unavailable_stop(method, params, deadline) -> Any:
            if method == "Player.Stop" and self.reject_stops:
                message = "read_pressure_before_stop_dispatch"
                raise fixtures.fireplace.RpcNotDispatchedError(message)
            return native(method, params, deadline)

        controller._rpc = unavailable_stop
        self.reject_stops = True
        with (
            patch.object(controller._closing, "wait", side_effect=self.expired_wait),
            self.assertRaisesRegex(
                fixtures.fireplace.FireplaceError, "stop_unconfirmed"
            ),
        ):
            controller.start("late-current")
        self.reject_stops = False
        self.assertEqual("unknown", controller.status()["state"])
        self.assertEqual("stop_unconfirmed", controller.status()["error"])
        self.assertEqual("opening", self.selection()["pending"]["phase"])
        self.assertNotEqual(
            self.selection()["pending"]["attempt_id"],
            self.generation()["request_id"],
        )
        self.assertTrue(controller._owner.status()["owned"])
        return controller

    def advance(self, controller) -> dict[str, Any]:
        self.now += 0.5
        return controller.observe()

    def test_late_current_progress_clears_failure_and_restores_exact_generation(
        self,
    ) -> None:
        controller = self.stalled()
        pending = dict(self.selection()["pending"])
        before = list(self.kodi.calls)
        proof = self.advance(controller)
        self.assertEqual("playing", proof["state"])
        self.assertTrue(proof["owned"] and proof["progressing"])
        self.assertIsNone(proof["error"])
        self.assertEqual("late-current", proof["request_id"])
        self.assertEqual(pending["source_id"], proof["source_id"])
        state = self.selection()
        self.assertIsNone(state["pending"])
        self.assertEqual(pending["attempt_id"], state["active"]["attempt_id"])
        self.assertEqual(pending["source_id"], state["last_successful_source_id"])
        self.assertEqual(pending["attempt_id"], self.generation()["request_id"])
        self.assertFalse(self.generation()["cancelled"])
        self.assertEqual(1, self.generation()["intent_seq"])
        actors = {"Player.Open", "Player.Stop", "Player.SetRepeat"}
        self.assertFalse(
            [call for call in self.kodi.calls[len(before) :] if call[0] in actors]
        )

    def test_recovered_success_survives_observation_and_bridge_restart(self) -> None:
        controller = self.stalled()
        attempt = self.selection()["pending"]["attempt_id"]
        self.assertEqual("playing", self.advance(controller)["state"])
        self.assertEqual("playing", self.advance(controller)["state"])
        controller.close()
        reopened = self.multi()
        self.advance(reopened)
        proof = self.advance(reopened)
        self.assertEqual("playing", proof["state"])
        self.assertEqual("late-current", proof["request_id"])
        self.assertEqual(attempt, self.generation()["request_id"])
        self.assertEqual("local-0", self.selection()["last_successful_source_id"])

    def test_restart_reconciles_already_promoted_selection_with_fenced_generation(
        self,
    ) -> None:
        controller = self.stalled()
        state = self.selection()
        pending = state.pop("pending")
        state.update(
            pending=None,
            active={key: value for key, value in pending.items() if key != "phase"},
            last_successful_source_id=pending["source_id"],
        )
        (self.root / "fireplace-selection.json").write_text(json.dumps(state))
        controller.close()
        reopened = self.multi()
        self.advance(reopened)
        proof = self.advance(reopened)
        self.assertEqual("playing", proof["state"])
        self.assertEqual("late-current", proof["request_id"])
        self.assertEqual(pending["attempt_id"], self.generation()["request_id"])
        self.assertFalse(self.generation()["cancelled"])

    def test_explicit_stop_cannot_be_reversed_by_late_matching_progress(self) -> None:
        controller = self.stalled()
        attempt = self.selection()["pending"]["attempt_id"]
        self.reject_stops = True
        with self.assertRaisesRegex(
            fixtures.fireplace.FireplaceError, "stop_unconfirmed"
        ):
            controller.stop()
        generation = self.generation()
        proof = self.advance(controller)
        self.assertNotEqual("playing", proof["state"])
        self.assertFalse(proof["progressing"])
        self.assertEqual(generation, self.generation())
        self.assertTrue(generation["cancelled"])
        self.assertNotEqual(attempt, generation["request_id"])
        self.assertIsNone(self.selection()["last_successful_source_id"])

    def test_cancelled_generation_remains_cancelled_after_bridge_restart(self) -> None:
        controller = self.stalled()
        self.reject_stops = True
        with self.assertRaises(fixtures.fireplace.FireplaceError):
            controller.stop()
        generation = self.generation()
        controller.close()
        self.reject_stops = False
        reopened = self.multi()
        proof = self.advance(reopened)
        self.assertNotEqual("playing", proof["state"])
        self.assertEqual(generation, self.generation())
        self.assertIsNone(self.selection()["last_successful_source_id"])

    def test_same_title_foreign_file_never_becomes_success(self) -> None:
        controller = self.stalled()
        expected = self.kodi.file
        native = controller._rpc

        def same_title(method, params, deadline) -> Any:
            result = native(method, params, deadline)
            if method == "Player.GetItem":
                result["item"]["title"] = expected
            return result

        controller._rpc = same_title
        self.kodi.file = "/human/other-video.mkv"
        proof = self.advance(controller)
        self.assertFalse(proof["progressing"])
        self.assertIsNone(self.selection()["last_successful_source_id"])
        self.assertEqual("/human/other-video.mkv", self.kodi.file)

    def test_new_admission_during_observation_blocks_old_promotion(self) -> None:
        self.isolated(lambda: self.newer_admission_race(at_store=False))

    def test_new_admission_during_selection_write_rolls_back_old_promotion(
        self,
    ) -> None:
        self.isolated(lambda: self.newer_admission_race(at_store=True))

    def newer_admission_race(self, *, at_store: bool) -> None:
        controller = self.stalled()
        attempt = self.selection()["pending"]["attempt_id"]
        paused, release, admitted = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        observed, observer_errors, launched, launch_errors = [], [], [], []
        original = controller._store if at_store else controller._proof
        clear = controller._cancelled.clear
        waiting = False

        def pause_boundary(*args: Any) -> Any:
            nonlocal waiting
            value = original(*args)
            if not waiting:
                waiting = True
                paused.set()
                if not release.wait(3):
                    raise TimeoutError
            return value

        def admit() -> None:
            clear()
            admitted.set()

        def unavailable(_source, _deadline) -> None:
            message = "new_source_unavailable"
            raise fixtures.fireplace.SourceUnavailable(message)

        controller._resolver = unavailable
        replacement = None
        boundary = "_store" if at_store else "_proof"
        with (
            patch.object(controller, boundary, side_effect=pause_boundary),
            patch.object(controller._cancelled, "clear", side_effect=admit),
        ):
            observer = recovery.worker(
                lambda: self.advance(controller), observed, observer_errors
            )
            try:
                self.assertTrue(paused.wait(2))
                replacement = recovery.worker(
                    lambda: controller.start("newer"), launched, launch_errors
                )
                self.assertTrue(admitted.wait(1))
            finally:
                release.set()
                observer.join(timeout=3)
                if replacement is not None:
                    replacement.join(timeout=3)
        self.assertFalse(observer.is_alive())
        self.assertIsNotNone(replacement)
        self.assertFalse(replacement.is_alive())
        self.assertEqual([], observer_errors)
        self.assertEqual([], launched)
        self.assertEqual(["sources_unavailable"], [str(e) for e in launch_errors])
        self.assertFalse(any(value["progressing"] for value in observed))
        self.assertEqual("newer", controller.status()["request_id"])
        self.assertIsNone(self.selection()["last_successful_source_id"])
        self.assertIsNone(self.selection()["active"])
        self.assertNotEqual(attempt, self.generation()["request_id"])
        self.assertEqual(2, self.generation()["intent_seq"])

    def test_stop_admitted_during_late_selection_write_wins(self) -> None:
        self.isolated(self.stop_during_store)

    def stop_during_store(self) -> None:
        controller = self.stalled()
        paused, release = threading.Event(), threading.Event()
        observed, observer_errors, stopped, stop_errors = [], [], [], []
        store = controller._store
        waiting = False

        def pause_store() -> None:
            nonlocal waiting
            store()
            if not waiting:
                waiting = True
                paused.set()
                if not release.wait(3):
                    raise TimeoutError

        stopper = None
        with patch.object(controller, "_store", side_effect=pause_store):
            observer = recovery.worker(
                lambda: self.advance(controller), observed, observer_errors
            )
            try:
                self.assertTrue(paused.wait(2))
                stopper = recovery.worker(controller.stop, stopped, stop_errors)
                self.assertTrue(controller._cancelled.wait(1))
            finally:
                release.set()
                observer.join(timeout=3)
                if stopper is not None:
                    stopper.join(timeout=3)
        self.assertFalse(observer.is_alive())
        self.assertIsNotNone(stopper)
        self.assertFalse(stopper.is_alive())
        self.assertEqual([], observer_errors + stop_errors)
        self.assertFalse(any(value["progressing"] for value in observed))
        self.assertTrue(self.generation()["cancelled"])
        self.assertIsNone(self.selection()["last_successful_source_id"])
        self.assertIsNone(self.kodi.file)

    def test_foreign_file_appearing_during_selection_write_is_not_promoted(
        self,
    ) -> None:
        controller = self.stalled()
        store = controller._store
        changed = False

        def foreign_after_store() -> None:
            nonlocal changed
            store()
            if not changed:
                changed = True
                self.kodi.file = "/human/new-during-store.mkv"

        with patch.object(controller, "_store", side_effect=foreign_after_store):
            proof = self.advance(controller)
        self.assertFalse(proof["progressing"])
        self.assertIsNone(self.selection()["last_successful_source_id"])
        self.assertEqual("/human/new-during-store.mkv", self.kodi.file)
