"""
External Kodi21.2 EventServer/JSON-RPC contracts for the local text bridge.

Wire constants are from Kodi21.2-Omega EventPacket.h, EventClient.cpp and
InputManager.cpp: PT_BUTTON=3; PTB_UNICODE=0x200; queued, nonrepeating down.
The transport is replaced at the socket boundary, never by a fake helper that
returns success. No Kodi process, network device or production service is used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


import contextlib
import importlib.util
import io
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "ha_host_actions_t9", ROOT / "scripts/host/ha-host-actions.py"
)
HOST = importlib.util.module_from_spec(SPEC)
with patch.object(sys, "path", [str(ROOT / "scripts/host"), *sys.path]):
    SPEC.loader.exec_module(HOST)


class KodiTextBridgeTests(unittest.TestCase):
    """Verify text edit ordering, wire bytes and privacy at inert boundaries."""

    def setUp(self) -> None:
        self.text, self.cursor, self.window = "preFIX", 3, 10103
        self.enabled, self.port, self.running, self.listening = True, 9777, True, True
        self.drop = False
        self.screensaver, self.wake_works = False, True
        HOST._KODI_TEXT_CONTEXT = None
        self.packets, self.rpc_calls = [], []

    def endpoint(self) -> Callable[[dict[str, Any]], dict[str, Any]]:
        fn = getattr(HOST, "kodi_text_input", None)
        self.assertTrue(
            callable(fn), "The authenticated Kodi text endpoint is not implemented"
        )
        return fn

    def rpc(self, method, params=None, timeout=5) -> dict[str, Any]:
        self.rpc_calls.append((method, params))
        if method == "GUI.GetProperties":
            result = {"currentwindow": {"id": self.window, "label": "Keyboard"}}
        elif method == "XBMC.GetInfoLabels":
            self.assertEqual(params["labels"], ["Control.GetLabel(312).index(1)"])
            result = {"Control.GetLabel(312).index(1)": self.text}
        elif method == "XBMC.GetInfoBooleans":
            self.assertEqual(params["booleans"], ["System.ScreenSaverActive"])
            result = {"System.ScreenSaverActive": self.screensaver}
        elif method == "Settings.GetSettingValue":
            result = {
                "value": {
                    "services.esenabled": self.enabled,
                    "services.esport": self.port,
                }[params["setting"]]
            }
        elif method == "Input.ExecuteAction":
            return self.execute_action(params)
        else:
            raise AssertionError("Unexpected RPC call: " + method)
        return {"ok": True, "status": 200, "body": {"result": result}}

    def execute_action(self, params) -> dict[str, Any]:
        """Model the real keyboard action and screensaver consumption boundary."""
        self.assertIn(params["action"], ("enter", "backspace", "noop"))
        if self.screensaver:
            # Kodi21.2 InputOperations::SendAction consumes the first action
            # while WakeUpScreenSaverAndDPMS returns true.
            if self.wake_works:
                self.screensaver = False
            return {"ok": True, "status": 200, "body": {"result": "OK"}}
        if params["action"] == "noop":
            return {"ok": True, "status": 200, "body": {"result": "OK"}}
        if params["action"] == "enter":
            self.window = 10000
        elif self.cursor:
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
            self.cursor -= 1
        result = "OK"
        return {"ok": True, "status": 200, "body": {"result": result}}

    def sendto(self, packet, target) -> int:
        self.assertEqual(target, ("127.0.0.1", 9777))
        self.assertEqual(len(packet), 40)
        signature, major, minor, packet_type, seq, total, size, uid = struct.unpack(
            "!4sBBHIIHI", packet[:22]
        )
        self.assertEqual(
            (signature, major, minor, packet_type, seq, total, size),
            (b"XBMC", 2, 0, 3, 1, 1, 8),
        )
        self.assertGreater(uid, 0)
        self.assertEqual(packet[22:32], bytes(10))
        code, flags, amount = struct.unpack("!HHH", packet[32:38])
        self.assertEqual(
            flags, 0x232
        )  # Unicode | no-repeat | queue | down; not KB/use-name.
        self.assertEqual(amount, 0)
        self.assertEqual(packet[38:], b"\0\0")
        self.packets.append((code, target))
        if self.screensaver:
            # Kodi21.2 InputManager::HandleKey consumes the first Unicode key
            # while waking, before it can reach the edit control.
            if self.wake_works:
                self.screensaver = False
            return len(packet)
        if not self.drop:
            # Kodi's Unicode key reaches GUIEditControl at its real cursor.
            if code == 8:
                if self.cursor:
                    self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
                    self.cursor -= 1
            else:
                self.text = (
                    self.text[: self.cursor] + chr(code) + self.text[self.cursor :]
                )
                self.cursor += 1
        return len(packet)

    @contextlib.contextmanager
    def boundaries(self) -> Iterator[Mock]:
        udp = Mock()
        udp.__enter__ = Mock(return_value=udp)
        udp.__exit__ = Mock(return_value=False)
        udp.sendto.side_effect = self.sendto
        with (
            patch.object(HOST, "kodi_running", side_effect=lambda: self.running),
            patch.object(HOST, "kodi_jsonrpc", side_effect=self.rpc),
            patch.object(
                HOST,
                "_kodi_eventserver_listening",
                side_effect=lambda _port: self.listening,
                create=True,
            ),
            patch("socket.socket", return_value=udp),
        ):
            yield udp

    def test_unicode_wire_preserves_existing_prefix_suffix_and_cursor(self) -> None:
        fn = self.endpoint()
        with self.boundaries():
            result = fn({"action": "add", "char": "ж"})
        self.assertEqual(self.text, "preжFIX")
        self.assertEqual(self.cursor, 4)
        self.assertEqual([p[0] for p in self.packets], [0x436])
        self.assertTrue(result["verified"])
        self.assertNotIn("pre", json.dumps(result))
        self.assertNotIn("ж", json.dumps(result, ensure_ascii=False))

    def test_cycle_is_ordered_backspace_then_unicode_without_whole_field_replace(
        self,
    ) -> None:
        fn = self.endpoint()
        with self.boundaries():
            fn({"action": "add", "char": "a"})
            self.packets.clear()
            fn({"action": "cycle", "char": "b"})
        self.assertEqual(self.text, "prebFIX")
        self.assertEqual(self.cursor, 4)
        self.assertEqual([p[0] for p in self.packets], [8, ord("b")])
        self.assertFalse(
            any(method == "Input.SendText" for method, _ in self.rpc_calls)
        )

    def test_commit_keeps_word_and_uses_enter_action(self) -> None:
        fn = self.endpoint()
        with self.boundaries():
            result = fn({"action": "commit"})
        self.assertEqual(self.text, "preFIX")
        self.assertEqual(self.window, 10000)
        self.assertEqual(self.packets, [])
        self.assertIn(("Input.ExecuteAction", {"action": "enter"}), self.rpc_calls)
        self.assertTrue(result["verified"])

    def test_backspace_preserves_suffix_and_does_not_close_keyboard(self) -> None:
        fn = self.endpoint()
        with self.boundaries():
            fn({"action": "backspace"})
        self.assertEqual(self.text, "prFIX")
        self.assertEqual(self.window, 10103)

    def test_invalid_actions_scalars_and_control_codes_fail_before_device_io(
        self,
    ) -> None:
        fn = self.endpoint()
        invalid = [
            None,
            [],
            {},
            {"action": "power"},
            {"action": "add"},
            {"action": "add", "char": "ab"},
            {"action": "add", "char": 1},
            {"action": "add", "char": "\n"},
            {"action": "add", "char": "\x7f"},
            {"action": "add", "char": "\x85"},
            {"action": "add", "char": "\u202e"},
            {"action": "add", "char": "\ud800"},
            {"action": "add", "char": "😀"},
            {"action": "commit", "char": "a"},
            {"action": "add", "char": "a", "host": "elsewhere"},
        ]
        with self.boundaries():
            for data in invalid:
                with (
                    self.subTest(data=repr(data)),
                    self.assertRaises(HOST.InvalidInput),
                ):
                    fn(data)
        self.assertEqual(self.packets, [])
        self.assertEqual(self.rpc_calls, [])

    def test_off_disabled_wrong_dialog_and_missing_socket_refuse_without_input(
        self,
    ) -> None:
        fn = self.endpoint()
        for field, value in [
            ("running", False),
            ("enabled", False),
            ("window", 10000),
            ("listening", False),
        ]:
            with self.subTest(precondition=field):
                old = getattr(self, field)
                setattr(self, field, value)
                with self.boundaries(), self.assertRaises(RuntimeError):
                    fn({"action": "add", "char": "a"})
                setattr(self, field, old)
                self.assertEqual(self.packets, [])
        self.assertEqual(self.text, "preFIX")

    def test_dropped_udp_is_not_reported_as_verified_or_retransmitted(self) -> None:
        fn = self.endpoint()
        self.drop = True
        with self.boundaries(), self.assertRaises(TimeoutError):
            fn({"action": "add", "char": "a"})
        self.assertEqual(self.text, "preFIX")
        self.assertEqual(len(self.packets), 1)

    def test_new_route_requires_existing_host_auth_before_dispatch(self) -> None:
        self.endpoint()
        route = ("POST", "/kodi/text-input")
        self.assertIn(route, HOST.ROUTES)
        self.assertIn(route, HOST.BODY_ROUTES)
        invoked = Mock(return_value={"verified": True})
        with (
            patch.dict(HOST.ROUTES, {route: invoked}),
            patch.object(HOST.Handler, "token", "test-only-token"),
            patch.object(HOST.Handler, "log_message", lambda *_args: None),
        ):
            server = HOST.ThreadingHTTPServer(("127.0.0.1", 0), HOST.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/kodi/text-input"
                for token in (None, "wrong-token"):
                    headers = {"Content-Type": "application/json"}
                    if token:
                        headers["X-HA-Host-Token"] = token
                    # The URL is built from this test process loopback HTTP server.
                    request = urllib.request.Request(  # noqa: S310
                        url, data=b'{"action":"add","char":"a"}', headers=headers
                    )
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        # The URL is built from this test process loopback HTTP
                        # server.
                        urllib.request.urlopen(request, timeout=2)  # noqa: S310
                    self.assertEqual(error.exception.code, 403)
                invoked.assert_not_called()
                # The URL is built from this test process loopback HTTP server.
                request = urllib.request.Request(  # noqa: S310
                    url,
                    data=b'{"action":"add","char":"a"}',
                    headers={
                        "Content-Type": "application/json",
                        "X-HA-Host-Token": "test-only-token",
                    },
                )
                # The URL is built from this test process loopback HTTP server.
                with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
                    self.assertEqual(response.status, 200)
                invoked.assert_called_once_with({"action": "add", "char": "a"})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_concurrent_http_operations_cannot_interleave_a_pending_edit(self) -> None:
        fn = self.endpoint()
        entered, release = threading.Event(), threading.Event()
        errors = []
        original_rpc = self.rpc

        def blocked_rpc(method, params=None, timeout=5) -> dict[str, Any]:
            if method == "GUI.GetProperties" and not entered.is_set():
                entered.set()
                if not release.wait(2):
                    error_message = "Test failed to release first operation"
                    raise AssertionError(error_message)
            return original_rpc(method, params, timeout)

        def first_edit() -> None:
            try:
                fn({"action": "add", "char": "a"})
            # Collect every worker failure for the main-thread assertions below.
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        with (
            self.boundaries(),
            patch.object(HOST, "kodi_jsonrpc", side_effect=blocked_rpc),
        ):
            first = threading.Thread(target=first_edit)
            first.start()
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaisesRegex(RuntimeError, "busy"):
                    fn({"action": "commit"})
                self.assertFalse(
                    any(method == "Input.ExecuteAction" for method, _ in self.rpc_calls)
                )
            finally:
                release.set()
                first.join(2)
        self.assertFalse(first.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.text, "preaFIX")
        self.assertEqual(self.window, 10103)

    def test_failed_add_cannot_authorize_cycle_to_delete_preexisting_text(self) -> None:
        fn = self.endpoint()
        self.text, self.cursor, self.drop = "old", 3, True
        with self.boundaries():
            with self.assertRaises(TimeoutError):
                fn({"action": "add", "char": "a"})
            self.drop = False
            self.packets.clear()
            with self.assertRaises(RuntimeError):
                fn({"action": "cycle", "char": "b"})
        self.assertEqual(self.text, "old")
        self.assertEqual(self.packets, [])

    def test_cycle_refuses_foreign_field_change_or_expired_session(self) -> None:
        fn = self.endpoint()
        with self.boundaries():
            fn({"action": "add", "char": "a"})
            self.text, self.cursor = "foreign", 7
            self.packets.clear()
            with self.assertRaises(RuntimeError):
                fn({"action": "cycle", "char": "b"})
            self.assertEqual(self.text, "foreign")
            self.assertEqual(self.packets, [])
            fn({"action": "add", "char": "a"})
            self.packets.clear()
            real_monotonic = HOST.time.monotonic
            with (
                patch.object(
                    HOST.time, "monotonic", side_effect=lambda: real_monotonic() + 10
                ),
                self.assertRaises(RuntimeError),
            ):
                fn({"action": "cycle", "char": "b"})
            self.assertEqual(self.text, "foreigna")
            self.assertEqual(self.packets, [])

    def test_cycle_rechecks_field_after_settings_before_sending_backspace(self) -> None:
        fn = self.endpoint()
        with self.boundaries():
            fn({"action": "add", "char": "a"})
            self.packets.clear()
            original_rpc = self.rpc

            def changing_rpc(method, params=None, timeout=5) -> dict[str, Any]:
                if method == "Settings.GetSettingValue":
                    self.text, self.cursor = "foreign", 7
                return original_rpc(method, params, timeout)

            with (
                patch.object(HOST, "kodi_jsonrpc", side_effect=changing_rpc),
                self.assertRaises(RuntimeError),
            ):
                fn({"action": "cycle", "char": "b"})
        self.assertEqual(self.text, "foreign")
        self.assertEqual(self.packets, [])

    def test_private_audit_omits_request_text_query_and_auth_token(self) -> None:
        # Exercise the real HTTP handler and file logger. Tokens/text must never
        # reach either output, even though authentication and dispatch succeeded.
        route = ("POST", "/kodi/text-input")
        private_text = "private-keyboard-content-DO-NOT-LOG"
        output = io.StringIO()
        old_handlers, old_level, old_propagate = (
            HOST._AUDIT.handlers[:],
            HOST._AUDIT.level,
            HOST._AUDIT.propagate,
        )
        with tempfile.TemporaryDirectory(prefix="kodi-audit-contract-") as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir(mode=0o700)
            old_umask = os.umask(0o077)
            try:
                with (
                    patch.object(tempfile, "mkdtemp", return_value=str(run_dir)),
                    patch("sys.stderr", output),
                ):
                    log_path, _ = HOST._start_audit()
                    with (
                        patch.dict(
                            HOST.ROUTES, {route: Mock(return_value={"verified": True})}
                        ),
                        patch.object(HOST.Handler, "token", private_text),
                    ):
                        server = HOST.ThreadingHTTPServer(
                            ("127.0.0.1", 0), HOST.Handler
                        )
                        thread = threading.Thread(
                            target=server.serve_forever, daemon=True
                        )
                        thread.start()
                        try:
                            request = urllib.request.Request(
                                f"http://127.0.0.1:{server.server_port}/kodi/text-input?private={private_text}",
                                data=json.dumps(
                                    {"action": "add", "char": private_text}
                                ).encode(),
                                headers={
                                    "Content-Type": "application/json",
                                    "X-HA-Host-Token": private_text,
                                },
                            )
                            # The URL is built from this test process loopback HTTP
                            # server.
                            with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
                                self.assertEqual(response.status, 200)
                        finally:
                            server.shutdown()
                            server.server_close()
                            thread.join(2)
                    for handler in HOST._AUDIT.handlers:
                        handler.flush()
                    self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(log_path.parent.stat().st_mode & 0o777, 0o700)
                    saved = log_path.read_text()
                    self.assertIn('"stage": "request"', saved)
                    self.assertIn('"status": 200', saved)
                    self.assertNotIn(private_text, saved)
                    self.assertNotIn(private_text, output.getvalue())
                    self.assertIn(str(log_path), output.getvalue())
            finally:
                for handler in HOST._AUDIT.handlers:
                    handler.close()
                HOST._AUDIT.handlers, HOST._AUDIT.level, HOST._AUDIT.propagate = (
                    old_handlers,
                    old_level,
                    old_propagate,
                )
                os.umask(old_umask)

    def test_audit_rotation_is_private_without_changing_caller_umask(self) -> None:
        old_handlers, old_level, old_propagate = (
            HOST._AUDIT.handlers[:],
            HOST._AUDIT.level,
            HOST._AUDIT.propagate,
        )
        old_umask = os.umask(0o022)
        with tempfile.TemporaryDirectory(prefix="kodi-audit-permissions-") as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir(mode=0o700)
            server = Mock()
            try:
                with (
                    patch.object(tempfile, "mkdtemp", return_value=str(run_dir)),
                    patch.object(HOST, "ThreadingHTTPServer", return_value=server),
                    patch.object(HOST.signal, "signal"),
                    patch("sys.stderr", io.StringIO()),
                ):
                    HOST.main()
                actual_umask = os.umask(0o022)
                with self.subTest(boundary="caller_umask"):
                    self.assertEqual(actual_umask, 0o022)
                file_handler = next(
                    h for h in HOST._AUDIT.handlers if hasattr(h, "doRollover")
                )
                file_handler.doRollover()
                HOST._audit("rotation_probe", outcome="safe")
                file_handler.flush()
                with self.subTest(boundary="new_rotated_file"):
                    self.assertEqual(
                        Path(file_handler.baseFilename).stat().st_mode & 0o777, 0o600
                    )
                with self.subTest(boundary="previous_rotated_file"):
                    self.assertEqual(
                        Path(file_handler.baseFilename + ".1").stat().st_mode & 0o777,
                        0o600,
                    )
            finally:
                for handler in HOST._AUDIT.handlers:
                    handler.close()
                HOST._AUDIT.handlers, HOST._AUDIT.level, HOST._AUDIT.propagate = (
                    old_handlers,
                    old_level,
                    old_propagate,
                )
                os.umask(old_umask)

    def test_screensaver_wakes_before_edit_without_consuming_or_repeating_the_character(
        self,
    ) -> None:
        fn = self.endpoint()
        for action in ("add", "backspace", "commit"):
            with self.subTest(action=action):
                self.setUp()
                self.screensaver = True
                payload = {"action": action}
                if action == "add":
                    payload["char"] = "a"
                with self.boundaries():
                    result = fn(payload)
                self.assertTrue(result["verified"])
                self.assertIn(
                    ("Input.ExecuteAction", {"action": "noop"}), self.rpc_calls
                )
                if action == "add":
                    self.assertEqual(self.text, "preaFIX")
                    self.assertEqual([p[0] for p in self.packets], [ord("a")])
                elif action == "backspace":
                    self.assertEqual(self.text, "prFIX")
                else:
                    self.assertEqual(self.window, 10000)
                    self.assertEqual(self.text, "preFIX")
        self.setUp()
        self.screensaver, self.wake_works = True, False
        with self.boundaries(), self.assertRaises((RuntimeError, TimeoutError)):
            fn({"action": "add", "char": "a"})
        self.assertEqual(self.text, "preFIX")
        self.assertEqual(self.packets, [])
