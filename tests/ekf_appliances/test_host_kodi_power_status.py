"""Real shutdown regression: terminated Kodi can leave a failed systemd unit."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ProbeOptions:
    """Configure independent process output and a missing supervisor identifier."""

    process_code: int = 1
    process_stdout: str = ""
    missing_pid: bool = False


class KodiPowerFeedbackTests(unittest.TestCase):
    """Verify process presence independently of supervisor failure status."""

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location(
            "host_kodi_power_feedback", ROOT / "scripts/host/ha-host-actions.py"
        )
        cls.host = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(ROOT / "scripts/host"), *sys.path]):
            spec.loader.exec_module(cls.host)

    def status(
        self,
        active="failed",
        pid="0",
        exit_status="143",
        unit_result="exit-code",
        *,
        probe: ProbeOptions | None = None,
    ) -> tuple[dict[str, Any], list[list[str]]]:
        probe = probe or ProbeOptions()
        text = (
            "LoadState=loaded\nActiveState="
            + active
            + "\nSubState="
            + ("failed" if active == "failed" else "dead")
            + "\nResult="
            + unit_result
            + "\nExecMainStatus="
            + exit_status
            + "\n"
        )
        if not probe.missing_pid:
            text += "MainPID=" + pid + "\n"
        calls = []

        def run(args, timeout) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args[:2] == ["systemctl", "show"]:
                return subprocess.CompletedProcess(args, 0, text, "")
            self.assertEqual(args, ["pgrep", "-u", str(os.getuid()), "-x", "kodi.bin"])
            self.assertEqual(timeout, 2)
            return subprocess.CompletedProcess(
                args, probe.process_code, probe.process_stdout, ""
            )

        with patch.object(self.host, "run", side_effect=run):
            result = self.host.kodi_status()
        return result, calls

    def test_real_sigterm_exit143_with_confirmed_process_absence_is_off_but_keeps_failure(
        self,
    ) -> None:
        result, calls = self.status()
        self.assertEqual(result["state"], "off")
        self.assertFalse(result["process_present"])
        self.assertEqual(result["active_state"], "failed")
        self.assertEqual(result["unit_result"], "exit-code")
        self.assertEqual(result["exit_status"], 143)
        self.assertEqual(len(calls), 2)

    def test_crash_diagnostic_remains_distinct_from_confirmed_physical_absence(
        self,
    ) -> None:
        result, _ = self.status(exit_status="11", unit_result="signal")
        self.assertEqual(result["state"], "off")
        self.assertEqual((result["unit_result"], result["exit_status"]), ("signal", 11))

    def test_failed_or_inactive_with_remaining_kodi_process_is_not_off(self) -> None:
        for active in ("failed", "inactive"):
            with self.subTest(active=active):
                result, _ = self.status(
                    active=active,
                    probe=ProbeOptions(process_code=0, process_stdout="12345\n"),
                )
                self.assertEqual(result["state"], "unknown")
                self.assertTrue(result["process_present"])

    def test_probe_failure_or_missing_or_malformed_main_pid_cannot_become_off(
        self,
    ) -> None:
        for kwargs in (
            {"probe": ProbeOptions(process_code=2)},
            {"probe": ProbeOptions(missing_pid=True)},
            {"pid": "bad"},
            {"pid": "-1"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError):
                self.status(**kwargs)

    def test_transitional_unit_without_process_remains_unknown(self) -> None:
        for active in ("activating", "deactivating", "reloading"):
            with self.subTest(active=active):
                result, _ = self.status(active=active)
                self.assertEqual(result["state"], "unknown")
