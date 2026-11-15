#!/usr/bin/env python3
"""
Verify required native push runs for the exact clean local commit.

This command reads Git/GitHub metadata only; it never executes tests or builds.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import TextIO

WORKFLOWS = {
    "ha-regressions.yml": ("maintained-suites",),
    "kodi-fireplace.yml": ("host-controller", "home-assistant-script"),
    "lint.yml": ("Ruff",),
}
ROOT = Path(__file__).resolve().parents[2]
METADATA_TIMEOUT = 60
AUTHENTICATION_EXIT_CODE = 4
WATCH_TIMEOUT = 3600
ERROR_LIMIT = 65536
OPERATIONS = (
    ("git", "status"),
    ("git", "rev-parse"),
    ("gh", "repo", "view"),
    ("gh", "run", "list"),
    ("gh", "run", "view"),
    ("gh", "run", "watch"),
)
SENSITIVE_PROGRESS = re.compile(
    r"authorization|bearer\s|token|password|secret|credential|cookie|"
    r"gh[pousr]_|github_pat_",
    re.IGNORECASE,
)
PROGRESS_URL = re.compile(r"https?://\S+", re.IGNORECASE)
TERMINAL_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class GateError(Exception):
    """A safe, actionable gate failure."""


def operation_name(arguments: list[str]) -> str:
    """Identify a supported metadata operation without echoing its arguments."""
    for operation in OPERATIONS:
        if arguments[: len(operation)] == list(operation):
            return " ".join(operation)
    message = "Unsupported metadata operation"
    raise GateError(message)


def failure_category(stderr: str, returncode: int) -> str:
    """Classify a failed command without retaining its raw diagnostic text."""
    lowered = stderr.lower()
    if returncode == AUTHENTICATION_EXIT_CODE or any(
        word in lowered for word in ("authentication", "bad credentials", "http 401")
    ):
        return "authentication"
    if any(word in lowered for word in ("http 403", "permission denied", "forbidden")):
        return "authorization"
    if any(word in lowered for word in ("not found", "not a git repository")):
        return "repository-or-run-not-found"
    if any(
        word in lowered
        for word in ("timeout", "timed out", "connection", "tls", "resolve host")
    ):
        return "transport"
    return "metadata-or-run-status"


def command_failure(operation: str, returncode: int, stderr: str) -> GateError:
    """Describe the operation, exit status, and sanitized failure category."""
    category = failure_category(stderr, returncode)
    return GateError(f"{operation} failed: exit={returncode} category={category}")


def capture(arguments: list[str]) -> str:
    """Read metadata and report failures without exposing raw stderr."""
    operation = operation_name(arguments)
    try:
        result = subprocess.run(
            arguments,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=METADATA_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        message = f"{operation} failed: category=timeout"
        raise GateError(message) from None
    except OSError:
        message = f"{operation} failed: category=command-unavailable"
        raise GateError(message) from None
    if result.returncode:
        raise command_failure(operation, result.returncode, result.stderr)
    return result.stdout.strip()


def local_commit() -> str:
    """Require clean source, index, untracked paths, and submodule state."""
    if capture(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ]
    ):
        message = "The worktree/index must be clean before using a commit's CI receipt"
        raise GateError(message)
    return capture(["git", "rev-parse", "HEAD"])


def verify_receipt(
    value: dict[str, Any],
    commit: str,
    required_jobs: tuple[str, ...] = ("maintained-suites",),
) -> str | None:
    """Accept only a successful push receipt and every required native job."""
    if (
        value.get("headSha") != commit
        or value.get("event") != "push"
        or value.get("status") != "completed"
        or value.get("conclusion") != "success"
    ):
        message = "Required push CI has not passed for this exact commit"
        raise GateError(message)
    for name in required_jobs:
        jobs = [job for job in value.get("jobs", []) if job.get("name") == name]
        if len(jobs) != 1 or jobs[0].get("conclusion") != "success":
            message = "A required native regression job did not pass: " + name
            raise GateError(message)
    return value.get("url")


def safe_progress(line: str) -> str:
    """Keep visible watch progress without credential text or embedded URLs."""
    text = TERMINAL_ESCAPE.sub("", line).strip()
    if SENSITIVE_PROGRESS.search(text):
        return "credential-related diagnostic withheld"
    return PROGRESS_URL.sub("[url]", text)[:1000]


def expire_watch(process: subprocess.Popen[str], expired: threading.Event) -> None:
    """Bound a stalled watch process without interrupting a finished command."""
    if process.poll() is None:
        expired.set()
        with contextlib.suppress(ProcessLookupError):
            process.kill()


def watch_run(
    repo: str,
    run_id: str,
    record: Callable[[str], None],
) -> None:
    """Mirror live watch output while retaining only categorized stderr failures."""
    arguments = [
        "gh",
        "run",
        "watch",
        run_id,
        "--repo",
        repo,
        "--exit-status",
        "--interval",
        "10",
    ]
    operation = operation_name(arguments)
    expired = threading.Event()
    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as diagnostics,
        subprocess.Popen(
            arguments,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=diagnostics,
            bufsize=1,
        ) as process,
    ):
        timer = threading.Timer(WATCH_TIMEOUT, expire_watch, args=(process, expired))
        timer.daemon = True
        timer.start()
        try:
            if process.stdout is None:
                message = "gh run watch failed: category=missing-progress-stream"
                raise GateError(message)
            for line in process.stdout:
                if progress := safe_progress(line):
                    record("watch " + progress)
            returncode = process.wait()
        finally:
            timer.cancel()
        if expired.is_set():
            message = "gh run watch failed: category=timeout"
            raise GateError(message)
        if returncode:
            diagnostics.seek(0)
            raise command_failure(operation, returncode, diagnostics.read(ERROR_LIMIT))


def latest_push_run(repo: str, commit: str, workflow: str) -> dict[str, Any]:
    """Relist and select the newest push run for this workflow and commit."""
    runs = json.loads(
        capture(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repo,
                "--workflow",
                workflow,
                "--commit",
                commit,
                "--event",
                "push",
                "--limit",
                "20",
                "--json",
                "databaseId,headSha,event,status,conclusion,url",
            ]
        )
    )
    matching = [
        run
        for run in runs
        if run.get("headSha") == commit and run.get("event") == "push"
    ]
    if not matching:
        message = (
            "No native push run for HEAD: "
            + workflow
            + "; push the reviewed commit first"
        )
        raise GateError(message)
    return max(matching, key=lambda item: item["databaseId"])


def current_run_receipt(repo: str, run_id: str) -> dict[str, Any]:
    """Reread the selected run's current event, result, and native job statuses."""
    return json.loads(
        capture(
            [
                "gh",
                "run",
                "view",
                run_id,
                "--repo",
                repo,
                "--json",
                "headSha,event,status,conclusion,url,jobs",
            ]
        )
    )


