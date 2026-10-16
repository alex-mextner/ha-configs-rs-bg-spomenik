# Copyright 2026 Project contributors
# ruff: noqa: INP001, PT009
"""The Compose boot unit recovers transient dependency failures without a restart loop."""

import configparser
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BootRetryTests(unittest.TestCase):
    """Keep successful startup stable and transient failure recovery finite."""

    def test_transient_failure_retries_but_success_stays_active(self) -> None:
        """Retry only unsuccessful starts with finite backoff and limits."""
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(ROOT / "scripts/host/ha-compose.service")
        self.assertEqual(unit["Service"]["Type"], "oneshot")
        self.assertEqual(unit["Service"]["RemainAfterExit"], "yes")
        self.assertEqual(unit["Service"].get("Restart"), "on-failure")
        self.assertEqual(unit["Service"].get("RestartSec"), "30s")
        self.assertEqual(unit["Unit"].get("StartLimitIntervalSec"), "infinity")
        self.assertEqual(unit["Unit"].get("StartLimitBurst"), "5")

    def test_retry_preserves_dns_gate_and_normal_stop(self) -> None:
        """Keep DNS validation and explicit stop behavior unchanged."""
        wrapper = (ROOT / "scripts/ha_compose.sh").read_text()
        self.assertIn("--wait --wait-timeout 90", wrapper)
        unit = (ROOT / "scripts/host/ha-compose.service").read_text()
        self.assertIn("ExecStop=@HA_ROOT@/scripts/ha_compose.sh stop", unit)
        self.assertNotIn("Restart=always", unit)

    def test_slow_prestart_and_startup_cannot_reset_attempt_budget(self) -> None:
        """A total attempt cap is independent of per-command timeout durations."""
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(ROOT / "scripts/host/ha-compose.service")
        self.assertEqual(unit["Unit"].get("StartLimitIntervalSec"), "infinity")
        self.assertEqual(unit["Unit"].get("StartLimitBurst"), "5")


if __name__ == "__main__":
    unittest.main()
