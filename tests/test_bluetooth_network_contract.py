# Copyright 2026 Project contributors
# ruff: noqa: INP001, PT009, PT027
"""Host-network migration keeps DNS readiness and maintenance routing intact."""

import ast
import importlib.util
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class NetworkContractTests(unittest.TestCase):
    """Exercise the deployment contracts changed by the Bluetooth migration."""

    def test_network_validation_rejects_drift_and_reserved_address_collision(
        self,
    ) -> None:
        path = ROOT / "scripts/ha_bluetooth_network_check.py"
        self.assertTrue(
            path.is_file(), "Network preflight is required before migration"
        )
        spec = importlib.util.spec_from_file_location(
            "ha_bluetooth_network_check", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        desired = {
            "Subnet": "172.20.0.0/16",
            "Gateway": "172.20.0.1",
            "IPRange": "172.20.128.0/17",
        }
        existing = {"IPAM": {"Config": [desired]}, "Containers": {}}
        module.validate_network(
            desired, existing, "172.20.0.53", "homeassistant-ha-docker-dns-1"
        )
        drift = {"IPAM": {"Config": [{**desired, "IPRange": ""}]}, "Containers": {}}
        with self.assertRaisesRegex(ValueError, "IPAM"):
            module.validate_network(
                desired, drift, "172.20.0.53", "homeassistant-ha-docker-dns-1"
            )
        occupied = {
            **existing,
            "Containers": {
                "id": {"Name": "other-service", "IPv4Address": "172.20.0.53/16"}
            },
        }
        with self.assertRaisesRegex(ValueError, "already occupied"):
            module.validate_network(
                desired, occupied, "172.20.0.53", "homeassistant-ha-docker-dns-1"
            )
        occupied["Containers"]["id"]["Name"] = "homeassistant-ha-docker-dns-1"
        module.validate_network(
            desired, occupied, "172.20.0.53", "homeassistant-ha-docker-dns-1"
        )

    def test_host_search_suffix_does_not_rewrite_docker_service_names(self) -> None:
        config = yaml.safe_load((ROOT / "ha.docker-compose.yaml").read_text())
        self.assertEqual(config["services"]["homeassistant"].get("dns_search"), ["."])

    def test_dns_is_healthy_before_homeassistant_starts(self) -> None:
        config = yaml.safe_load((ROOT / "ha.docker-compose.yaml").read_text())
        services = config["services"]
        self.assertEqual(
            services["homeassistant"]["depends_on"]["ha-docker-dns"]["condition"],
            "service_healthy",
        )
        probe = services["ha-docker-dns"].get("healthcheck", {}).get("test", [])
        self.assertIn("nslookup", " ".join(probe))
        self.assertIn("ha-docker-dns.", probe)
        self.assertIn("127.0.0.1", probe)
        build = services["ha-docker-dns"].get("build")
        self.assertIsNotNone(build, "Health probe must exist in the pinned DNS image")

    def test_bridge_services_keep_both_legacy_homeassistant_names(self) -> None:
        config = yaml.safe_load((ROOT / "ha.docker-compose.yaml").read_text())
        for name, service in config["services"].items():
            if name == "ha-docker-dns" or "ha-net" not in service.get("networks", []):
                continue
            with self.subTest(service=name):
                hosts = service.get("extra_hosts", [])
                self.assertIn("homeassistant:host-gateway", hosts)
                self.assertIn("homeassistant.local:host-gateway", hosts)

    def test_voice_maintenance_uses_the_machine_overlay_wrapper(self) -> None:
        source = (ROOT / "scripts/host/ha-host-actions.py").read_text()
        tree = ast.parse(source)
        selected = [
            node
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(child, ast.Name) and child.id == "COMPOSE"
                for child in ast.walk(node)
            )
        ]
        self.assertEqual(
            ast.literal_eval(selected[0].value),
            ["/home/ultra/homeassistant/scripts/ha_compose.sh"],
        )

    def test_wrapper_does_not_depend_on_callers_working_directory(self) -> None:
        wrapper = (ROOT / "scripts/ha_compose.sh").read_text()
        self.assertIn('cd -- "$ROOT"', wrapper)

    def test_network_preflight_precedes_mutating_compose(self) -> None:
        wrapper = (ROOT / "scripts/ha_compose.sh").read_text()
        self.assertIn("ha_bluetooth_network_check.py", wrapper)
        self.assertLess(
            wrapper.index("ha_bluetooth_network_check.py"),
            wrapper.index("exec docker compose"),
        )


if __name__ == "__main__":
    unittest.main()
