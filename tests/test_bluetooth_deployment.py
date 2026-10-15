# Copyright 2026 Project contributors
# Standalone host checks intentionally use unittest without a tests package.
# ruff: noqa: INP001, PT009
"""Regression checks for local Bluetooth and portable Compose startup."""

import ipaddress
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class BluetoothDeploymentTests(unittest.TestCase):
    """Verify Linux Bluetooth prerequisites and the real Compose merge result."""

    def setUp(self) -> None:
        self.compose = yaml.safe_load((ROOT / "ha.docker-compose.yaml").read_text())
        self.ha = self.compose["services"]["homeassistant"]

    def test_local_hci_uses_host_network_not_a_bridge(self) -> None:
        self.assertEqual(self.ha.get("network_mode"), "host")
        self.assertNotIn("networks", self.ha)
        self.assertNotIn("ports", self.ha)

    def test_bluetooth_capabilities_and_read_only_dbus_are_explicit(self) -> None:
        self.assertTrue({"NET_ADMIN", "NET_RAW"} <= set(self.ha.get("cap_add", [])))
        self.assertIn("/run/dbus:/run/dbus:ro", self.ha["volumes"])

    def test_host_network_keeps_dynamic_docker_dns_without_publishing_it(self) -> None:
        services = self.compose["services"]
        self.assertTrue("ha-docker-dns" in services, "Private Docker DNS is required")
        dns = services["ha-docker-dns"]
        self.assertEqual(self.ha["dns"], ["172.20.0.53"])
        self.assertEqual(dns["networks"]["ha-net"]["ipv4_address"], self.ha["dns"][0])
        self.assertNotIn("ports", dns)
        self.assertIn("ha-docker-dns", self.ha["depends_on"])
        self.assertEqual(dns["restart"], "unless-stopped")
        self.assertTrue(dns["read_only"])
        self.assertIn("@sha256:", dns["image"])
        ipam = self.compose["networks"]["ha-net"]["ipam"]["config"][0]
        self.assertIn(
            ipaddress.ip_address(self.ha["dns"][0]),
            ipaddress.ip_network(ipam["subnet"]),
        )
        self.assertNotIn(
            ipaddress.ip_address(self.ha["dns"][0]),
            ipaddress.ip_network(ipam["ip_range"]),
        )
        self.assertIn(
            "forward . 127.0.0.11", (ROOT / "docker/bluetooth/Corefile").read_text()
        )

    def test_old_machine_overlay_cannot_reintroduce_bridge_or_published_ports(
        self,
    ) -> None:
        wrapper = ROOT / "scripts/ha_compose.sh"
        self.assertTrue(
            wrapper.is_file(), "All startup paths need the portable Compose wrapper"
        )
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp) / "old-machine.yaml"
            overlay.write_text(
                'services:\n  homeassistant:\n    networks:\n      ha-net:\n        ipv4_address: 172.20.0.5\n    ports: ["21064:21064"]\n    image: ghcr.io/home-assistant/home-assistant:2026.9.3\n'
            )
            env = dict(
                os.environ,
                HA_COMPOSE_ENV_FILE=str(Path(tmp) / "absent"),
                HA_COMPOSE_OVERLAYS=str(overlay),
            )
            for key in (
                "CHATGPT_WEB_API_KEY",
                "CODE_SERVER_SUDO_PASSWORD",
                "HGA_DB_PASSWORD",
                "OPENCLAW_GATEWAY_TOKEN",
                "SSH_SERVER_PASSWORD",
            ):
                env[key] = "deployment-test-placeholder"
            result = subprocess.run(  # noqa: S603 - fixed local wrapper, no user input
                [str(wrapper), "config", "--format", "json", "--no-env-resolution"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            service = json.loads(result.stdout)["services"]["homeassistant"]
            self.assertEqual(service["network_mode"], "host")
            self.assertFalse(service.get("networks"))
            self.assertFalse(service.get("ports"))
            self.assertEqual(
                service["image"], "ghcr.io/home-assistant/home-assistant:2026.9.3"
            )

    def test_boot_unit_prepares_bluetooth_and_uses_wrapper(self) -> None:
        unit = (ROOT / "scripts/host/ha-compose.service").read_text()
        after = next(line for line in unit.splitlines() if line.startswith("After="))
        self.assertIn("bluetooth.service", after)
        self.assertIn("dbus.service", after)
        self.assertIn("ha_compose.sh up -d", unit)
        self.assertIn("ExecStartPre=+", unit)
        self.assertIn("ha_bluetooth_host.sh", unit)

    def test_missing_machine_overlay_fails_before_docker_changes(self) -> None:
        wrapper = ROOT / "scripts/ha_compose.sh"
        self.assertTrue(wrapper.is_file())
        env = dict(
            os.environ,
            HA_COMPOSE_ENV_FILE="/nonexistent/ha-env",
            HA_COMPOSE_OVERLAYS="/nonexistent/ha-machine-overlay",
        )
        result = subprocess.run(  # noqa: S603 - fixed local wrapper, no user input
            [str(wrapper), "config", "--quiet"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing Compose overlay", result.stderr)


if __name__ == "__main__":
    unittest.main()
