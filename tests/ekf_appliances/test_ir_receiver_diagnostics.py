"""Exercise the installed Tuya diagnostics consumer with the EKF observer."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import homeassistant
from custom_components.ekf_appliances.ir_receiver import IrReceiver


def native_diagnostics():
    root = next(p for p in [Path(__file__).parent, *Path(__file__).parents]
                if (p / "custom_components/tuya_local/diagnostics.py").is_file())
    source = ast.parse((root / "custom_components/tuya_local/diagnostics.py").read_text())
    wanted = [node for node in source.body
              if isinstance(node, ast.FunctionDef) and node.name in {"redact_entity", "redact_dps"}]
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *wanted],
        type_ignores=[],
    )
    namespace = {"REDACTED": "**REDACTED**"}
    exec(compile(ast.fix_missing_locations(module), "installed_tuya_diagnostics", "exec"), namespace)
    return namespace


class TestObserverDiagnostics(unittest.TestCase):
    def setUp(self):
        hass = SimpleNamespace(bus=SimpleNamespace(async_listen=lambda *_: lambda: None))
        runtime = SimpleNamespace(hass=hass, entry=SimpleNamespace(
            entry_id="observer-test", data={"remote_entity_id": "remote.ekf"}))
        with patch("custom_components.ekf_appliances.ir_receiver.Store"):
            self.receiver = IrReceiver(runtime)
        self.helpers = native_diagnostics()
        self.native_dp = SimpleNamespace(id="101", name="secret", sensitive=True,
                                         get_value=lambda _: "private")
        self.native = SimpleNamespace(_config=SimpleNamespace(
            unique_id=lambda _: "native-remote", dps=lambda: [self.native_dp]))
        self.device = SimpleNamespace(unique_id="test-hub", _children=[self.native, self.receiver])

    def test_old_observer_contract_reproduces_actual_diagnostics_failure(self):
        self.receiver._config = SimpleNamespace(dps=lambda: [])
        with self.assertRaises(AttributeError):
            self.helpers["redact_entity"](
                self.device, "native-remote", {"state": "on", "attributes": {}})

    def test_device_diagnostics_work_and_keep_native_redaction(self):
        result = self.helpers["redact_entity"](
            self.device, "native-remote",
            {"state": "on", "attributes": {"secret": "private"}, "context": {"id": "unused"}})
        self.assertEqual(result, {"state": "on", "attributes": {"secret": "**REDACTED**"}})

    def test_configuration_diagnostics_keep_sensitive_dps_redacted(self):
        result = self.helpers["redact_dps"](
            self.device, {"101": "private", "201": '{"control":"study_exit"}'})
        self.assertEqual(result["101"], "**REDACTED**")
        self.assertEqual(result["201"], '{"control":"study_exit"}')

    def test_observer_never_matches_native_registry_entity(self):
        self.assertNotEqual(self.receiver._config.unique_id(self.device.unique_id), "native-remote")


if __name__ == "__main__":
    unittest.main()
