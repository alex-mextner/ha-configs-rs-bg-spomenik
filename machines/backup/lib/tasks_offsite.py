#!/usr/bin/python3 -B
# ruff: noqa: T201, S603, CPY001  # a CLI prints; it runs only rclone
"""
Off-site copy of the Tasks backups: the encrypted sets the Tasks puller verified.

The puller (tasks_backup.pull, installed in ~/.local/lib/tasks-backup) keeps
<source>/<RUNID>/ directories that are complete and manifest-verified (a run is
renamed from .partial-<RUNID> only after that). This copies them unchanged, still
age-encrypted, to <remote>/<RUNID>/ and applies the puller's own retention there
(select_keep with its keep_recent / keep_daily_days defaults), so Google Drive
holds what the puller holds plus nothing older. Runs the puller still keeps
locally are never removed remotely, and nothing is removed when the copy failed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from tasks_backup.common import RUN_ID_RE, select_keep
from tasks_backup.pull import Config

# rclone filter for <RUNID>/ directories only: skips state.json and the
# puller's .partial-* work directories
DIGIT = "[0-9]"
RUN_FILTER = f"/{DIGIT * 8}T{DIGIT * 6}Z/**"


def rclone(program: str, *args: str) -> str:
    """Run rclone; raise on failure, return stdout (stderr goes to the journal)."""
    return subprocess.run(
        [program, *args], check=True, stdout=subprocess.PIPE, text=True
    ).stdout


def main() -> int:
    """Copy new runs, then prune the remote runs the puller's retention drops."""
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--remote", required=True, help="gdrive:homelab-backups/tasks")
    parser.add_argument("--rclone", default="rclone")
    args = parser.parse_args()

    local = sorted(
        p.name for p in args.source.iterdir() if p.is_dir() and RUN_ID_RE.match(p.name)
    )
    if not local:
        print(f"tasks-offsite: no runs in {args.source}", file=sys.stderr)
        return 1
    rclone(args.rclone, "copy", str(args.source), args.remote, "--include", RUN_FILTER)
    listing = rclone(args.rclone, "lsf", "--dirs-only", args.remote).splitlines()
    remote = sorted(n.rstrip("/") for n in listing if RUN_ID_RE.match(n.rstrip("/")))
    missing = sorted(set(local) - set(remote))
    if missing:
        print(f"tasks-offsite: missing after copy: {missing}", file=sys.stderr)
        return 1
    keep = select_keep(
        remote, datetime.now(UTC), Config.keep_recent, Config.keep_daily_days
    )
    dropped = sorted(set(remote) - keep - set(local))
    for run in dropped:
        rclone(args.rclone, "purge", f"{args.remote}/{run}")
    print(
        f"tasks-offsite: {len(local)} local runs on {args.remote}; "
        f"{len(remote) - len(dropped)} kept there, {len(dropped)} expired"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
