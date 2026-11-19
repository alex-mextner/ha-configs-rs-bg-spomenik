"""Fault real startup read boundaries without replaying mutations."""

from __future__ import annotations

import contextlib
import io
import json
import urllib.error
from typing import Any

import test_native_startup_budget as base


class StartupReadReconciliationCases(base.NativeStartupBudgetCases):
    """Keep transient startup reads inside one owned, cancellable launch."""

    def setUp(self) -> None:
        super().setUp()
        self.phase = "before_open"
        self.fail_phase = "pre_repeat"
        self.fail_method = "Player.GetActivePlayers"
        self.failures = 1
        self.fault_kind = "timeout"
        self.faults = []
        self.on_fault = lambda: None

    def controller(self, **options: Any) -> Any:
        controller = super().controller(**options)
        persist = controller._owner._persist
        start = controller._owner.start

        def persist_arm(receipt) -> None:
            persist(receipt)
            if receipt["phase"] == "armed":
                self.phase = "pre_repeat"

        def finish_native_start(**kwargs: Any) -> Any:
            proof = start(**kwargs)
            self.phase = "progress"
            return proof

        controller._owner._persist = persist_arm
        controller._owner.start = finish_native_start
        return controller

    def jsonrpc(self, method, params, *, timeout: float) -> dict[str, Any]:
        if (
            self.phase == self.fail_phase
            and method == self.fail_method
            and self.failures
        ):
            self.failures -= 1
            self.faults.append((self.phase, method, self.clock.now))
            self.transports.append((method, timeout, self.clock.now))
            self.on_fault()
            if self.fault_kind == "rejected":
                return {"ok": True, "body": {"error": {"code": -32602}}}
            if self.fault_kind == "http":
                private_url = "https://private.invalid/?secret=value"
                raise urllib.error.HTTPError(
                    private_url, 403, "denied", {}, io.BytesIO()
                )
            self.clock.now += timeout
            private_error = "https://private.invalid/?secret=value"
            if self.fault_kind == "private_engine_error":
                raise base.fixtures.fireplace.FireplaceError(private_error)
            raise TimeoutError(private_error)
        value = super().jsonrpc(method, params, timeout=timeout)
        if method == "Player.SetRepeat":
            self.phase = "post_repeat"
        return value

    def mutation_count(self, method: str) -> int:
        return sum(name == method for name, _, _ in self.transports)

    def phase_recovers(self, phase: str, method: str) -> None:
        self.fail_phase, self.fail_method = phase, method
        self.assert_playing(self.controller())
        self.assertEqual(len(self.faults), 1)
        self.assertEqual(self.mutation_count("Player.Open"), 1)
        self.assertEqual(self.mutation_count("Player.SetRepeat"), 1)

    def test_pre_repeat_active_read_recovers(self) -> None:
        self.phase_recovers("pre_repeat", "Player.GetActivePlayers")

    def test_pre_repeat_item_read_recovers(self) -> None:
        self.phase_recovers("pre_repeat", "Player.GetItem")

    def test_pre_repeat_properties_read_recovers(self) -> None:
        self.phase_recovers("pre_repeat", "Player.GetProperties")

    def test_post_repeat_active_read_recovers(self) -> None:
        self.phase_recovers("post_repeat", "Player.GetActivePlayers")

    def test_post_repeat_item_read_recovers(self) -> None:
        self.phase_recovers("post_repeat", "Player.GetItem")

    def test_post_repeat_properties_read_recovers(self) -> None:
        self.phase_recovers("post_repeat", "Player.GetProperties")

    def test_progress_read_recovers_with_existing_observation_budget(self) -> None:
        self.failures = 2
        self.fail_phase, self.fail_method = "progress", "Player.GetItem"
        self.assert_playing(self.controller())
        self.assertEqual(len(self.faults), 2)
        self.assertEqual(self.mutation_count("Player.Open"), 1)
        self.assertEqual(self.mutation_count("Player.SetRepeat"), 1)

    def test_foreign_file_after_properties_timeout_is_reidentified(self) -> None:
        self.fail_method = "Player.GetProperties"
        foreign = "/__newer_user_video__"

        def replace_content() -> None:
            self.queued_file = foreign
            self.kodi.file = foreign
            self.kodi.repeat = "off"

        self.on_fault = replace_content
        with self.assertRaises(base.fixtures.fireplace.FireplaceError):
            self.controller().start("foreign-during-read")
        self.assertEqual(self.kodi.file, foreign)
        self.assertEqual(self.kodi.repeat, "off")
        self.assertEqual(self.mutation_count("Player.SetRepeat"), 0)
        self.assertFalse((self.root / "owner.json").exists())

    def test_newer_repeat_after_properties_timeout_is_preserved(self) -> None:
        self.fail_method = "Player.GetProperties"
        self.on_fault = lambda: setattr(self.kodi, "repeat", "all")
        with self.assertRaises(base.fixtures.fireplace.FireplaceError):
            self.controller().start("repeat-during-read")
        self.assertEqual(self.kodi.repeat, "all")
        self.assertEqual(self.mutation_count("Player.SetRepeat"), 0)
        self.assertFalse((self.root / "owner.json").exists())

    def test_pre_repeat_retries_keep_original_deadline(self) -> None:
        self.visible_after = 0
        self.failures = 100
        with self.assertRaises(base.fixtures.fireplace.FireplaceError):
            self.controller(operation_timeout=6).start("pre-repeat-deadline")
        self.assertGreaterEqual(self.clock.now, 105)
        self.assertLessEqual(self.clock.now, 106.2)
        self.assertEqual(self.mutation_count("Player.Open"), 1)
        self.assertEqual(self.mutation_count("Player.SetRepeat"), 0)
        self.assertFalse((self.root / "owner.json").exists())

    def test_post_repeat_retries_keep_original_deadline_and_receipt(self) -> None:
        self.visible_after = 0
        self.fail_phase = "post_repeat"
        self.failures = 100
        with self.assertRaises(base.fixtures.fireplace.FireplaceError):
            self.controller(operation_timeout=6).start("post-repeat-deadline")
        self.assertGreaterEqual(self.clock.now, 105)
        self.assertLessEqual(self.clock.now, 106.2)
        self.assertEqual(self.mutation_count("Player.Open"), 1)
        self.assertEqual(self.mutation_count("Player.SetRepeat"), 1)
        self.assertTrue((self.root / "owner.json").exists())

    def cancel_at_phase(self, phase: str) -> None:
        self.visible_after = 0
        self.fail_phase = phase
        controller = self.controller()
        self.on_fault = controller._cancelled.set
        with self.assertRaisesRegex(
            base.fixtures.fireplace.FireplaceError, "cancelled"
        ):
            controller.start("cancel-late-startup")
        self.assertLess(self.clock.now, 102)
        self.assertEqual(self.mutation_count("Player.Open"), 1)
        self.assertEqual(
            self.mutation_count("Player.SetRepeat"), int(phase == "post_repeat")
        )
        self.assertEqual((self.root / "owner.json").exists(), phase == "post_repeat")

    def test_cancel_before_repeat_forgets_undispatched_arm(self) -> None:
        self.cancel_at_phase("pre_repeat")

    def test_cancel_after_repeat_retains_dispatched_lease(self) -> None:
        self.cancel_at_phase("post_repeat")

    def test_definite_pre_repeat_http_rejection_is_not_retried(self) -> None:
        self.fault_kind = "http"
        self.failures = 10
        with self.assertRaises(base.fixtures.fireplace.FireplaceError):
            self.controller().start("http-rejected-read")
        self.assertEqual(len(self.faults), 1)
        self.assertEqual(self.mutation_count("Player.SetRepeat"), 0)

    def test_definite_post_repeat_rpc_rejection_is_not_retried(self) -> None:
        self.fault_kind = "rejected"
        self.fail_phase = "post_repeat"
        self.failures = 10
        with self.assertRaises(base.fixtures.fireplace.FireplaceError):
            self.controller().start("rpc-rejected-read")
        self.assertEqual(len(self.faults), 1)
        self.assertEqual(self.mutation_count("Player.SetRepeat"), 1)

    def test_failure_diagnostic_names_phase_without_private_error(self) -> None:
        self.fault_kind = "http"
        stream = io.StringIO()
        with (
            contextlib.redirect_stderr(stream),
            self.assertRaises(base.fixtures.fireplace.FireplaceError),
        ):
            self.controller().start("safe-startup-failure")
        records = [
            json.loads(line)
            for line in stream.getvalue().splitlines()
            if line.startswith("{")
        ]
        errors = [
            record
            for record in records
            if record.get("stage") == "native_startup_failed"
        ]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["phase"], "arm_validation")
        self.assertEqual(errors[0].get("error_code"), "rpc_rejected")
        self.assertIn("error_class", errors[0])
        self.assertNotIn("private.invalid", stream.getvalue())
        self.assertNotIn("secret", stream.getvalue())

    def test_unknown_engine_error_code_is_omitted(self) -> None:
        self.fault_kind = "private_engine_error"
        stream = io.StringIO()
        with (
            contextlib.redirect_stderr(stream),
            self.assertRaises(base.fixtures.fireplace.FireplaceError),
        ):
            self.controller().start("unknown-startup-failure")
        records = [
            json.loads(line)
            for line in stream.getvalue().splitlines()
            if line.startswith("{")
        ]
        errors = [
            record
            for record in records
            if record.get("stage") == "native_startup_failed"
        ]
        self.assertEqual(len(errors), 1)
        self.assertIn("error_code", errors[0])
        self.assertIsNone(errors[0]["error_code"])
        self.assertNotIn("private.invalid", stream.getvalue())
        self.assertNotIn("secret", stream.getvalue())

    def test_diagnostic_ignores_non_string_or_multiple_error_args(self) -> None:
        owner = self.controller()._owner
        owner._startup_started = self.clock.now
        for args in (({},), ("rpc_rejected", "private"), ()):
            with self.subTest(args=args):
                stream = io.StringIO()
                with contextlib.redirect_stderr(stream):
                    owner._startup_failure(Exception(*args), self.clock.now + 10)
                record = json.loads(stream.getvalue())
                self.assertIn("error_code", record)
                self.assertIsNone(record["error_code"])
                self.assertNotIn("private", stream.getvalue())
