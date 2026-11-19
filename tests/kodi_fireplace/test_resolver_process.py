"""Exercise resolver cancellation against a real subprocess tree in CI."""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "rotation_process", ROOT / "scripts/host/ha_kodi_fireplace_rotation.py"
)
rotation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rotation)


def is_running(pid) -> bool:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        return fields[2] not in {"Z", "X"}
    except FileNotFoundError:
        return False


class ResolverProcessTests(unittest.TestCase):
    """Exercise resolver process tests behavior with deterministic native fixtures."""

    def test_spawn_failure_is_a_preparation_failure(self) -> None:
        with (
            patch.object(
                rotation.subprocess, "Popen", side_effect=OSError("missing executable")
            ),
            self.assertRaisesRegex(rotation.SourceUnavailable, "youtube_unavailable"),
        ):
            rotation.resolve_source(
                {"kind": "youtube", "youtube_id": "L_LUpnjgPso"},
                time.monotonic() + 1,
                "attempt",
            )

    def test_unreaped_process_is_not_reported_as_success(self) -> None:
        process = Mock(pid=12345)
        process.wait.side_effect = subprocess.TimeoutExpired("helper", 2)
        with (
            patch.object(rotation.os, "killpg"),
            self.assertRaisesRegex(
                rotation.SourceUnavailable, "resolver_cleanup_unconfirmed"
            ),
        ):
            rotation.reap_resolver(process)

    def test_cancelled_resolver_terminates_and_reaps_its_process_group(self) -> None:
        cancelled, spawned = threading.Event(), threading.Event()
        errors, processes = [], []
        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            code = (
                "import pathlib,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                "pending=pathlib.Path(sys.argv[1]+'.tmp')\n"
                "pending.write_text(str(child.pid))\n"
                "pending.replace(sys.argv[1])\n"
                "time.sleep(60)\n"
            )

            def spawn(_command, **options: Any) -> Any:
                process = real_popen(
                    [sys.executable, "-c", code, str(pid_file)], **options
                )
                processes.append(process)
                spawned.set()
                return process

            def resolve() -> None:
                try:
                    rotation.resolve_source(
                        {"kind": "youtube", "youtube_id": "L_LUpnjgPso"},
                        time.monotonic() + 5,
                        "attempt",
                        cancelled,
                    )
                except rotation.SourceUnavailable as error:
                    errors.append(str(error))

            with patch.object(rotation.subprocess, "Popen", side_effect=spawn):
                worker = threading.Thread(target=resolve)
                worker.start()
                try:
                    self.assertTrue(spawned.wait(1))
                    deadline = time.monotonic() + 2
                    while not pid_file.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(pid_file.exists(), "Resolver child never started")
                    child_pid = int(pid_file.read_text())
                    cancelled.set()
                    worker.join(timeout=3)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(errors, ["youtube_deadline_or_cancelled"])
                    self.assertIsNotNone(processes[0].returncode)
                    deadline = time.monotonic() + 1
                    while is_running(child_pid) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertFalse(
                        is_running(child_pid),
                        "Runtime child survived resolver cancellation",
                    )
                finally:
                    cancelled.set()
                    for process in processes:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
                    worker.join(timeout=3)
