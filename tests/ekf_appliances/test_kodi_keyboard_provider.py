"""Phone keyboard host contract: actual-field CAS, privacy and shared T9 lock."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


import contextlib
import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "host_phone_contract", ROOT / "scripts/host/ha-host-actions.py"
)
HOST = importlib.util.module_from_spec(SPEC)
with patch.object(sys, "path", [str(ROOT / "scripts/host"), *sys.path]):
    SPEC.loader.exec_module(HOST)


class KodiPhoneHostTests(unittest.TestCase):
    """Verify the private keyboard protocol through inert transport boundaries."""

    def setUp(self) -> None:
        self.text, self.window, self.calls = "preFIX", 10103, []
        self.screensaver = False
        if hasattr(HOST, "_KODI_PHONE"):
            HOST._KODI_PHONE.clear()
        HOST._KODI_TEXT_CONTEXT = None

    def endpoint(self) -> Callable[[dict[str, Any]], dict[str, Any]]:
        fn = getattr(HOST, "kodi_keyboard", None)
        self.assertTrue(
            callable(fn),
            "Private guarded Kodi phone keyboard endpoint is not implemented",
        )
        return fn

    def rpc(self, method, params, timeout=1) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "GUI.GetProperties":
            result = {"currentwindow": {"id": self.window}}
        elif method == "XBMC.GetInfoLabels":
            result = {"Control.GetLabel(312).index(1)": self.text}
        elif method == "XBMC.GetInfoBooleans":
            self.assertEqual(params, {"booleans": ["System.ScreenSaverActive"]})
            result = {"System.ScreenSaverActive": self.screensaver}
        elif method == "Input.SendText":
            self.assertIs(params["done"], expr2=False)
            self.text = params["text"]
            result = "OK"
        elif method == "Input.ExecuteAction":
            self.assertIn(params["action"], ("enter", "noop"))
            if self.screensaver:
                self.screensaver = False
            elif params["action"] == "enter":
                self.window = 10000
            result = "OK"
        elif method == "Input.Back":
            if self.screensaver:
                self.screensaver = False
            else:
                self.window = 10000
            result = "OK"
        else:
            error_message = "Unexpected Kodi method"
            raise AssertionError(error_message)
        return {"ok": True, "body": {"result": result}, "status": 200}

    @contextlib.contextmanager
    def boundary(self) -> Iterator[None]:
        with (
            patch.object(HOST, "kodi_running", return_value=True),
            patch.object(HOST, "kodi_jsonrpc", side_effect=self.rpc),
        ):
            yield

    def started(self, fn, kind="keyboard") -> dict[str, Any]:
        return fn({"action": "context", "kind": kind})["snapshot"]

    def test_unknown_and_password_contexts_never_read_or_return_field_text(
        self,
    ) -> None:
        fn = self.endpoint()
        with self.boundary():
            for payload in [
                {"action": "snapshot"},
                {"action": "context", "kind": "password"},
                {"action": "snapshot"},
            ]:
                result = fn(payload)["snapshot"]
                self.assertNotIn("text", result)
                self.assertFalse(result["capabilities"]["edit"])
        self.assertFalse(
            any(method == "XBMC.GetInfoLabels" for method, _ in self.calls)
        )

    def test_actual_keyboard_snapshot_has_unique_session_and_no_invented_caret(
        self,
    ) -> None:
        fn = self.endpoint()
        with self.boundary():
            first = self.started(fn)
            self.assertEqual(first["text"], "preFIX")
            self.assertEqual(first["status"], "ready")
            self.assertIsNone(first["selection"])
            self.assertFalse(first["capabilities"]["selection"])
            fn({"action": "context", "kind": "closed"})
            second = self.started(fn)
            self.assertNotEqual(first["session_id"], second["session_id"])

    def test_foreign_text_change_rejects_phone_cas_without_overwriting_input(
        self,
    ) -> None:
        fn = self.endpoint()
        with self.boundary():
            old = self.started(fn)
            self.text = "foreign"
            with self.assertRaises(HOST.KeyboardFailure) as error:
                fn(
                    {
                        "action": "edit",
                        "session_id": old["session_id"],
                        "expected_revision": old["revision"],
                        "text": "ours",
                    }
                )
            self.assertEqual(str(error.exception), "stale_revision")
            current = fn({"action": "snapshot"})["snapshot"]
        self.assertEqual(current["text"], "foreign")
        self.assertFalse(any(method == "Input.SendText" for method, _ in self.calls))

    def test_phone_whole_field_edit_is_explicit_and_confirmed_by_actual_readback(
        self,
    ) -> None:
        fn = self.endpoint()
        with self.boundary():
            before = self.started(fn)
            result = fn(
                {
                    "action": "edit",
                    "session_id": before["session_id"],
                    "expected_revision": before["revision"],
                    "text": "preNEWFIX",
                }
            )
        self.assertEqual(result["snapshot"]["text"], "preNEWFIX")
        self.assertTrue(result["verified"])
        self.assertGreater(result["snapshot"]["revision"], before["revision"])
        self.assertEqual(
            [p for m, p in self.calls if m == "Input.SendText"],
            [{"text": "preNEWFIX", "done": False}],
        )
        self.assertIsNone(HOST._KODI_TEXT_CONTEXT)

    def test_old_session_and_invalid_payloads_cannot_edit_current_field(self) -> None:
        fn = self.endpoint()
        with self.boundary():
            old = self.started(fn)
            self.started(fn)
            with self.assertRaisesRegex(HOST.KeyboardFailure, "session_changed"):
                fn(
                    {
                        "action": "edit",
                        "session_id": old["session_id"],
                        "expected_revision": old["revision"],
                        "text": "ours",
                    }
                )
            current = fn({"action": "snapshot"})["snapshot"]
            for text in ["a" * 16385, "😀" * 8193, "\ud800", "x\x00y"]:
                with (
                    self.subTest(length=len(text)),
                    self.assertRaises(HOST.KeyboardFailure),
                ):
                    fn(
                        {
                            "action": "edit",
                            "session_id": current["session_id"],
                            "expected_revision": current["revision"],
                            "text": text,
                        }
                    )
        self.assertFalse(any(method == "Input.SendText" for method, _ in self.calls))

    def test_commit_and_close_require_observed_dialog_exit_and_preserve_word(
        self,
    ) -> None:
        fn = self.endpoint()
        with self.boundary():
            for action in ("commit", "close"):
                self.window = 10103
                before = self.started(fn)
                result = fn(
                    {
                        "action": action,
                        "session_id": before["session_id"],
                        "expected_revision": before["revision"],
                    }
                )
                self.assertTrue(result["verified"])
                self.assertEqual(result["snapshot"]["status"], "idle")
                self.assertNotIn("text", result["snapshot"])
                self.assertEqual(self.text, "preFIX")

    def test_phone_and_t9_share_one_host_lock(self) -> None:
        fn = self.endpoint()
        with self.boundary():
            before = self.started(fn)
            HOST._KODI_TEXT_LOCK.acquire()
            try:
                with self.assertRaisesRegex(HOST.KeyboardFailure, "busy"):
                    fn(
                        {
                            "action": "edit",
                            "session_id": before["session_id"],
                            "expected_revision": before["revision"],
                            "text": "ours",
                        }
                    )
            finally:
                HOST._KODI_TEXT_LOCK.release()
        self.assertFalse(any(method == "Input.SendText" for method, _ in self.calls))

    def test_invalid_context_kind_has_safe_validation_error_before_kodi_io(
        self,
    ) -> None:
        fn = self.endpoint()
        with self.boundary():
            for kind in ([], {}, None, 42):
                with self.subTest(kind_type=type(kind).__name__):
                    with self.assertRaises(HOST.KeyboardFailure) as error:
                        fn({"action": "context", "kind": kind})
                    self.assertEqual(str(error.exception), "invalid_input")
        self.assertEqual(self.calls, [])

    def test_new_native_dialog_context_revokes_previous_confirmed_t9_character(
        self,
    ) -> None:
        fn = self.endpoint()
        self.cursor = 3
        packets = []
        original_rpc = self.rpc

        def rpc(method, params, timeout=1) -> dict[str, Any]:
            if method == "Settings.GetSettingValue":
                value = True if params["setting"] == "services.esenabled" else 9777
                return {"ok": True, "body": {"result": {"value": value}}, "status": 200}
            return original_rpc(method, params, timeout)

        def send(packet, _target) -> int:
            code = int.from_bytes(packet[32:34], "big")
            packets.append(code)
            if code == 8:
                self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
                self.cursor -= 1
            else:
                self.text = (
                    self.text[: self.cursor] + chr(code) + self.text[self.cursor :]
                )
                self.cursor += 1
            return len(packet)

        udp = Mock()
        udp.__enter__ = Mock(return_value=udp)
        udp.__exit__ = Mock(return_value=False)
        udp.sendto.side_effect = send
        with (
            self.boundary(),
            patch.object(HOST, "kodi_jsonrpc", side_effect=rpc),
            patch.object(HOST, "_kodi_eventserver_listening", return_value=True),
            patch("socket.socket", return_value=udp),
        ):
            HOST.kodi_text_input({"action": "add", "char": "a"})
            self.assertEqual(self.text, "preaFIX")
            # A new native OnInputRequested can open another dialog with identical
            # text; its lifecycle must revoke the earlier T9 replacement right.
            self.started(fn)
            packets.clear()
            with self.assertRaises(RuntimeError):
                HOST.kodi_text_input({"action": "cycle", "char": "b"})
        self.assertEqual(self.text, "preaFIX")
        self.assertEqual(packets, [])

    def test_phone_mutations_wake_screensaver_without_losing_enter_or_back(
        self,
    ) -> None:
        fn = self.endpoint()
        with self.boundary():
            for action in ("edit", "commit", "close"):
                with self.subTest(action=action):
                    self.window, self.text, self.screensaver = 10103, "preFIX", True
                    before = self.started(fn)
                    self.calls.clear()
                    payload = {
                        "action": action,
                        "session_id": before["session_id"],
                        "expected_revision": before["revision"],
                    }
                    if action == "edit":
                        payload["text"] = "ours"
                    result = fn(payload)
                    self.assertTrue(result["verified"])
                    self.assertIn(
                        ("Input.ExecuteAction", {"action": "noop"}), self.calls
                    )
                    self.assertFalse(self.screensaver)
                    if action == "edit":
                        self.assertEqual(result["snapshot"]["text"], "ours")
                        self.assertEqual(self.window, 10103)
                    else:
                        self.assertEqual(result["snapshot"]["status"], "idle")
                        self.assertEqual(self.text, "preFIX")