def verify_workflow(
    repo: str,
    commit: str,
    workflow: str,
    required_jobs: tuple[str, ...],
    record: Callable[[str], None],
    _log: TextIO,
) -> None:
    """Wait for and verify the newest exact-commit push run for one workflow."""
    run = latest_push_run(repo, commit, workflow)
    run_id = str(run["databaseId"])
    record(
        f"native CI workflow={workflow} commit={commit} run={run_id} url={run['url']}"
    )
    if run.get("status") != "completed":
        watch_run(repo, run_id, record)
    url = verify_receipt(current_run_receipt(repo, run_id), commit, required_jobs)
    record(f"verified native regression receipt workflow={workflow} url={url}")


def verify_all(record: Callable[[str], None], log: TextIO) -> None:
    """Wait for required workflows, then revalidate current receipts and source."""
    commit = local_commit()
    repo = json.loads(capture(["gh", "repo", "view", "--json", "nameWithOwner"]))[
        "nameWithOwner"
    ]
    for workflow, required_jobs in WORKFLOWS.items():
        verify_workflow(repo, commit, workflow, required_jobs, record, log)
    if local_commit() != commit:
        message = "The local commit changed while native CI was being verified"
        raise GateError(message)
    record("revalidating latest push receipts without waiting")
    for workflow, required_jobs in WORKFLOWS.items():
        run = latest_push_run(repo, commit, workflow)
        receipt = current_run_receipt(repo, str(run["databaseId"]))
        url = verify_receipt(receipt, commit, required_jobs)
        record(f"revalidated latest native push receipt workflow={workflow} url={url}")
    if local_commit() != commit:
        message = "The local commit changed while native CI was being verified"
        raise GateError(message)


def main() -> int:
    """Report verification progress to the terminal and a private automatic log."""
    descriptor, log_path = tempfile.mkstemp(
        prefix="ha-regression-ci-gate-", suffix=".log", dir="/tmp"
    )
    os.fchmod(descriptor, 0o600)
    started, outcome = time.monotonic(), 1
    with os.fdopen(descriptor, "w", encoding="utf-8") as log:

        def record(message: str) -> None:
            line = f"{datetime.now(UTC).isoformat()} {message} log={log_path}\n"
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            log.flush()

        record("starting")
        try:
            verify_all(record, log)
            outcome = 0
        except (
            GateError,
            OSError,
            subprocess.TimeoutExpired,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            record(
                str(error)
                if isinstance(error, GateError)
                else "Unable to verify native CI receipt"
            )
        finally:
            record(f"completed rc={outcome} elapsed={time.monotonic() - started:.1f}s")
    return outcome


if __name__ == "__main__":
    raise SystemExit(main())
