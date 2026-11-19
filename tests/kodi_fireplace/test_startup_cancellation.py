"""Cancel a pending Kodi unit start through the existing projector-off route."""

from __future__ import annotations

import ast
import os
import subprocess
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = Path(
    os.environ.get(
        "KODI_STARTUP_TEST_BRIDGE", str(ROOT / "scripts/host/ha-host-actions.py")
    )
)


def load_stop_functions() -> types.ModuleType:
    """Exercise exact source functions, including an explicit rollout overlay."""
    tree = ast.parse(BRIDGE.read_text(), filename=str(BRIDGE))
    required = {"kodi_running", "stop_kodi", "projector_off"}
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in required | {"_kodi_active_state"}
    ]
    if not required.issubset({node.name for node in selected}):
        message = "bridge stop functions missing"
        raise AssertionError(message)
    module = types.ModuleType("startup_cancellation_bridge")
    module.Any = Any
    module.KODI_UNIT = "kodi.service"
    code = compile(ast.Module(body=selected, type_ignores=[]), str(BRIDGE), "exec")
    exec(code, module.__dict__)  # noqa: S102 - execute only exact selected source functions
    helper_path = BRIDGE.with_name("ha_host_fireplace.py")
    if helper_path.exists():
        helper = types.ModuleType("startup_cancellation_helpers")
        helper_tree = ast.parse(helper_path.read_text(), filename=str(helper_path))
        helper_nodes = [
            node
            for node in helper_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"stop_kodi", "_kodi_active_state"}
        ]
        helper_code = compile(
            ast.Module(body=helper_nodes, type_ignores=[]), str(helper_path), "exec"
        )
        exec(helper_code, helper.__dict__)  # noqa: S102 - exact selected source functions
        module._fireplace_http = helper
    return module


class UnitFixture:
    """Represent systemd phase transitions without executing host commands."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.calls = []
        self.stop_result = 0
        self.after_stop = "inactive"
        self.show_result = 0
        self.before_show = None

    def run(self, argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, timeout))
        if argv == ["systemctl", "is-active", "--quiet", "kodi.service"]:
            result = 0 if self.state in {"active", "reloading"} else 3
            return subprocess.CompletedProcess(argv, result, "", "")
        if argv == [
            "systemctl",
            "show",
            "kodi.service",
            "--property=ActiveState",
            "--value",
        ]:
            if self.before_show is not None:
                self.state, self.before_show = self.before_show, None
            return subprocess.CompletedProcess(
                argv, self.show_result, self.state + "\n", ""
            )
        if argv == ["sudo", "-n", "systemctl", "stop", "kodi.service"]:
            self.state = self.after_stop
            return subprocess.CompletedProcess(argv, self.stop_result, "", "")
        message = "unexpected host command"
        raise AssertionError(message)

    def quit(self) -> dict[str, bool]:
        self.state = "inactive"
        return {"ok": True}


class StartupCancellationTests(unittest.TestCase):
    """Preserve graceful active shutdown while fencing an unfinished unit start."""

    def setUp(self) -> None:
        self.host = load_stop_functions()
        self.unit = UnitFixture("activating")
        self.host.run = self.unit.run
        self.host.kodi_jsonrpc_quit = Mock(side_effect=self.unit.quit)
        self.host.time = types.SimpleNamespace(monotonic=lambda: 0, sleep=Mock())
        if hasattr(self.host, "_fireplace_http"):
            self.host._fireplace_http.time = self.host.time

    def stop_calls(self) -> list:
        return [
            entry
            for entry in self.unit.calls
            if entry[0] == ["sudo", "-n", "systemctl", "stop", "kodi.service"]
        ]

    def test_projector_off_cancels_activating_unit_without_jsonrpc(self) -> None:
        result = self.host.projector_off()["kodi"]
        self.assertEqual(result["kodi"], "stopped")
        self.assertEqual(result["method"], "systemctl_stop")
        self.assertEqual(self.unit.state, "inactive")
        self.assertEqual(len(self.stop_calls()), 1)
        self.host.kodi_jsonrpc_quit.assert_not_called()
        self.host.time.sleep.assert_not_called()

    def test_still_activating_is_not_reported_as_stopped(self) -> None:
        self.unit.stop_result = 1
        self.unit.after_stop = "activating"
        result = self.host.stop_kodi()
        self.assertEqual(result["kodi"], "stop_requested")
        self.assertEqual(result["systemctl_returncode"], 1)
        self.assertEqual(len(self.stop_calls()), 1)
        self.host.kodi_jsonrpc_quit.assert_not_called()

    def test_active_unit_keeps_existing_graceful_shutdown(self) -> None:
        self.unit.state = "active"
        result = self.host.stop_kodi()
        self.assertEqual(result["kodi"], "stopped")
        self.assertEqual(result["method"], "jsonrpc")
        self.host.kodi_jsonrpc_quit.assert_called_once_with()
        self.assertEqual(self.stop_calls(), [])
        self.assertFalse(any(argv[1] == "show" for argv, _ in self.unit.calls))

    def test_start_completing_during_status_read_uses_graceful_shutdown(self) -> None:
        self.unit.before_show = "active"
        result = self.host.stop_kodi()
        self.assertEqual(result["kodi"], "stopped")
        self.assertEqual(result["method"], "jsonrpc")
        self.host.kodi_jsonrpc_quit.assert_called_once_with()
        self.assertEqual(self.stop_calls(), [])

    def test_inactive_and_failed_units_remain_noop(self) -> None:
        for state in ("inactive", "failed"):
            with self.subTest(state=state):
                self.unit.state = state
                self.assertEqual(self.host.stop_kodi(), {"kodi": "not_running"})
        self.assertEqual(self.stop_calls(), [])
        self.host.kodi_jsonrpc_quit.assert_not_called()

    def test_unreadable_activation_state_does_not_claim_stopped(self) -> None:
        self.unit.show_result = 1
        with self.assertRaisesRegex(RuntimeError, "Kodi unit state could not be read"):
            self.host.stop_kodi()
        self.assertEqual(self.stop_calls(), [])
        self.host.kodi_jsonrpc_quit.assert_not_called()
