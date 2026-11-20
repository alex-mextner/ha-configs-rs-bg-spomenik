"""Guard the ODROID Kodi startup recovery without touching kernel memory in CI."""

from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "kodi_prestart", ROOT / "scripts/host/ha_kodi_prestart.py"
)
prestart = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prestart)
REAL_WAIT = prestart.wait_hdmi
REAL_MEMORY = prestart.read_memory


def memory(free: int) -> dict[str, int]:
    return {"CmaTotal": 262144, "CmaFree": free, "MemAvailable": 800000}


class PrestartTests(unittest.TestCase):
    """Exercise startup-only recovery admission and its single mutation bound."""

    def setUp(self) -> None:
        self.model = patch.object(
            prestart, "read_model", return_value="Hardkernel ODROID-N2Plus"
        ).start()
        self.uid = patch.object(prestart.os, "geteuid", return_value=0).start()
        self.wait = patch.object(prestart, "wait_hdmi", return_value=True).start()
        self.connected = patch.object(
            prestart, "hdmi_connected", return_value=True
        ).start()
        self.read_memory = patch.object(
            prestart, "read_memory", return_value=memory(1000)
        ).start()
        self.sync = patch.object(
            prestart.subprocess, "run", return_value=Mock(returncode=0)
        ).start()
        self.drop = patch.object(prestart, "drop_clean_pages").start()
        self.addCleanup(patch.stopall)

    def test_unknown_board_never_touches_memory(self) -> None:
        self.model.return_value = "Another board"
        self.assertEqual(prestart.prepare(), "unsupported_board")
        self.wait.assert_not_called()
        self.sync.assert_not_called()
        self.drop.assert_not_called()

    def test_nonroot_context_cannot_reclaim(self) -> None:
        self.uid.return_value = 1000
        with self.assertRaisesRegex(PermissionError, "root_unit_context_required"):
            prestart.prepare()
        self.wait.assert_not_called()
        self.drop.assert_not_called()

    def test_disconnected_projector_never_reclaims(self) -> None:
        self.wait.return_value = False
        self.assertEqual(prestart.prepare(), "hdmi_not_ready")
        self.sync.assert_not_called()
        self.drop.assert_not_called()

    def test_adequate_cma_does_not_flush_cache(self) -> None:
        self.read_memory.return_value = memory(prestart.MIN_CMA_KIB)
        self.assertEqual(prestart.prepare(), "reclaim_unneeded")
        self.sync.assert_not_called()
        self.drop.assert_not_called()

    def test_low_cma_recovers_once_after_hdmi_and_sync(self) -> None:
        events = []
        self.wait.side_effect = lambda: events.append("hdmi") or True
        self.sync.side_effect = lambda *_args, **_kwargs: events.append("sync")
        self.drop.side_effect = lambda: events.append("drop")
        self.assertEqual(prestart.prepare(), "reclaim_attempted")
        self.assertEqual(events, ["hdmi", "sync", "drop"])
        self.sync.assert_called_once_with(
            ["sync"], check=True, timeout=prestart.SYNC_TIMEOUT
        )
        self.drop.assert_called_once_with()
        self.assertEqual(self.read_memory.call_count, 3)

    def test_sync_failure_never_drops_dirty_pages(self) -> None:
        self.sync.side_effect = subprocess.TimeoutExpired("sync", 10)
        with self.assertRaises(subprocess.TimeoutExpired):
            prestart.prepare()
        self.drop.assert_not_called()

    def test_hdmi_disconnection_during_sync_cancels_reclaim(self) -> None:
        self.connected.return_value = False
        self.assertEqual(prestart.prepare(), "hdmi_disconnected")
        self.drop.assert_not_called()

    def test_memory_freed_during_sync_needs_no_drop(self) -> None:
        self.read_memory.side_effect = [memory(1000), memory(prestart.MIN_CMA_KIB)]
        self.assertEqual(prestart.prepare(), "reclaim_unneeded_after_sync")
        self.drop.assert_not_called()

    def test_insufficient_result_does_not_trigger_a_second_reclaim(self) -> None:
        self.assertEqual(prestart.prepare(), "reclaim_attempted")
        self.assertEqual(self.read_memory.return_value["CmaFree"], 1000)
        self.sync.assert_called_once()
        self.drop.assert_called_once()

    def test_wait_is_bounded_without_a_connected_display(self) -> None:
        with (
            patch.object(prestart, "hdmi_connected", return_value=False),
            patch.object(
                prestart.time, "monotonic", side_effect=[0, 0, prestart.HDMI_TIMEOUT]
            ),
            patch.object(prestart.time, "sleep") as sleep,
        ):
            # Exercise the real wait function despite the admission fixture.
            self.assertFalse(REAL_WAIT())
        sleep.assert_called_once_with(prestart.POLL_INTERVAL)

    def test_memory_schema_cannot_turn_missing_or_invalid_cma_into_pressure(
        self,
    ) -> None:
        invalid = ("MemFree: 10 kB\n", "CmaTotal: 10 kB\nCmaFree: 20 kB\n")
        for payload in invalid:
            with (
                self.subTest(payload=payload),
                patch.object(prestart.Path, "read_text", return_value=payload),
                self.assertRaisesRegex(ValueError, "invalid_cma_observation"),
            ):
                REAL_MEMORY()
        self.drop.assert_not_called()

    def test_optional_service_recovery_keeps_root_isolation_and_outer_deadline(
        self,
    ) -> None:
        unit = (ROOT / "scripts/host/kodi-memory-prestart.conf").read_text()
        self.assertIn(
            "ExecStartPre=-+/usr/bin/timeout --kill-after=2s 55s /usr/bin/python3 -I /usr/local/libexec/ha_kodi_prestart.py",
            unit,
        )
        self.assertNotIn("ExecStart=", unit)
        self.assertNotIn("Restart=", unit)
