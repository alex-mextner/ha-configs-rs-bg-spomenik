"""Late native activation and human intent across exact stop confirmation."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import test_rotation as fixtures
import test_rotation_recovery as recovery


class StopBoundaryCases(fixtures.FixtureBase):
    """Exercise stop boundary cases behavior with deterministic native fixtures."""

    multi = fixtures.RotationCases.multi
    selection = recovery.RecoveryCases.selection
    interrupted_first_open = recovery.RecoveryCases.interrupted_first_open

    def looped_pending(self) -> Any:
        controller = self.multi()
        with (
            patch.object(
                controller, "_await_progress", side_effect=recovery.SimulatedCrashError
            ),
            self.assertRaises(recovery.SimulatedCrashError),
        ):
            controller.start("interrupted-after-loop")
        controller.close()
        self.kodi.calls.clear()
        return self.multi()

    def test_empty_stop_cannot_reuse_an_earlier_pending_match(self) -> None:
        controller = self.looped_pending()
        expected = self.kodi.file
        self.assertTrue(controller.observe()["owned"])
        self.kodi.file, self.kodi.speed = None, 0
        self.kodi.calls.clear()
        with self.assertRaisesRegex(
            fixtures.fireplace.FireplaceError, "stop_unconfirmed"
        ):
            controller.stop()
        self.assertIsNotNone(self.selection()["pending"])
        self.assertFalse([call for call in self.kodi.calls if call[0] == "Player.Stop"])
        self.kodi.file, self.kodi.speed = expected, 1
        controller.observe()
        self.assertIsNone(self.kodi.file)
        self.assertIsNone(self.selection()["pending"])

    def test_absence_during_initial_progress_keeps_delayed_cleanup_identity(
        self,
    ) -> None:
        controller = self.multi(operation_timeout=8)
        observe = controller._owner.observe
        expected = str(self.root / "0.webm")

        def disappear(**options: Any) -> Any:
            self.kodi.file, self.kodi.speed = None, 0
            controller._owner._idle_since = time.monotonic() - 3
            return observe(**options)

        with (
            patch.object(controller._owner, "observe", side_effect=disappear),
            self.assertRaises(fixtures.fireplace.FireplaceError),
        ):
            controller.start("disappearing")
        self.assertIsNotNone(self.selection()["pending"])
        self.assertEqual(controller.status()["state"], "unknown")
        self.assertEqual(
            sum(method == "Player.Open" for method, _ in self.kodi.calls), 1
        )
        self.kodi.file, self.kodi.speed = expected, 1
        controller.observe()
        self.assertIsNone(self.kodi.file)
        self.assertIsNone(self.selection()["pending"])

    def test_reactivation_during_final_reread_retains_pending_for_another_stop(
        self,
    ) -> None:
        controller = self.interrupted_first_open()
        expected = self.kodi.file
        observe = controller._owner.observe
        reads = 0

        def reactivate(**options: Any) -> Any:
            nonlocal reads
            reads += 1
            if reads == 2:
                self.kodi.file, self.kodi.speed = expected, 1
            return observe(**options)

        with patch.object(controller._owner, "observe", side_effect=reactivate):
            proof = controller.observe()
        self.assertEqual(reads, 2)
        self.assertEqual(proof["state"], "unknown")
        self.assertIsNotNone(self.selection()["pending"])
        self.assertEqual(self.kodi.file, expected)
        controller.observe()
        self.assertIsNone(self.kodi.file)
        self.assertIsNone(self.selection()["pending"])
        self.assertEqual(
            sum(method == "Player.Stop" for method, _ in self.kodi.calls), 2
        )

    def test_malformed_post_stop_players_never_confirm_cleanup(self) -> None:
        for malformed in (None, {}, False, 0):
            with self.subTest(response=malformed):
                controller = self.interrupted_first_open()
                native = controller._rpc
                stopped = False

                def malformed_after_stop(
                    method, params, deadline, *, case=(malformed, native)
                ) -> Any:
                    nonlocal stopped
                    malformed, native = case
                    if method == "Player.GetActivePlayers" and stopped:
                        return malformed
                    result = native(method, params, deadline)
                    if method == "Player.Stop":
                        stopped = True
                    return result

                controller._rpc = malformed_after_stop
                proof = controller.observe()
                self.assertTrue(stopped)
                self.assertEqual(proof["state"], "unknown")
                self.assertIsNotNone(self.selection()["pending"])
                controller.close()

    def test_stop_final_read_failure_recovers_without_another_request(self) -> None:
        controller = self.multi()
        controller.start("owned")
        read = controller._owner._read
        injected = False

        def fail_after_release(deadline) -> Any:
            nonlocal injected
            value = read(deadline)
            if controller._owner._owner is None and not injected:
                injected = True
                error_message = "final stop observation unavailable"
                raise OSError(error_message)
            return value

        with (
            patch.object(controller._owner, "_read", side_effect=fail_after_release),
            self.assertRaisesRegex(
                fixtures.fireplace.FireplaceError, "stop_unconfirmed"
            ),
        ):
            controller.stop()
        self.assertTrue(injected)
        self.assertIsNotNone(self.selection()["active"])
        self.kodi.calls.clear()
        controller._auto_monitor = True
        controller.start_monitor()
        deadline = time.monotonic() + 2
        while controller.status()["state"] != "idle" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(controller.status()["state"], "idle")
        self.assertIsNone(self.selection()["active"])
        self.assertEqual(self.selection()["last_successful_source_id"], "local-0")
        self.assertFalse([call for call in self.kodi.calls if call[0] == "Player.Open"])

    def fallback_after_human_change(self, change) -> None:
        prepared = []

        def prepare(source, _deadline) -> dict[str, Any]:
            prepared.append(source["id"])
            if len(prepared) == 2:
                change()
            return {"file": source["path"]}

        controller = self.multi(operation_timeout=8, resolver=prepare)
        native = controller._rpc

        def stall_first(method, params, deadline) -> Any:
            result = native(method, params, deadline)
            if method == "Player.GetProperties" and self.kodi.file == str(
                self.root / "0.webm"
            ):
                result["time"] = {
                    "hours": 0,
                    "minutes": 0,
                    "seconds": 0,
                    "milliseconds": 0,
                }
            return result

        controller._rpc = stall_first
        with self.assertRaises(fixtures.fireplace.FireplaceError):
            controller.start("fallback-overtaken")
        self.assertEqual(prepared, ["local-0", "local-1"])
        self.assertEqual(
            sum(method == "Player.Open" for method, _ in self.kodi.calls), 1
        )
        self.assertIsNone(self.selection()["last_successful_source_id"])

    def test_human_video_started_during_fallback_preparation_is_preserved(self) -> None:
        def human_video() -> None:
            self.kodi.file, self.kodi.speed = "/human/movie.mkv", 1

        self.fallback_after_human_change(human_video)
        self.assertEqual(self.kodi.file, "/human/movie.mkv")

    def test_human_repeat_changed_during_fallback_preparation_is_preserved(
        self,
    ) -> None:
        def human_repeat() -> None:
            self.kodi.repeat = "all"

        self.fallback_after_human_change(human_repeat)
        self.assertEqual(self.kodi.repeat, "all")

    def test_shutdown_inside_adapter_drops_an_undispatched_repeat_arm(self) -> None:
        controller = self.multi()
        callback = controller._owner._rpc_callback

        def close_before_dispatch(method, params, deadline) -> Any:
            if method == "Player.SetRepeat":
                controller._closing.set()
            return callback(method, params, deadline)

        controller._owner._rpc_callback = close_before_dispatch
        with self.assertRaises(fixtures.fireplace.FireplaceError):
            controller.start("closed-before-repeat")
        self.assertFalse((self.root / "owner.json").exists())
        self.assertFalse(
            [call for call in self.kodi.calls if call[0] == "Player.SetRepeat"]
        )
