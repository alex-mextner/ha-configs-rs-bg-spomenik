"""The remote-only ship gate must reject stale or skipped verification."""

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "ci_gate", ROOT / "scripts/ci/wait_ha_regressions.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class CiReceiptTests(unittest.TestCase):
    def receipt(self, **changes):
        value = {
            "headSha": "required-commit",
            "status": "completed",
            "conclusion": "success",
            "jobs": [{"name": "maintained-suites", "conclusion": "success"}],
            "url": "https://github.com/example/repo/actions/runs/1",
        }
        value.update(changes)
        return value

    def test_exact_commit_and_successful_required_suite_are_accepted(self):
        self.assertEqual(
            gate.verify_receipt(self.receipt(), "required-commit"),
            self.receipt()["url"],
        )

    def test_success_for_another_commit_is_not_evidence(self):
        with self.assertRaises(gate.GateError):
            gate.verify_receipt(
                self.receipt(headSha="previous-commit"), "required-commit"
            )

    def test_skipped_missing_or_duplicate_job_is_not_a_pass(self):
        for jobs in (
            [],
            [{"name": "maintained-suites", "conclusion": "skipped"}],
            [{"name": "maintained-suites", "conclusion": "success"}] * 2,
        ):
            with self.subTest(jobs=jobs), self.assertRaises(gate.GateError):
                gate.verify_receipt(self.receipt(jobs=jobs), "required-commit")

    def test_pending_failed_and_cancelled_runs_fail_closed(self):
        for changes in (
            {"status": "in_progress"},
            {"conclusion": "failure"},
            {"conclusion": "cancelled"},
        ):
            with self.subTest(changes=changes), self.assertRaises(gate.GateError):
                gate.verify_receipt(self.receipt(**changes), "required-commit")

    def test_feature_receipt_requires_both_actual_jobs(self):
        required = ("host-controller", "home-assistant-script")
        jobs = [{"name": name, "conclusion": "success"} for name in required]
        self.assertEqual(
            gate.verify_receipt(self.receipt(jobs=jobs), "required-commit", required),
            self.receipt()["url"],
        )
        for invalid in (
            jobs[:1],
            [jobs[0], {"name": required[1], "conclusion": "skipped"}],
        ):
            with self.subTest(jobs=invalid), self.assertRaises(gate.GateError):
                gate.verify_receipt(
                    self.receipt(jobs=invalid), "required-commit", required
                )

    def test_dirty_worktree_cannot_borrow_clean_head_evidence(self):
        from unittest.mock import patch

        with patch.object(
            gate, "capture", return_value=" M scripts/host/ha_kodi_fireplace.py"
        ) as capture:
            with self.assertRaisesRegex(gate.GateError, "clean"):
                gate.local_commit()
        self.assertEqual(capture.call_count, 1)

    def test_feature_failure_blocks_after_maintained_suite_success(self):
        from unittest.mock import Mock, patch

        with (
            patch.object(gate, "local_commit", return_value="required-commit"),
            patch.object(
                gate, "capture", return_value='{"nameWithOwner":"example/repo"}'
            ),
            patch.object(
                gate,
                "verify_workflow",
                side_effect=[None, gate.GateError("feature failed")],
            ) as workflow,
        ):
            with self.assertRaisesRegex(gate.GateError, "feature failed"):
                gate.verify_all(Mock(), Mock())
        self.assertEqual(
            [call.args[2] for call in workflow.call_args_list],
            ["ha-regressions.yml", "kodi-fireplace.yml"],
        )
        self.assertEqual(
            workflow.call_args_list[1].args[3],
            ("host-controller", "home-assistant-script"),
        )

    def test_head_movement_during_remote_wait_invalidates_evidence(self):
        from unittest.mock import Mock, patch

        with (
            patch.object(
                gate, "local_commit", side_effect=["required-commit", "new-commit"]
            ),
            patch.object(
                gate, "capture", return_value='{"nameWithOwner":"example/repo"}'
            ),
            patch.object(gate, "verify_workflow"),
        ):
            with self.assertRaisesRegex(gate.GateError, "changed"):
                gate.verify_all(Mock(), Mock())
