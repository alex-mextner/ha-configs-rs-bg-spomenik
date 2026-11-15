#!/usr/bin/env python3
"""Verify all required native CI suites for the exact clean local commit.

This command reads Git/GitHub metadata only; it never executes tests or builds.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

WORKFLOWS = {
    "ha-regressions.yml": ("maintained-suites",),
    "kodi-fireplace.yml": ("host-controller", "home-assistant-script"),
}
ROOT = Path(__file__).resolve().parents[2]


class GateError(Exception):
    """A safe, actionable gate failure."""


def capture(arguments):
    result = subprocess.run(
        arguments, cwd=ROOT, text=True, capture_output=True, timeout=60, check=False
    )
    if result.returncode:
        raise GateError("Git/GitHub metadata request failed; inspect the native CI run")
    return result.stdout.strip()


def local_commit():
    if capture(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ]
    ):
        raise GateError(
            "The worktree/index must be clean before using a commit's CI receipt"
        )
    return capture(["git", "rev-parse", "HEAD"])


def verify_receipt(value, commit, required_jobs=("maintained-suites",)):
    if (
        value.get("headSha") != commit
        or value.get("status") != "completed"
        or value.get("conclusion") != "success"
    ):
        raise GateError("Required regression CI has not passed for this exact commit")
    for name in required_jobs:
        jobs = [job for job in value.get("jobs", []) if job.get("name") == name]
        if len(jobs) != 1 or jobs[0].get("conclusion") != "success":
            raise GateError("A required native regression job did not pass: " + name)
    return value.get("url")


def verify_workflow(repo, commit, workflow, required_jobs, record, log):
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
                "--limit",
                "20",
                "--json",
                "databaseId,headSha,status,conclusion,url",
            ]
        )
    )
    matching = [run for run in runs if run.get("headSha") == commit]
    if not matching:
        raise GateError(
            "No native run for HEAD: " + workflow + "; push the reviewed commit first"
        )
    run = max(matching, key=lambda item: item["databaseId"])
    run_id = str(run["databaseId"])
    record(
        f"native CI workflow={workflow} commit={commit} run={run_id} url={run['url']}"
    )
    if run.get("status") != "completed":
        result = subprocess.run(
            [
                "gh",
                "run",
                "watch",
                run_id,
                "--repo",
                repo,
                "--exit-status",
                "--interval",
                "10",
            ],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=3600,
            check=False,
        )
        if result.returncode:
            raise GateError(
                "Native regression run failed or was cancelled: " + workflow
            )
    receipt = json.loads(
        capture(
            [
                "gh",
                "run",
                "view",
                run_id,
                "--repo",
                repo,
                "--json",
                "headSha,status,conclusion,url,jobs",
            ]
        )
    )
    url = verify_receipt(receipt, commit, required_jobs)
    record(f"verified native regression receipt workflow={workflow} url={url}")


def verify_all(record, log):
    commit = local_commit()
    repo = json.loads(capture(["gh", "repo", "view", "--json", "nameWithOwner"]))[
        "nameWithOwner"
    ]
    for workflow, required_jobs in WORKFLOWS.items():
        verify_workflow(repo, commit, workflow, required_jobs, record, log)
    if local_commit() != commit:
        raise GateError("The local commit changed while native CI was being verified")


def main():
    descriptor, log_path = tempfile.mkstemp(
        prefix="ha-regression-ci-gate-", suffix=".log", dir="/tmp"
    )
    os.fchmod(descriptor, 0o600)
    started, outcome = time.monotonic(), 1
    with os.fdopen(descriptor, "w") as log:

        def record(message):
            line = f"{datetime.now(timezone.utc).isoformat()} {message} log={log_path}"
            print(line, flush=True)
            print(line, file=log, flush=True)

        record("starting")
        try:
            verify_all(record, log)
            outcome = 0
        except (GateError, subprocess.TimeoutExpired, ValueError, KeyError) as error:
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
