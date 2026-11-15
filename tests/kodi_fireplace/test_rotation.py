"""Durable fireplace selection and playback evidence regressions."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Self
from unittest.mock import Mock, patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "fireplace", ROOT / "scripts/host/ha_kodi_fireplace.py"
)
fireplace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fireplace)


class Kodi:
    """Model kodi state and controlled responses for regression checks."""

    def __init__(self) -> None:
        self.file = None
        self.repeat = "off"
        self.speed = 0
        self.started = time.monotonic()
        self.calls = []

    def rpc(self, method, params, deadline) -> Any:
        self.calls.append((method, dict(params)))
        if method == "Player.GetActivePlayers":
            return (
                [{"playerid": 1, "type": "video", "playertype": "internal"}]
                if self.file
                else []
            )
        if method == "Player.GetItem":
            return {
                "item": {
                    "file": self.file,
                    "label": Path(self.file).name,
                    "title": Path(self.file).name,
                }
            }
        if method == "Player.GetProperties":
            elapsed = time.monotonic() - self.started
            return {
                "repeat": self.repeat,
                "speed": self.speed,
                "time": {
                    "hours": 0,
                    "minutes": 0,
                    "seconds": int(elapsed),
                    "milliseconds": int(elapsed % 1 * 1000),
                },
                "totaltime": {
                    "hours": 0,
                    "minutes": 10,
                    "seconds": 0,
                    "milliseconds": 0,
                },
            }
        if method == "Player.Open":
            self.file = params["item"]["file"]
            self.speed = 1
            self.started = time.monotonic()
            return "OK"
        if method == "Player.SetRepeat":
            self.repeat = params["repeat"]
            return "OK"
        if method == "Player.Stop":
            self.file = None
            self.speed = 0
            return "OK"
        raise AssertionError(method)


class FixtureBase(unittest.TestCase):
    """Exercise fixture base behavior with deterministic native fixtures."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "one.webm"
        self.media.write_bytes(b"verified fireplace fixture")
        self.sha = hashlib.sha256(self.media.read_bytes()).hexdigest()
        self.kodi = Kodi()
        self.controllers = []

    def tearDown(self) -> None:
        for controller in self.controllers:
            controller.close()
        self.temp.cleanup()

    def controller(self) -> Any:
        controller = fireplace.FireplaceController(
            self.kodi.rpc,
            self.root / "owner.json",
            media_path=self.media,
            expected_sha256=self.sha,
            auto_monitor=False,
            operation_timeout=3,
            poll_interval=0.01,
        )
        self.controllers.append(controller)
        return controller


class RotationRegression(FixtureBase):
    """Exercise rotation regression behavior with deterministic native fixtures."""

    def test_a_second_success_cannot_reuse_the_only_source(self) -> None:
        controller = self.controller()
        self.assertEqual(controller.start()["state"], "playing")
        with self.assertRaises(
            fireplace.FireplaceError,
            msg="A second command must choose a different source or report failure",
        ):
            controller.start()

    def test_restart_cannot_forget_the_last_successful_source(self) -> None:
        controller = self.controller()
        self.assertEqual(controller.start()["state"], "playing")
        controller.close()
        recovered = self.controller()
        with self.assertRaises(
            fireplace.FireplaceError,
            msg="Restart must retain no-adjacent-repeat history",
        ):
            recovered.start()


