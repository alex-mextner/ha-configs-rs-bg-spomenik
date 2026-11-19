"""Reconcile one delayed native Open without extending transport or Stop waits."""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import threading
import types
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import patch

import test_rotation as fixtures

ROOT = Path(__file__).resolve().parents[2]


class NativeStartupBudgetCases(fixtures.FixtureBase):
    """Execute the real bridge transport, rotation and repeat owner together."""

    def setUp(self) -> None:
        super().setUp()
        self.clock = types.SimpleNamespace(now=100.0)
        self.fake_time = types.SimpleNamespace(monotonic=lambda: self.clock.now)
        self.visible_after = 12.0
        self.open_mode = "ok"
        self.read_failures = 0
        self.opened_at = None
        self.queued_file = None
        self.transports = []
        self.events = []
        self.tick = lambda: None
        for module in (fixtures.fireplace, fixtures.fireplace._rotation):
            time_patch = patch.object(module, "time", self.fake_time)
            time_patch.start()
            self.addCleanup(time_patch.stop)
        self.bridge_rpc = self.load_bridge_rpc()

    def audit(self, stage: str, **fields: Any) -> None:
        self.events.append({"stage": stage, **fields})

    def load_bridge_rpc(self) -> Any:
        path = ROOT / "scripts/host/ha-host-actions.py"
        tree = ast.parse(path.read_text(), filename=str(path))
        selected = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_fireplace_rpc"
        ]
        if selected:
            module = types.ModuleType("native_startup_bridge")
            module.Any = Any
            module.time = self.fake_time
            module.urllib = urllib
            module.FireplaceError = fixtures.fireplace.FireplaceError
            module._fireplace_module = fixtures.fireplace
            module.kodi_jsonrpc = self.jsonrpc
            module.logging = types.SimpleNamespace(
                getLogger=lambda _name: types.SimpleNamespace(
                    info=lambda template, *args: self.audit(template % args)
                )
            )
            module.__spec__ = importlib.util.spec_from_file_location(
                module.__name__, path
            )
            code = compile(
                ast.Module(body=selected, type_ignores=[]), str(path), "exec"
            )
            exec(code, module.__dict__)  # noqa: S102 - exact production transport only
            return module._fireplace_rpc
        path = ROOT / "scripts/host/ha_host_fireplace.py"
        spec = importlib.util.spec_from_file_location("native_startup_endpoint", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.time = self.fake_time
        module._fireplace_module = fixtures.fireplace
        module.FireplaceError = fixtures.fireplace.FireplaceError
        return module.FireplaceEndpoints(
            self.jsonrpc, self.root, ValueError, audit=self.audit
        ).rpc

    def jsonrpc(self, method, params, *, timeout: float) -> dict[str, Any]:
        self.transports.append((method, timeout, self.clock.now))
        self.clock.now += 0.01
        if method == "Player.Open":
            self.kodi.calls.append((method, dict(params)))
            if self.open_mode == "http_rejected":
                private_url = "https://private.invalid/?secret=value"
                raise urllib.error.HTTPError(
                    private_url,
                    403,
                    "denied",
                    {},
                    io.BytesIO(),
                )
            if self.open_mode == "rejected":
                return {"ok": True, "body": {"error": {"code": -32602}}}
            self.opened_at, self.queued_file = self.clock.now, params["item"]["file"]
            if self.open_mode == "timeout":
                self.clock.now += timeout / 2
                self.tick()
                self.clock.now += timeout / 2
                message = "https://private.invalid/?secret=value"
                raise TimeoutError(message)
            return {"ok": True, "body": {"result": "OK"}}
        self.tick()
        if (
            self.opened_at is not None
            and self.clock.now >= self.opened_at + self.visible_after
        ):
            self.kodi.file, self.kodi.speed = self.queued_file, 1
        if (
            method == "Player.GetActivePlayers"
            and self.opened_at is not None
            and self.read_failures
        ):
            self.read_failures -= 1
            self.clock.now += timeout
            message = "private read timeout"
            raise TimeoutError(message)
        result = self.kodi.rpc(method, params, self.clock.now + timeout)
        if method == "Player.GetProperties":
            elapsed = max(
                0.0,
                self.clock.now
                - (self.opened_at or self.clock.now)
                - self.visible_after,
            )
            result["time"] = {
                "hours": 0,
                "minutes": 0,
                "seconds": int(elapsed),
                "milliseconds": round(elapsed % 1 * 1000),
            }
        return {"ok": True, "body": {"result": result}}

    def controller(self, **options: Any) -> Any:
        controller = fixtures.fireplace.FireplaceController(
            self.bridge_rpc,
            self.root / "owner.json",
            media_path=self.media,
            expected_sha256=self.sha,
            auto_monitor=False,
            **options,
        )
        self.controllers.append(controller)
        for event in (controller._closing, controller._owner._closing):
            wait_patch = patch.object(event, "wait", side_effect=self.wait_tick)
            wait_patch.start()
            self.addCleanup(wait_patch.stop)
        return controller

    def wait_tick(self, timeout: float) -> bool:
        self.clock.now += timeout
        self.tick()
        return False

    def assert_one_open(self) -> None:
        self.assertEqual(
            [method for method, _, _ in self.transports].count("Player.Open"), 1
        )

    def assert_playing(self, controller) -> None:
        try:
            proof = controller.start("delayed-native-start")
        except fixtures.fireplace.FireplaceError as error:
            self.fail("The single delayed native Open was rejected: " + str(error))
        self.assertEqual(proof["state"], "playing")
        self.assertTrue(
            proof["owned"] and proof["file_matches"] and proof["progressing"]
        )
        self.assertEqual(proof["repeat"], "one")
        self.assertEqual(proof["request_id"], "delayed-native-start")
        self.assert_one_open()

    def test_cold_plugin_visible_after_twelve_seconds_can_play(self) -> None:
        self.assert_playing(self.controller())
        self.assertGreater(self.clock.now, 112)
        self.assertLess(self.clock.now, 145)

    def test_ambiguous_open_ack_reconciles_reads_without_resending(self) -> None:
        self.open_mode = "timeout"
        self.assert_playing(self.controller())
        self.assertGreater(self.clock.now, 112)
        self.assertLess(self.clock.now, 145)

    def test_transient_startup_reads_do_not_cancel_the_open(self) -> None:
        self.read_failures = 2
        self.assert_playing(self.controller())
        self.assertEqual(self.read_failures, 0)

    def test_shared_six_second_budget_still_limits_startup(self) -> None:
        controller = self.controller(operation_timeout=6)
        with self.assertRaises(fixtures.fireplace.FireplaceError):
            controller.start("short-shared-deadline")
        self.assert_one_open()
        self.assertLessEqual(self.clock.now, 106.2)
        self.assertFalse(
            any(method == "Player.SetRepeat" for method, _, _ in self.transports)
        )

    def test_unconfirmed_startup_expires_without_a_second_open(self) -> None:
        self.visible_after = 100
        controller = self.controller()
        with self.assertRaises(fixtures.fireplace.FireplaceError):
            controller.start("native-never-ready")
        self.assert_one_open()
        self.assertGreaterEqual(self.clock.now, 145)
        self.assertLess(self.clock.now, 146)
        self.assertFalse(
            any(method == "Player.SetRepeat" for method, _, _ in self.transports)
        )

    def cancel_during_start(self, *, at: float) -> None:
        controller = self.controller()
        stopper = None
        outcomes = []
        cancellation = []

        def stop() -> None:
            try:
                outcomes.append(controller.stop())
            except fixtures.fireplace.FireplaceError as error:
                outcomes.append(str(error))
            cancellation.append(self.clock.now)

        def tick() -> None:
            nonlocal stopper
            if self.clock.now >= at and stopper is None:
                cancellation.append(self.clock.now)
                stopper = threading.Thread(target=stop)
                stopper.start()
                self.assertTrue(controller._cancelled.wait(1))

        self.tick = tick
        try:
            with self.assertRaisesRegex(fixtures.fireplace.FireplaceError, "cancelled"):
                controller.start("cancel-delayed-native")
        finally:
            if stopper is not None:
                stopper.join(timeout=2)
        self.assertIsNotNone(stopper)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(cancellation), 2)
        self.assertLessEqual(cancellation[-1] - cancellation[0], 8.2)
        self.assert_one_open()
        self.assertFalse(
            any(method == "Player.SetRepeat" for method, _, _ in self.transports)
        )

    def test_stop_interrupts_extended_confirmation_without_repeat_mutation(
        self,
    ) -> None:
        self.visible_after = 100
        self.cancel_during_start(at=110)

    def test_stop_during_ambiguous_ack_keeps_existing_transport_bound(self) -> None:
        self.visible_after = 100
        self.open_mode = "timeout"
        self.cancel_during_start(at=104)

    def test_cancel_before_repeat_dispatch_drops_undispatched_arm(self) -> None:
        self.visible_after = 0
        controller = self.controller()
        callback = controller._owner._rpc_callback

        def cancel_before_dispatch(method, params, deadline) -> Any:
            if method == "Player.SetRepeat":
                controller._cancelled.set()
            return callback(method, params, deadline)

        controller._owner._rpc_callback = cancel_before_dispatch
        with self.assertRaisesRegex(fixtures.fireplace.FireplaceError, "cancelled"):
            controller.start("cancel-before-repeat")
        self.assertFalse((self.root / "owner.json").exists())
        self.assert_one_open()
        self.assertFalse(
            any(method == "Player.SetRepeat" for method, _, _ in self.transports)
        )

    def test_definite_jsonrpc_rejection_is_not_reconciled_as_success(self) -> None:
        self.open_mode = "rejected"
        with self.assertRaises(fixtures.fireplace.FireplaceError):
            self.controller().start("definite-rejection")
        self.assert_one_open()
        self.assertLess(self.clock.now, 101)

    def test_definite_http_rejection_is_not_transport_ambiguity(self) -> None:
        self.open_mode = "http_rejected"
        with self.assertRaises(fixtures.fireplace.FireplaceError):
            self.controller().start("http-rejection")
        self.assert_one_open()
        self.assertLess(self.clock.now, 101)

    def test_pre_dispatch_deadline_sends_nothing(self) -> None:
        with self.assertRaises(fixtures.fireplace.RpcNotDispatchedError):
            self.bridge_rpc(
                "Player.Open", {"item": {"file": str(self.media)}}, self.clock.now
            )
        self.assertEqual(self.transports, [])

    def test_open_stop_and_read_transport_caps_are_preserved(self) -> None:
        for method, params in (
            ("Player.Open", {"item": {"file": str(self.media)}}),
            ("Player.Stop", {"playerid": 1}),
            ("Player.GetActivePlayers", {}),
        ):
            self.bridge_rpc(method, params, self.clock.now + 100)
        self.assertEqual(
            [timeout for _, timeout, _ in self.transports], [8.0, 8.0, 1.0]
        )

    def test_transport_diagnostics_redact_private_error_and_payload(self) -> None:
        self.open_mode = "timeout"
        self.assert_playing(self.controller())
        text = json.dumps(self.events)
        self.assertIn("TimeoutError", text)
        self.assertIn("Player.Open", text)
        self.assertNotIn("private.invalid", text)
        self.assertNotIn("secret", text)
        self.assertNotIn(str(self.media), text)
