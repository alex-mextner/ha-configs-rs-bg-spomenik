"""Crash recovery and intent ordering against the real rotation controller."""

import json
import threading
import time
from unittest.mock import patch

import test_rotation as fixtures


class SimulatedCrashError(BaseException):
    """Stop an operation at a durable boundary without ordinary cleanup."""


class CrashLock:
    """Model process exit after stop admission and before its owner transition."""

    def __enter__(self):
        raise SimulatedCrashError

    def __exit__(self, *_args):
        return False


class DelayedStopLock:
    """Deterministically let a newer start acquire the operation lock first."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.waiting = threading.Event()
        self.release_stop = threading.Event()

    def __enter__(self):
        if threading.current_thread().name == "older-stop":
            self.waiting.set()
            if not self.release_stop.wait(5):
                raise TimeoutError
        self.lock.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.lock.release()

    def acquire(self, **kwargs):
        return self.lock.acquire(**kwargs)

    def release(self) -> None:
        self.lock.release()


def worker(call, results, errors, *, name="request") -> threading.Thread:
    """Collect the real operation outcome without losing thread exceptions."""

    def invoke() -> None:
        try:
            results.append(call())
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=invoke, name=name)
    thread.start()
    return thread


class RecoveryCases(fixtures.FixtureBase):
    """Prove stop, crash and observation boundaries retain the right intent."""

    multi = fixtures.RotationCases.multi

    def selection(self) -> dict:
        return json.loads((self.root / "fireplace-selection.json").read_text())

    def generation(self) -> dict:
        return json.loads((self.root / "youtube/current-request.json").read_text())

    def test_extraction_crash_restores_the_previous_active_manifest(self) -> None:
        controller = self.multi()
        controller.start("previous")
        old_attempt = self.selection()["active"]["attempt_id"]

        def extraction_crash(_source, _deadline):
            raise SimulatedCrashError

        controller._resolver = extraction_crash
        with self.assertRaises(SimulatedCrashError):
            controller.start("replacement")
        self.assertNotEqual(self.generation()["request_id"], old_attempt)
        controller.close()
        recovered = self.multi()
        recovered.observe()
        self.assertEqual(self.generation()["request_id"], old_attempt)
        self.assertEqual(self.selection()["last_successful_source_id"], "local-0")

    def test_prepared_recovery_after_restart_never_revives_a_stopped_generation(
        self,
    ) -> None:
        controller = self.multi()
        controller.start("previous")
        with patch.object(controller._owner, "start", side_effect=SimulatedCrashError):
            with self.assertRaises(SimulatedCrashError):
                controller.start("prepared")
        state = self.selection()
        playable_attempts = {
            state["active"]["attempt_id"],
            state["pending"]["attempt_id"],
        }
        with patch.object(controller, "_lock", CrashLock()):
            with self.assertRaises(SimulatedCrashError):
                controller.stop()
        controller.close()
        self.kodi.calls.clear()
        recovered = self.multi()
        proof = recovered.observe()
        self.assertNotIn(self.generation()["request_id"], playable_attempts)
        self.assertNotEqual(proof["state"], "playing")
        self.assertIsNone(self.kodi.file)
        self.assertFalse([call for call in self.kodi.calls if call[0] == "Player.Open"])

    def test_stop_during_promotion_cannot_receive_a_later_success(self) -> None:
        controller = self.multi(operation_timeout=5)
        saving, release = threading.Event(), threading.Event()
        original_store = controller._store
        starts, start_errors, stops, stop_errors = [], [], [], []

        def paused_store() -> None:
            if controller._active is not None and controller._pending is None:
                saving.set()
                if not release.wait(3):
                    raise TimeoutError
            original_store()

        stopper = None
        with patch.object(controller, "_store", side_effect=paused_store):
            launcher = worker(lambda: controller.start("new"), starts, start_errors)
            try:
                self.assertTrue(saving.wait(2))
                stopper = worker(controller.stop, stops, stop_errors)
                self.assertTrue(controller._cancelled.wait(1))
            finally:
                release.set()
                launcher.join(timeout=3)
                if stopper is not None:
                    stopper.join(timeout=3)
        self.assertFalse(launcher.is_alive())
        self.assertIsNotNone(stopper)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(starts, [], "Stop was admitted before success publication")
        self.assertEqual([str(error) for error in start_errors], ["cancelled"])
        self.assertEqual(stop_errors, [])
        self.assertFalse(controller.status()["owned"])

    def test_an_older_waiting_stop_cannot_kill_a_newer_start(self) -> None:
        controller = self.multi(operation_timeout=5)
        controller.start("previous")
        gate = DelayedStopLock()
        results, errors = [], []
        with patch.object(controller, "_lock", gate):
            stopper = worker(controller.stop, results, errors, name="older-stop")
            try:
                self.assertTrue(gate.waiting.wait(1))
                proof = controller.start("newer")
                selected_file = self.kodi.file
                self.assertEqual(proof["state"], "playing")
            finally:
                gate.release_stop.set()
                stopper.join(timeout=3)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.kodi.file, selected_file)
        self.assertEqual(controller.status()["request_id"], "newer")
        self.assertEqual(controller.status()["state"], "playing")

    def test_crash_after_first_open_is_recovered_without_a_repeat_lease(self) -> None:
        controller = self.multi()
        original = controller._rpc

        def crash_after_open(method, params, deadline):
            result = original(method, params, deadline)
            if method == "Player.Open":
                raise SimulatedCrashError
            return result

        controller._rpc = crash_after_open
        with self.assertRaises(SimulatedCrashError):
            controller.start("interrupted")
        self.assertFalse((self.root / "owner.json").exists())
        controller.close()
        self.kodi.calls.clear()
        recovered = self.multi(auto_monitor=True)
        recovered.start_monitor()
        deadline = time.monotonic() + 2
        while self.kodi.file is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNone(self.kodi.file)
        self.assertFalse([call for call in self.kodi.calls if call[0] == "Player.Open"])
        self.assertIsNone(self.selection()["last_successful_source_id"])

    def test_progress_samples_from_different_attempts_cannot_confirm_a_source(
        self,
    ) -> None:
        controller = self.multi()
        controller.start("previous")
        previous_file = self.kodi.file
        original = controller._rpc

        def crash_after_open(method, params, deadline):
            result = original(method, params, deadline)
            if method == "Player.Open":
                raise SimulatedCrashError
            return result

        controller._rpc = crash_after_open
        with self.assertRaises(SimulatedCrashError):
            controller.start("pending")
        pending_file = self.kodi.file
        controller.close()
        native_rpc = self.kodi.rpc

        def fixed_position(method, params, deadline):
            result = native_rpc(method, params, deadline)
            if method == "Player.GetProperties":
                result["time"] = {
                    "hours": 0,
                    "minutes": 0,
                    "seconds": 10 if self.kodi.file == previous_file else 20,
                    "milliseconds": 0,
                }
            return result

        self.kodi.rpc = fixed_position
        recovered = self.multi()
        self.kodi.file = previous_file
        recovered.observe()
        self.kodi.file = pending_file
        proof = recovered.observe()
        self.assertFalse(proof["progressing"])
        self.assertNotEqual(proof["state"], "playing")
        self.assertEqual(self.selection()["last_successful_source_id"], "local-0")

    def test_deferred_cleanup_after_stop_publishes_released_ownership(self) -> None:
        controller = self.multi()
        controller.start("own")
        original = controller._rpc
        foreign = True

        def audio_player(method, params, deadline):
            if foreign and method == "Player.GetActivePlayers":
                return [{"playerid": 0, "type": "audio", "playertype": "internal"}]
            return original(method, params, deadline)

        controller._rpc = audio_player
        self.assertTrue(controller.stop()["owned"])
        foreign = False
        self.kodi.file, self.kodi.speed = None, 0
        proof = controller.observe()
        self.assertFalse(proof["owned"])
        self.assertEqual(proof["state"], "idle")
        self.assertEqual(self.kodi.repeat, "off")

    def test_preparation_past_the_launch_deadline_never_opens(self) -> None:
        def delayed(source, _deadline):
            time.sleep(0.08)
            return {"file": source["path"]}

        controller = self.multi(operation_timeout=0.04, resolver=delayed)
        with self.assertRaises(fixtures.fireplace.FireplaceError):
            controller.start("expired")
        self.assertFalse([call for call in self.kodi.calls if call[0] == "Player.Open"])

    def test_missing_item_file_retains_ownership_during_a_native_transition(
        self,
    ) -> None:
        controller = self.multi()
        controller.start("own")
        original = controller._rpc
        self.kodi.calls.clear()

        def incomplete_item(method, params, deadline):
            if method == "Player.GetItem":
                return {"item": {"label": "transition"}}
            return original(method, params, deadline)

        controller._rpc = incomplete_item
        proof = controller.observe()
        self.assertTrue(proof["owned"])
        self.assertEqual(proof["state"], "unknown")
        self.assertEqual(self.kodi.repeat, "one")
        self.assertFalse(
            [call for call in self.kodi.calls if call[0] == "Player.SetRepeat"]
        )

    def test_stalled_source_with_uncertain_stop_is_never_retried(self) -> None:
        original = self.kodi.rpc

        def stalled_and_lost_stop(method, params, deadline):
            result = original(method, params, deadline)
            if method == "Player.GetProperties":
                result["time"] = {
                    "hours": 0,
                    "minutes": 0,
                    "seconds": 0,
                    "milliseconds": 0,
                }
            if method == "Player.Stop":
                raise fixtures.fireplace.FireplaceError("rpc_unavailable")
            return result

        self.kodi.rpc = stalled_and_lost_stop
        controller = self.multi(operation_timeout=8)
        with self.assertRaisesRegex(
            fixtures.fireplace.FireplaceError, "stop_unconfirmed"
        ):
            controller.start("stalled")
        self.assertEqual(controller.status()["state"], "unknown")
        self.assertEqual(
            len([call for call in self.kodi.calls if call[0] == "Player.Open"]), 1
        )
