"""The remote-only ship gate must reject stale or skipped verification."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, Mock, patch

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "ci_gate", ROOT / "scripts/ci/wait_ha_regressions.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class CiReceiptTests(unittest.TestCase):
    """Require complete native push evidence before the shipping gate succeeds."""

    def receipt(self, **changes: Any) -> dict[str, Any]:
        value = {
            "headSha": "required-commit",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "jobs": [{"name": "maintained-suites", "conclusion": "success"}],
            "url": "https://github.com/example/repo/actions/runs/1",
        }
        value.update(changes)
        return value

    def test_exact_commit_and_successful_required_suite_are_accepted(self) -> None:
        self.assertEqual(
            gate.verify_receipt(self.receipt(), "required-commit"),
            self.receipt()["url"],
        )

    def test_success_for_another_commit_is_not_evidence(self) -> None:
        with self.assertRaises(gate.GateError):
            gate.verify_receipt(
                self.receipt(headSha="previous-commit"), "required-commit"
            )

    def test_skipped_missing_or_duplicate_job_is_not_a_pass(self) -> None:
        for jobs in (
            [],
            [{"name": "maintained-suites", "conclusion": "skipped"}],
            [{"name": "maintained-suites", "conclusion": "success"}] * 2,
        ):
            with self.subTest(jobs=jobs), self.assertRaises(gate.GateError):
                gate.verify_receipt(self.receipt(jobs=jobs), "required-commit")

    def test_pending_failed_and_cancelled_runs_fail_closed(self) -> None:
        for changes in (
            {"status": "in_progress"},
            {"conclusion": "failure"},
            {"conclusion": "cancelled"},
        ):
            with self.subTest(changes=changes), self.assertRaises(gate.GateError):
                gate.verify_receipt(self.receipt(**changes), "required-commit")

    def test_feature_receipt_requires_both_actual_jobs(self) -> None:
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

    def test_dirty_worktree_cannot_borrow_clean_head_evidence(self) -> None:
        with (
            patch.object(
                gate, "capture", return_value=" M scripts/host/ha_kodi_fireplace.py"
            ) as capture,
            self.assertRaisesRegex(gate.GateError, "clean"),
        ):
            gate.local_commit()
        self.assertEqual(capture.call_count, 1)

    def test_feature_failure_blocks_after_maintained_suite_success(self) -> None:
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
            self.assertRaisesRegex(gate.GateError, "feature failed"),
        ):
            gate.verify_all(Mock(), Mock())
        self.assertEqual(
            [call.args[2] for call in workflow.call_args_list],
            ["ha-regressions.yml", "kodi-fireplace.yml"],
        )
        self.assertEqual(
            workflow.call_args_list[1].args[3],
            ("host-controller", "home-assistant-script"),
        )

    def test_head_movement_during_remote_wait_invalidates_evidence(self) -> None:
        with (
            patch.object(
                gate, "local_commit", side_effect=["required-commit", "new-commit"]
            ),
            patch.object(
                gate, "capture", return_value='{"nameWithOwner":"example/repo"}'
            ),
            patch.object(gate, "verify_workflow"),
            self.assertRaisesRegex(gate.GateError, "changed"),
        ):
            gate.verify_all(Mock(), Mock())

    def test_merge_tree_manual_and_missing_event_receipts_are_rejected(self) -> None:
        for event in ("pull_request", "pull_request_target", "workflow_dispatch", None):
            with self.subTest(event=event), self.assertRaises(gate.GateError):
                gate.verify_receipt(self.receipt(event=event), "required-commit")
        missing = self.receipt()
        del missing["event"]
        with self.assertRaises(gate.GateError):
            gate.verify_receipt(missing, "required-commit")

    def test_pr_only_run_cannot_satisfy_the_exact_source_gate(self) -> None:
        run = self.receipt(event="pull_request", databaseId=301)
        with (
            patch.object(
                gate, "capture", side_effect=[json.dumps([run]), json.dumps(run)]
            ) as capture,
            self.assertRaises(gate.GateError),
        ):
            gate.verify_workflow(
                "example/repo",
                "required-commit",
                "ha-regressions.yml",
                ("maintained-suites",),
                Mock(),
                io.StringIO(),
            )
        self.assertEqual(capture.call_count, 1)

    def test_newer_pr_success_cannot_hide_a_failed_push(self) -> None:
        pushed = self.receipt(databaseId=201, conclusion="failure")
        merged = self.receipt(databaseId=301, event="pull_request")
        observed: list[list[str]] = []

        def response(arguments: list[str]) -> str:
            observed.append(arguments)
            if arguments[1:3] == ["run", "list"]:
                return json.dumps([merged, pushed])
            return json.dumps(pushed if arguments[3] == "201" else merged)

        with (
            patch.object(gate, "capture", side_effect=response),
            self.assertRaises(gate.GateError),
        ):
            gate.verify_workflow(
                "example/repo",
                "required-commit",
                "ha-regressions.yml",
                ("maintained-suites",),
                Mock(),
                io.StringIO(),
            )
        self.assertEqual(observed[-1][3], "201")

    def test_newest_push_failure_cannot_borrow_an_older_push_success(self) -> None:
        old = self.receipt(databaseId=101)
        latest = self.receipt(databaseId=201, conclusion="failure")

        def response(arguments: list[str]) -> str:
            if arguments[1:3] == ["run", "list"]:
                return json.dumps([old, latest])
            return json.dumps(latest if arguments[3] == "201" else old)

        with (
            patch.object(gate, "capture", side_effect=response),
            self.assertRaises(gate.GateError),
        ):
            gate.verify_workflow(
                "example/repo",
                "required-commit",
                "ha-regressions.yml",
                ("maintained-suites",),
                Mock(),
                io.StringIO(),
            )

    def test_lint_failure_blocks_after_both_regression_workflows_pass(self) -> None:
        with (
            patch.object(gate, "local_commit", return_value="required-commit"),
            patch.object(
                gate, "capture", return_value='{"nameWithOwner":"example/repo"}'
            ),
            patch.object(
                gate,
                "verify_workflow",
                side_effect=[None, None, gate.GateError("Ruff did not pass")],
            ) as workflow,
            self.assertRaisesRegex(gate.GateError, "Ruff"),
        ):
            gate.verify_all(Mock(), Mock())
        self.assertEqual(
            [(call.args[2], call.args[3]) for call in workflow.call_args_list],
            [
                ("ha-regressions.yml", ("maintained-suites",)),
                ("kodi-fireplace.yml", ("host-controller", "home-assistant-script")),
                ("lint.yml", ("Ruff",)),
            ],
        )

    def test_failed_metadata_has_safe_operation_exit_and_error_category(self) -> None:
        arguments = ["gh", "repo", "view", "--json", "nameWithOwner"]
        sensitive = "fixture-do-not-log-this-secret"
        failure = subprocess.CompletedProcess(
            arguments,
            4,
            "",
            "HTTP 401: Bad credentials "
            f"(https://api.github.com/private?token={sensitive}) "
            f"Authorization: Bearer {sensitive}",
        )
        with (
            patch.object(gate.subprocess, "run", return_value=failure),
            self.assertRaises(gate.GateError) as caught,
        ):
            gate.capture(arguments)
        message = str(caught.exception)
        self.assertIn("gh repo view", message)
        self.assertIn("exit=4", message)
        self.assertIn("authentication", message.lower())
        self.assertNotIn(sensitive, message)
        self.assertNotIn("https://", message)
        self.assertNotIn("Authorization", message)

    def test_watch_progress_reaches_the_terminal_and_private_log(self) -> None:
        pending = self.receipt(databaseId=201, status="in_progress")
        receipt = self.receipt(databaseId=201)
        progress = "host-controller: queued -> in_progress\n"
        log, terminal = io.StringIO(), io.StringIO()
        process = MagicMock()
        process.stdout = io.StringIO(progress)
        process.wait.return_value = 0
        process.poll.return_value = 0
        process.returncode = 0
        process.__enter__.return_value = process

        def record(message: str) -> None:
            sys.stdout.write(message + "\n")
            print(message, file=log)

        def legacy_watch(
            arguments: list[str], **options: Any
        ) -> subprocess.CompletedProcess:
            options["stdout"].write(progress)
            return subprocess.CompletedProcess(arguments, 0)

        with (
            patch.object(
                gate,
                "capture",
                side_effect=[json.dumps([pending]), json.dumps(receipt)],
            ),
            patch.object(gate.subprocess, "run", side_effect=legacy_watch),
            patch.object(gate.subprocess, "Popen", return_value=process),
            redirect_stdout(terminal),
        ):
            gate.verify_workflow(
                "example/repo",
                "required-commit",
                "ha-regressions.yml",
                ("maintained-suites",),
                record,
                log,
            )
        self.assertIn(progress.strip(), terminal.getvalue())
        self.assertIn(progress.strip(), log.getvalue())

    def test_event_field_is_requested_for_list_and_view(self) -> None:
        run = self.receipt(databaseId=201)
        observed: list[list[str]] = []

        def response(arguments: list[str]) -> str:
            observed.append(arguments)
            fields = arguments[arguments.index("--json") + 1].split(",")
            projected = {name: run[name] for name in fields}
            payload = [projected] if arguments[1:3] == ["run", "list"] else projected
            return json.dumps(payload)

        with patch.object(gate, "capture", side_effect=response):
            gate.verify_workflow(
                "example/repo",
                "required-commit",
                "ha-regressions.yml",
                ("maintained-suites",),
                Mock(),
                io.StringIO(),
            )
        self.assertEqual(
            [arguments[1:3] for arguments in observed],
            [["run", "list"], ["run", "view"]],
        )
        for arguments in observed:
            with self.subTest(operation=arguments[2]):
                fields = arguments[arguments.index("--json") + 1].split(",")
                self.assertIn("event", fields)

    def metadata_changed_after_wait(
        self,
        final_runs: list[dict[str, Any]],
        final_receipts: dict[str, dict[str, Any]],
    ) -> tuple[
        Callable[[list[str]], str],
        Callable[[str, str, Callable[[str], None]], None],
    ]:
        waited = False
        baseline = {
            "ha-regressions.yml": self.receipt(databaseId=101),
            "kodi-fireplace.yml": self.receipt(
                databaseId=201,
                jobs=[
                    {"name": "host-controller", "conclusion": "success"},
                    {"name": "home-assistant-script", "conclusion": "success"},
                ],
            ),
            "lint.yml": self.receipt(
                databaseId=301,
                jobs=[{"name": "Ruff", "conclusion": "success"}],
            ),
        }
        initial_runs = {name: [dict(receipt)] for name, receipt in baseline.items()}
        initial_runs["kodi-fireplace.yml"][0].update(
            status="in_progress", conclusion=None
        )
        receipts_by_id = {
            str(receipt["databaseId"]): receipt for receipt in baseline.values()
        }

        def response(arguments: list[str]) -> str:
            if arguments[1:3] == ["repo", "view"]:
                return '{"nameWithOwner": "example/repo"}'
            fields = arguments[arguments.index("--json") + 1].split(",")
            if arguments[1:3] == ["run", "list"]:
                workflow = arguments[arguments.index("--workflow") + 1]
                runs = (
                    final_runs
                    if waited and workflow == "ha-regressions.yml"
                    else initial_runs[workflow]
                )
                return json.dumps(
                    [{field: run[field] for field in fields} for run in runs]
                )
            run_id = arguments[3]
            receipts = (
                {**receipts_by_id, **final_receipts} if waited else receipts_by_id
            )
            return json.dumps({field: receipts[run_id][field] for field in fields})

        def finish_wait(
            _repo: str, _run_id: str, _record: Callable[[str], None]
        ) -> None:
            nonlocal waited
            waited = True

        return response, finish_wait

    def test_later_wait_invalidates_earlier_receipts_without_another_wait(self) -> None:
        accepted = self.receipt(databaseId=101)
        failed_rerun = self.receipt(databaseId=101, conclusion="failure")
        pending_rerun = self.receipt(
            databaseId=101, status="in_progress", conclusion=None
        )
        newer_failed = self.receipt(databaseId=401, conclusion="failure")
        newer_pending = self.receipt(databaseId=401, status="queued", conclusion=None)
        scenarios = (
            ("rerun failed", [accepted], {"101": failed_rerun}),
            ("rerun pending", [accepted], {"101": pending_rerun}),
            ("new push failed", [accepted, newer_failed], {"401": newer_failed}),
            ("new push pending", [accepted, newer_pending], {"401": newer_pending}),
        )
        for name, final_runs, final_receipts in scenarios:
            response, finish_wait = self.metadata_changed_after_wait(
                final_runs, final_receipts
            )
            with (
                self.subTest(change=name),
                patch.object(gate, "local_commit", return_value="required-commit"),
                patch.object(gate, "capture", side_effect=response),
                patch.object(gate, "watch_run", side_effect=finish_wait) as watch,
                self.assertRaises(gate.GateError),
            ):
                gate.verify_all(Mock(), io.StringIO())
            self.assertEqual(watch.call_count, 1)
            self.assertEqual(watch.call_args.args[1], "201")

    def test_final_pass_reselects_latest_success_for_every_workflow(self) -> None:
        accepted = self.receipt(databaseId=101)
        newer_success = self.receipt(databaseId=401)
        response, finish_wait = self.metadata_changed_after_wait(
            [accepted, newer_success], {"401": newer_success}
        )
        with (
            patch.object(gate, "local_commit", return_value="required-commit"),
            patch.object(gate, "capture", side_effect=response) as capture,
            patch.object(gate, "watch_run", side_effect=finish_wait) as watch,
        ):
            gate.verify_all(Mock(), io.StringIO())
        requests = [call.args[0] for call in capture.call_args_list]
        listings = [
            arguments[arguments.index("--workflow") + 1]
            for arguments in requests
            if arguments[1:3] == ["run", "list"]
        ]
        self.assertEqual(
            listings,
            ["ha-regressions.yml", "kodi-fireplace.yml", "lint.yml"] * 2,
        )
        views = [
            arguments[3] for arguments in requests if arguments[1:3] == ["run", "view"]
        ]
        self.assertEqual(views, ["101", "201", "301", "401", "201", "301"])
        self.assertEqual(watch.call_count, 1)