class RotationCases(FixtureBase):
    """Exercise rotation cases behavior with deterministic native fixtures."""

    def multi(self, **options: Any) -> Any:
        sources = []
        for index in range(3):
            path = self.root / (str(index) + ".webm")
            path.write_bytes(("fireplace-" + str(index)).encode())
            sources.append(
                {
                    "id": "local-" + str(index),
                    "kind": "local",
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "title": path.name,
                }
            )
        defaults = {
            "sources": sources,
            "auto_monitor": False,
            "operation_timeout": 2,
            "poll_interval": 0.01,
            "progress_interval": 0.02,
            "chooser": lambda _items: None,
        }
        defaults.update(options)
        controller = fireplace.FireplaceController(
            self.kodi.rpc, self.root / "owner.json", **defaults
        )
        self.controllers.append(controller)
        return controller

    def test_three_commands_advance_and_match_their_own_request(self) -> None:
        controller = self.multi()
        proofs = [controller.start("request-" + str(index)) for index in range(3)]
        self.assertEqual(
            [value["source_id"] for value in proofs], ["local-0", "local-1", "local-0"]
        )
        for index, value in enumerate(proofs):
            self.assertEqual(value["request_id"], "request-" + str(index))
            self.assertTrue(value["progressing"])
            self.assertTrue(value["file_matches"])
            self.assertEqual(value["repeat"], "one")
            self.assertGreaterEqual(value["observed_at"], value["launch_started_at"])

    def test_stop_and_restart_preserve_selection_history_and_prior_repeat(self) -> None:
        self.kodi.repeat = "all"
        controller = self.multi()
        first = controller.start("first")
        controller.stop()
        self.assertEqual(self.kodi.repeat, "all")
        controller.close()
        second = self.multi().start("second")
        self.assertNotEqual(first["source_id"], second["source_id"])

    def test_failed_resolution_skips_source_and_records_only_success(self) -> None:
        attempted = []

        def resolve(source, _deadline) -> dict[str, Any]:
            attempted.append(source["id"])
            if source["id"] == "local-0":
                error_message = "missing_video"
                raise fireplace.SourceUnavailable(error_message)
            return {"file": source["path"]}

        controller = self.multi(resolver=resolve)
        proof = controller.start("fallback")
        self.assertEqual(attempted, ["local-0", "local-1"])
        self.assertEqual(proof["source_id"], "local-1")

        state = json.loads((self.root / "fireplace-selection.json").read_text())
        self.assertEqual(state["last_successful_source_id"], "local-1")

    def test_failed_new_command_does_not_relabel_previous_playback_as_success(
        self,
    ) -> None:
        controller = self.multi()
        controller.start("old")

        def unavailable(_source, _deadline) -> None:
            error_message = "unavailable"
            raise fireplace.SourceUnavailable(error_message)

        controller._resolver = unavailable
        with self.assertRaises(fireplace.FireplaceError):
            controller.start("new")
        controller.observe()
        self.assertEqual(controller.status()["state"], "error")
        self.assertEqual(controller.status()["request_id"], "new")
        self.assertFalse(controller.status()["progressing"])

    def test_status_is_responsive_and_concurrent_start_is_explicitly_busy(self) -> None:

        entered, release = threading.Event(), threading.Event()
        errors = []

        def resolve(source, _deadline) -> dict[str, Any]:
            entered.set()
            if not release.wait(1):
                error_message = "test_timeout"
                raise fireplace.SourceUnavailable(error_message)
            return {"file": source["path"]}

        controller = self.multi(resolver=resolve)

        def launch() -> None:
            try:
                controller.start("running")
            # Return worker failures to the joining test for assertions.
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        worker = threading.Thread(target=launch)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            value = controller.status()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(value["state"], "starting")
            self.assertEqual(value["request_id"], "running")
            with self.assertRaisesRegex(fireplace.FireplaceError, "busy"):
                controller.start("concurrent")
            self.assertEqual(controller.status()["request_id"], "running")
        finally:
            release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_ambiguous_open_is_not_retried_or_reported_as_a_definitive_failure(
        self,
    ) -> None:
        original = self.kodi.rpc

        def uncertain(method, params, deadline) -> Any:
            result = original(method, params, deadline)
            if method == "Player.Open":
                error_message = "ack lost"
                raise OSError(error_message)
            return result

        self.kodi.rpc = uncertain
        controller = self.multi()
        with self.assertRaisesRegex(fireplace.FireplaceError, "open_unconfirmed"):
            controller.start("uncertain")
        self.assertEqual(
            len([item for item in self.kodi.calls if item[0] == "Player.Open"]), 1
        )
        self.assertEqual(controller.status()["state"], "unknown")
        self.assertEqual(controller.status()["request_id"], "uncertain")
        self.assertFalse(controller.status()["progressing"])

    def test_invalid_request_never_opens_media(self) -> None:
        controller = self.multi()
        for ident in ("", "x" * 97, "has space", "../path", 17, False):
            with self.assertRaisesRegex(fireplace.FireplaceError, "invalid_request_id"):
                controller.start(ident)
        self.assertFalse(self.kodi.calls)

    def test_corrupt_history_fails_closed(self) -> None:
        (self.root / "fireplace-selection.json").write_text("{bad")
        controller = self.multi()
        with self.assertRaisesRegex(fireplace.FireplaceError, "selection_unreadable"):
            controller.start("request")
        self.assertFalse(self.kodi.calls)

    def test_foreign_video_restores_prior_repeat_and_never_leaks_its_title(
        self,
    ) -> None:
        controller = self.multi()
        controller.start("own")
        self.kodi.file = "/private/secret-movie.mkv"
        value = controller.observe()
        self.assertEqual(self.kodi.repeat, "off")
        self.assertFalse(value["owned"])
        self.assertNotIn("secret-movie", str(value))

    def test_stalled_identified_source_is_stopped_before_failover(self) -> None:
        original = self.kodi.rpc

        def stalled_first(method, params, deadline) -> Any:
            result = original(method, params, deadline)
            if (
                method == "Player.GetProperties"
                and self.kodi.file
                and self.kodi.file.endswith("0.webm")
            ):
                result["time"] = {
                    "hours": 0,
                    "minutes": 0,
                    "seconds": 0,
                    "milliseconds": 0,
                }
            return result

        self.kodi.rpc = stalled_first
        controller = self.multi(operation_timeout=8)
        proof = controller.start("stalled-fallback")
        self.assertEqual(proof["source_id"], "local-1")
        mutations = [
            method
            for method, _ in self.kodi.calls
            if method in {"Player.Open", "Player.Stop"}
        ]
        self.assertEqual(mutations, ["Player.Open", "Player.Stop", "Player.Open"])

        state = json.loads((self.root / "fireplace-selection.json").read_text())
        self.assertEqual(state["last_successful_source_id"], "local-1")

    def test_attempt_generation_is_canonical_and_invalidated_after_failed_source(
        self,
    ) -> None:

        generations = []

        def resolve(source, _deadline) -> dict[str, Any]:
            generation = json.loads(
                (self.root / "youtube/current-request.json").read_text()
            )["request_id"]
            self.assertEqual(str(UUID(generation)), generation)
            generations.append(generation)
            if len(generations) == 1:
                error_message = "not_found"
                raise fireplace.SourceUnavailable(error_message)
            return {"file": source["path"]}

        controller = self.multi(resolver=resolve)
        controller.start("generation-proof")
        self.assertEqual(len(set(generations)), 2)
        controller.stop()
        final = json.loads((self.root / "youtube/current-request.json").read_text())[
            "request_id"
        ]
        self.assertNotIn(final, generations)

    def test_corrupt_history_remains_unchanged_after_stop_and_observation(self) -> None:
        history = self.root / "fireplace-selection.json"
        history.write_text("{unreadable-state")
        controller = self.multi()
        with self.assertRaisesRegex(fireplace.FireplaceError, "selection_unreadable"):
            controller.stop()
        self.assertEqual(controller.observe()["error"], "selection_unreadable")
        self.assertEqual(history.read_text(), "{unreadable-state")
        self.assertEqual(self.kodi.calls, [])

    def test_crash_after_preparation_preserves_previous_owned_source(self) -> None:

        class Crash(BaseException):
            """Exercise crash behavior with deterministic native fixtures."""

        controller = self.multi()
        previous = controller.start("previous")
        old_file = self.kodi.file
        with (
            patch.object(controller._owner, "start", side_effect=Crash),
            self.assertRaises(Crash),
        ):
            controller.start("prepared")
        state = json.loads((self.root / "fireplace-selection.json").read_text())
        self.assertEqual(state["active"]["source_id"], previous["source_id"])
        self.assertEqual(state["pending"]["phase"], "prepared")
        controller.close()
        self.kodi.calls.clear()
        recovered = self.multi()
        recovered.observe()
        time.sleep(0.03)
        proof = recovered.observe()
        self.assertEqual(proof["source_id"], previous["source_id"])
        self.assertTrue(proof["owned"])
        self.assertEqual(self.kodi.file, old_file)
        self.assertEqual(self.kodi.repeat, "one")
        self.assertFalse(
            [
                call
                for call in self.kodi.calls
                if call[0] in {"Player.Open", "Player.Stop", "Player.SetRepeat"}
            ]
        )

    def test_crash_after_open_recovers_actual_candidate_identity(self) -> None:
        class Crash(BaseException):
            """Exercise crash behavior with deterministic native fixtures."""

        controller = self.multi()
        controller.start("previous")
        original = controller._rpc

        def crash_after_open(method, params, deadline) -> Any:
            result = original(method, params, deadline)
            if method == "Player.Open":
                raise Crash
            return result

        controller._rpc = crash_after_open
        with self.assertRaises(Crash):
            controller.start("new-command")
        controller.close()
        recovered = self.multi()
        recovered.observe()
        time.sleep(0.03)
        proof = recovered.observe()
        self.assertEqual(proof["source_id"], "local-1")
        self.assertEqual(proof["request_id"], "new-command")
        self.assertTrue(proof["progressing"])

    def test_lost_stop_ack_immediately_clears_success_evidence(self) -> None:
        controller = self.multi()
        controller.start("own")
        original = controller._rpc

        def lost_stop(method, params, deadline) -> Any:
            result = original(method, params, deadline)
            if method == "Player.Stop":
                error_message = "lost acknowledgement"
                raise OSError(error_message)
            return result

        controller._rpc = lost_stop
        with self.assertRaisesRegex(fireplace.FireplaceError, "stop_unconfirmed"):
            controller.stop()
        proof = controller.status()
        self.assertEqual(proof["state"], "unknown")
        self.assertFalse(proof["progressing"])
        self.assertFalse(proof["file_matches"])

    def test_launch_budget_does_not_reduce_later_cleanup_budget(self) -> None:
        def delayed(source, _deadline) -> dict[str, Any]:
            time.sleep(0.12)
            return {"file": source["path"]}

        controller = self.multi(operation_timeout=0.4, resolver=delayed)
        before = controller._owner._operation_timeout
        controller.start("late-success")
        self.assertEqual(controller._owner._operation_timeout, before)
        controller.stop()
        self.assertEqual(self.kodi.repeat, "off")

    def test_native_title_takes_precedence_over_kodi_label(self) -> None:
        original = self.kodi.rpc

        def native_title(method, params, deadline) -> Any:
            result = original(method, params, deadline)
            if method == "Player.GetItem":
                self.assertIn("title", params["properties"])
                result["item"].update(
                    title="Actual native title", label="Different label"
                )
            return result

        self.kodi.rpc = native_title
        proof = self.multi().start("title-proof")
        self.assertEqual(proof["title"], "Actual native title")

    def test_failed_preparation_restores_current_manifest_generation(self) -> None:

        controller = self.multi()
        original = controller.start("original")
        generation = self.root / "youtube/current-request.json"
        original_attempt = json.loads(generation.read_text())["request_id"]
        original_file = self.kodi.file

        def unavailable(_source, _deadline) -> None:
            error_message = "provider_unavailable"
            raise fireplace.SourceUnavailable(error_message)

        controller._resolver = unavailable
        with self.assertRaisesRegex(fireplace.FireplaceError, "sources_unavailable"):
            controller.start("replacement")
        self.assertEqual(
            json.loads(generation.read_text())["request_id"], original_attempt
        )
        self.assertEqual(self.kodi.file, original_file)
        self.assertEqual(controller._active["source_id"], original["source_id"])
        self.assertIsNone(controller._pending)
        self.assertEqual(controller.status()["request_id"], "replacement")
        self.assertEqual(controller.status()["state"], "error")

    def test_uncertain_replacement_never_revives_previous_manifest_generation(
        self,
    ) -> None:

        controller = self.multi()
        controller.start("original")
        generation = self.root / "youtube/current-request.json"
        original_attempt = json.loads(generation.read_text())["request_id"]
        original_rpc = controller._rpc

        def lost_open(method, params, deadline) -> Any:
            result = original_rpc(method, params, deadline)
            if method == "Player.Open":
                error_message = "lost acknowledgement"
                raise OSError(error_message)
            return result

        controller._rpc = lost_open
        with self.assertRaisesRegex(fireplace.FireplaceError, "open_unconfirmed"):
            controller.start("replacement")
        self.assertNotEqual(
            json.loads(generation.read_text())["request_id"], original_attempt
        )
        self.assertEqual(controller.status()["state"], "unknown")

    def test_stop_during_preparation_prevents_any_late_open(self) -> None:

        entered, release = threading.Event(), threading.Event()
        errors, stopped = [], []

        def resolve(source, _deadline) -> dict[str, Any]:
            entered.set()
            if not release.wait(1):
                error_message = "test_timeout"
                raise fireplace.SourceUnavailable(error_message)
            return {"file": source["path"]}

        controller = self.multi(resolver=resolve)

        def launch() -> None:
            try:
                controller.start("cancel-me")
            except fireplace.FireplaceError as error:
                errors.append(str(error))

        launch_thread = threading.Thread(target=launch)
        stop_thread = threading.Thread(target=lambda: stopped.append(controller.stop()))
        launch_thread.start()
        try:
            self.assertTrue(entered.wait(1))
            stop_thread.start()
            self.assertTrue(controller._cancelled.wait(0.5))
            self.assertFalse(controller.status()["progressing"])
        finally:
            release.set()
            launch_thread.join(timeout=2)
            if stop_thread.ident is not None:
                stop_thread.join(timeout=2)
        self.assertFalse(launch_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(errors, ["cancelled"])
        self.assertFalse(stopped[0]["owned"])
        self.assertFalse([call for call in self.kodi.calls if call[0] == "Player.Open"])

    def test_stop_at_launch_admission_is_never_cleared_by_launch(self) -> None:

        admitted, release, stop_entered = (threading.Event() for _ in range(3))
        errors, stopped = [], []
        controller = self.multi()
        original_lock = controller._lock

        class AdmissionLock:
            """Exercise admission lock behavior with deterministic native fixtures."""

            # Match threading.Lock.acquire for the injected lock fixture.
            def acquire(self, blocking=True) -> Any:  # noqa: FBT002
                acquired = original_lock.acquire(blocking=blocking)
                if acquired and not admitted.is_set():
                    admitted.set()
                    release.wait(1)
                return acquired

            def release(self) -> None:
                original_lock.release()

            def __enter__(self) -> Self:
                """Enter the controlled fixture context."""
                self.acquire()
                return self

            def __exit__(self, *args: object) -> None:
                """Exit the fixture context without suppressing failures."""
                original_lock.release()

        controller._lock = AdmissionLock()
        original_candidates = controller._candidates

        def candidates_after_stop() -> Any:
            self.assertTrue(
                controller._cancelled.wait(1), "Admitted launch erased the newer stop"
            )
            return original_candidates()

        controller._candidates = candidates_after_stop

        def launch() -> None:
            try:
                controller.start("admission-race")
            except fireplace.FireplaceError as error:
                errors.append(str(error))

        def stop() -> None:
            stop_entered.set()
            stopped.append(controller.stop())

        launch_thread = threading.Thread(target=launch)
        stop_thread = threading.Thread(target=stop)
        launch_thread.start()
        try:
            self.assertTrue(admitted.wait(1))
            stop_thread.start()
            self.assertTrue(stop_entered.wait(1))
            controller._cancelled.wait(0.1)
        finally:
            release.set()
            launch_thread.join(timeout=3)
            if stop_thread.ident is not None:
                stop_thread.join(timeout=3)
        self.assertFalse(launch_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(errors, ["cancelled"])
        self.assertFalse(stopped[0]["owned"])
        self.assertFalse([call for call in self.kodi.calls if call[0] == "Player.Open"])

    def test_replacement_waits_for_monitor_instead_of_reporting_busy(self) -> None:

        controller = self.multi()
        controller.start("first")
        entered, release, finished = (threading.Event() for _ in range(3))
        proofs, errors = [], []

        def observe() -> None:
            with controller._lock:
                entered.set()
                release.wait(1)

        def launch() -> None:
            try:
                proofs.append(controller.start("after-observation"))
            except fireplace.FireplaceError as error:
                errors.append(str(error))
            finally:
                finished.set()

        monitor = threading.Thread(target=observe)
        launcher = threading.Thread(target=launch)
        monitor.start()
        try:
            self.assertTrue(entered.wait(1))
            launcher.start()
            self.assertFalse(finished.wait(0.05))
        finally:
            release.set()
            monitor.join(timeout=2)
            if launcher.ident is not None:
                launcher.join(timeout=3)
        self.assertFalse(launcher.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(proofs[0]["source_id"], "local-1")

    def test_unleased_candidate_stop_accepts_real_kodi_player_metadata(self) -> None:
        controller = self.multi()
        target = str(self.media)
        self.kodi.file, self.kodi.speed = target, 1
        controller._pending = {
            "source_id": "local-0",
            "source_kind": "local",
            "file_hash": hashlib.sha256(target.encode()).hexdigest(),
            "phase": "opening",
        }
        controller.stop()
        self.assertIsNone(self.kodi.file)
        self.assertEqual(
            sum(method == "Player.Stop" for method, _ in self.kodi.calls), 1
        )

    def test_prepared_recovery_never_reverses_a_newer_cancellation(self) -> None:

        controller = self.multi()
        controller.start("active")
        controller._pending = {**controller._active, "phase": "prepared"}
        controller._store()
        controller._cancelled.set()
        controller._cancel_attempt()
        generation = self.root / "youtube/current-request.json"
        cancellation = json.loads(generation.read_text())
        controller.observe()
        self.assertEqual(json.loads(generation.read_text()), cancellation)
        self.assertIsNone(controller._pending)

    def test_pre_open_failure_is_definitive_and_restores_active_generation(
        self,
    ) -> None:

        controller = self.multi()
        controller.start("active")
        generation = self.root / "youtube/current-request.json"
        active_generation = json.loads(generation.read_text())
        previous_file = self.kodi.file
        with (
            patch.object(
                controller._owner,
                "start",
                side_effect=fireplace.FireplaceError("previous_owner_pending"),
            ),
            self.assertRaisesRegex(fireplace.FireplaceError, "sources_unavailable"),
        ):
            controller.start("pre-open-failed")
        self.assertEqual(controller.status()["state"], "error")
        observed_generation = json.loads(generation.read_text())
        self.assertEqual(
            observed_generation["request_id"], active_generation["request_id"]
        )
        self.assertFalse(observed_generation["cancelled"])
        self.assertGreater(
            observed_generation["intent_seq"], active_generation["intent_seq"]
        )
        self.assertEqual(self.kodi.file, previous_file)
        self.assertEqual(
            sum(method == "Player.Open" for method, _ in self.kodi.calls), 1
        )

    def test_corrupt_owner_fails_before_source_preparation(self) -> None:

        (self.root / "owner.json").write_text("unreadable owner")
        resolver = Mock()
        controller = self.multi(resolver=resolver)
        self.assertEqual(controller.status()["state"], "error")
        with self.assertRaisesRegex(fireplace.FireplaceError, "selection_unreadable"):
            controller.start("bad-owner")
        resolver.assert_not_called()
        self.assertEqual(self.kodi.calls, [])

    def test_observation_cannot_republish_playing_after_stop_intent(self) -> None:
        controller = self.multi()
        controller.start("active")
        controller._cancelled.set()
        pending = controller.status()
        pending.update(
            state="unknown", progressing=False, observed_at=None, error="stop_pending"
        )
        controller._publish(pending)
        observed = controller.observe()
        self.assertEqual(observed["state"], "unknown")
        self.assertFalse(observed["progressing"])
        self.assertEqual(observed["error"], "stop_pending")
