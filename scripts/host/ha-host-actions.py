#!/usr/bin/env python3
"""Small local HTTP bridge for Home Assistant host actions."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import ha_host_media as _media
from ha_host_audit import _AUDIT, _audit, _start_audit
from ha_host_media import (
    choose_video_file_from_candidates,
    clean_media_title,
    completed_video_files,
    detect_media_type,
    ensure_sources_xml,
    ensure_symlink,
    path_contains,
    season_number,
    torrent_matches_payload,
    torrent_root_path,
    trailing_slash,
)
from ha_host_media import (
    video_candidates as _video_candidates,
)

# Retain helper names exposed by the original standalone bridge.
episode_match = _media.episode_match
season_episode_key = _media.season_episode_key
normalized_path = _media.normalized_path
sanitize_path_part = _media.sanitize_path_part

_PROC_UDP_MIN_FIELDS = 4
_PROC_SOCKET_ADDRESS_PARTS = 2
_UNICODE_BMP_MAX = 0xFFFF
_UDP_PORT_LIMIT = 65536
_KODI_PHONE_MAX_UTF16_UNITS = 16384
_HTTP_CONFLICT = 409
_IPV4_VERSION = 4
_HISTORY_MAX_DAYS = 90
_HISTORY_MIN_BUCKET_SECONDS = 300
_SECONDS_PER_DAY = 86400

CONFIG_DIR = Path.home() / ".config" / "ha-host-actions"
TOKEN_FILE = CONFIG_DIR / "token"
PASSWORD_FILE = CONFIG_DIR / "desktop-password"
KODI_UNIT = os.environ.get("KODI_UNIT", "kodi.service")
AGENT_BROWSER = Path(
    os.environ.get("AGENT_BROWSER", "/home/linuxbrew/.linuxbrew/bin/agent-browser")
)
RUTRACKER_LOGIN_URL = os.environ.get(
    "RUTRACKER_LOGIN_URL", "https://rutracker.org/forum/login.php"
)
RUTRACKER_BROWSER_SESSION = os.environ.get(
    "RUTRACKER_BROWSER_SESSION", "ha-rutracker-login"
)
KODI_JSONRPC_URL = os.environ.get("KODI_JSONRPC_URL", "http://127.0.0.1:8080/jsonrpc")
KODI_USERNAME = os.environ.get("KODI_USERNAME", "kodi")
KODI_PASSWORD = os.environ.get("KODI_PASSWORD", "")
KODI_USERDATA_DIR = Path(
    os.environ.get("KODI_USERDATA_DIR", str(Path.home() / ".kodi/userdata"))
)
KODI_LIBRARY_DIR = Path(
    os.environ.get("KODI_LIBRARY_DIR", str(Path.home() / "KodiLibrary"))
)
KODI_SERIES_DIR = KODI_LIBRARY_DIR / "Series"
KODI_MOVIES_DIR = KODI_LIBRARY_DIR / "Movies"
TRANSMISSION_RPC_URL = os.environ.get(
    "TRANSMISSION_RPC_URL", "http://127.0.0.1:9091/transmission/rpc"
)
TRANSMISSION_RPC_USERNAME = os.environ.get("TRANSMISSION_RPC_USERNAME", "")
TRANSMISSION_RPC_PASSWORD = os.environ.get("TRANSMISSION_RPC_PASSWORD", "")
VIDEO_SUFFIXES = {
    ".avi",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ts",
    ".webm",
}
MIN_VIDEO_SIZE = 50 * 1024 * 1024


def read_secret(path: Path) -> str:
    """Read an owner-managed local value, returning empty when absent."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def session_env() -> dict[str, str]:
    """Build the desktop session environment for the existing host commands."""
    env = os.environ.copy()
    runtime_dir = f"/run/user/{os.getuid()}"
    env.update(
        {
            "HOME": str(Path.home()),
            "USER": os.environ.get("USER", "ultra"),
            "LOGNAME": os.environ.get("LOGNAME", "ultra"),
            "XDG_RUNTIME_DIR": runtime_dir,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus",
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
            "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", "wayland-0"),
        }
    )
    xauthority = os.environ.get("XAUTHORITY")
    if not xauthority:
        auth_files = sorted(Path(runtime_dir).glob(".mutter-Xwaylandauth.*"))
        if auth_files:
            xauthority = str(auth_files[-1])
    if xauthority:
        env["XAUTHORITY"] = xauthority
    return env


def run(
    cmd: list[str],
    timeout: float = 10.0,
    *,
    check: bool = False,
    input_data: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an argument list with captured output and desktop session variables."""
    # Arguments are local command lists; execution never uses a shell.
    return subprocess.run(  # noqa: S603
        cmd,
        env=session_env(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=check,
        input=input_data,
    )


def dbus_call(args: list[str], timeout: float = 5.0) -> dict[str, Any]:
    """Call a session bus method and return its bounded diagnostic result."""
    try:
        proc = run(["gdbus", "call", "--session", *args], timeout=timeout)
    except FileNotFoundError:
        # gdbus is absent on headless hosts; report the probe as unavailable.
        return {
            "cmd": " ".join(args),
            "returncode": None,
            "stdout": None,
            "stderr": "gdbus unavailable",
        }
    return {
        "cmd": " ".join(args),
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def wake_and_unlock_screen() -> dict[str, Any]:
    """Wake the desktop display and deactivate the screensaver."""
    results = []
    results.append(
        dbus_call(
            [
                "--dest",
                "org.gnome.Mutter.DisplayConfig",
                "--object-path",
                "/org/gnome/Mutter/DisplayConfig",
                "--method",
                "org.freedesktop.DBus.Properties.Set",
                "org.gnome.Mutter.DisplayConfig",
                "PowerSaveMode",
                "<int32 0>",
            ]
        )
    )
    results.append(
        dbus_call(
            [
                "--dest",
                "org.gnome.ScreenSaver",
                "--object-path",
                "/org/gnome/ScreenSaver",
                "--method",
                "org.gnome.ScreenSaver.SetActive",
                "false",
            ]
        )
    )
    return {"screen": "unlocked", "results": results}


def lock_screen() -> dict[str, Any]:
    """Request the desktop screensaver lock."""
    result = dbus_call(
        [
            "--dest",
            "org.gnome.ScreenSaver",
            "--object-path",
            "/org/gnome/ScreenSaver",
            "--method",
            "org.gnome.ScreenSaver.Lock",
        ]
    )
    return {"screen": "locked", "result": result}


def kodi_running() -> bool:
    """Check whether the configured Kodi service is active."""
    return (
        run(["systemctl", "is-active", "--quiet", KODI_UNIT], timeout=3).returncode == 0
    )


def kodi_status() -> dict[str, Any]:
    """Report actual process power separately from the supervisor's result."""
    proc = run(
        [
            "systemctl",
            "show",
            KODI_UNIT,
            "--property=LoadState,ActiveState,SubState,MainPID,Result,ExecMainStatus",
        ],
        timeout=2,
    )
    if proc.returncode:
        error_message = "Kodi process status could not be read"
        raise RuntimeError(error_message)
    properties = dict(
        line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line
    )
    if properties.get("LoadState") != "loaded":
        error_message = "Kodi process unit is not loaded"
        raise RuntimeError(error_message)
    main_pid = properties.get("MainPID")
    if not isinstance(main_pid, str) or not re.fullmatch(r"[0-9]+", main_pid):
        error_message = "Kodi process identifier could not be read"
        raise RuntimeError(error_message)
    active, sub = properties.get("ActiveState"), properties.get("SubState")
    process_present = int(main_pid) > 0
    if not process_present:
        process = run(["pgrep", "-u", str(os.getuid()), "-x", "kodi.bin"], timeout=2)
        if process.returncode not in {0, 1}:
            error_message = "Kodi independent process check failed"
            raise RuntimeError(error_message)
        process_present = process.returncode == 0
    state = "unknown"
    if active == "active" and int(main_pid) > 0:
        state = "on"
    elif active in {"inactive", "failed"} and not process_present:
        state = "off"
    exit_status = properties.get("ExecMainStatus", "")
    return {
        "state": state,
        "active_state": active,
        "sub_state": sub,
        "process_present": process_present,
        "unit_result": properties.get("Result"),
        "exit_status": int(exit_status)
        if re.fullmatch(r"[0-9]+", exit_status)
        else None,
    }


def start_kodi() -> dict[str, Any]:
    """Start the configured Kodi unit once and report the accepted request."""
    if kodi_running():
        return {"kodi": "already_running"}

    # kodi.service is a system unit (DRM/ALSA access, see /usr/local/bin/kodi-
    # session); the sudoers
    # drop-in /etc/sudoers.d/ha-kodi lets this user start/stop exactly that unit.
    proc = run(
        ["sudo", "-n", "systemctl", "start", "--no-block", KODI_UNIT], timeout=10
    )
    if proc.returncode != 0:
        error_message = (
            f"systemctl start {KODI_UNIT} failed: {proc.stderr.strip()[-300:]}"
        )
        raise RuntimeError(error_message)
    return {"kodi": "starting", "unit": KODI_UNIT}


def kodi_jsonrpc(
    method: str, params: dict[str, Any] | None = None, timeout: float = 5.0
) -> dict[str, Any]:
    """Call the configured Kodi HTTP endpoint and retain its response body."""
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": "ha-host-actions",
        "method": method,
    }
    if params is not None:
        payload["params"] = params
    # This endpoint is host configuration, never a request-supplied URL.
    request = urllib.request.Request(  # noqa: S310
        KODI_JSONRPC_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    credentials = f"{KODI_USERNAME}:{KODI_PASSWORD}".encode()
    request.add_header(
        "Authorization",
        "Basic " + __import__("base64").b64encode(credentials).decode("ascii"),
    )
    # This endpoint is host configuration, never a request-supplied URL.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read(65536).decode("utf-8", "replace")
    try:
        decoded: Any = json.loads(body)
    except json.JSONDecodeError:
        decoded = body
    return {"ok": True, "status": response.status, "body": decoded}


# Kodi's JSON-RPC Input.SendText is a whole-field replacement, and the v21.2
# Input.ButtonEvent KB path does not populate printable Unicode. Use its native
# local EventServer Unicode path instead; keep every edit behind existing HTTP
# authentication, a real keyboard dialog, and an observed field change.
_KODI_TEXT_LOCK = threading.Lock()
_KODI_TEXT_CLIENT_ID = int.from_bytes(os.urandom(4), "big") or 1
_KODI_KEYBOARD_WINDOW = 10103
_KODI_KEYBOARD_LABEL = "Control.GetLabel(312).index(1)"
# A cycle may replace only the character produced by our immediately preceding
# confirmed operation. Keep a short, volatile witness, never stored field text.
_KODI_TEXT_CONTEXT: tuple[bytes, float] | None = None
_KODI_TEXT_CONTEXT_KEY = os.urandom(32)
_KODI_TEXT_CONTEXT_TTL = 3.0


def _kodi_text_fingerprint(text: str) -> bytes:
    return hmac.digest(
        _KODI_TEXT_CONTEXT_KEY, text.encode("utf-8", "surrogatepass"), hashlib.sha256
    )


def _kodi_text_rpc(method: str, params: dict[str, Any], deadline: float) -> Any:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        error_message = "Kodi text input deadline exceeded"
        raise TimeoutError(error_message)
    response = kodi_jsonrpc(method, params, timeout=min(1.0, remaining))
    body = response.get("body")
    if (
        not response.get("ok")
        or not isinstance(body, dict)
        or "error" in body
        or "result" not in body
    ):
        error_message = "Kodi text input JSON-RPC request failed"
        raise RuntimeError(error_message)
    return body["result"]


def _kodi_keyboard_window(deadline: float) -> bool:
    result = _kodi_text_rpc(
        "GUI.GetProperties", {"properties": ["currentwindow"]}, deadline
    )
    return (
        isinstance(result, dict)
        and result.get("currentwindow", {}).get("id") == _KODI_KEYBOARD_WINDOW
    )


def _kodi_keyboard_text(deadline: float) -> str:
    if not _kodi_keyboard_window(deadline):
        error_message = "Kodi keyboard dialog is not active"
        raise RuntimeError(error_message)
    result = _kodi_text_rpc(
        "XBMC.GetInfoLabels", {"labels": [_KODI_KEYBOARD_LABEL]}, deadline
    )
    text = result.get(_KODI_KEYBOARD_LABEL) if isinstance(result, dict) else None
    if not isinstance(text, str):
        error_message = "Kodi keyboard field cannot be read"
        # Preserve RuntimeError for malformed upstream protocol observations.
        raise RuntimeError(error_message)  # noqa: TRY004
    return text


def _kodi_wake_keyboard(deadline: float) -> None:
    """Wake through JSON-RPC before a character/action can be consumed as wake."""

    def active() -> bool:
        result = _kodi_text_rpc(
            "XBMC.GetInfoBooleans", {"booleans": ["System.ScreenSaverActive"]}, deadline
        )
        value = (
            result.get("System.ScreenSaverActive") if isinstance(result, dict) else None
        )
        if type(value) is not bool:
            error_message = "Kodi screensaver state cannot be read"
            raise RuntimeError(error_message)
        return value

    if not active():
        return
    # InputOperations::SendAction wakes before dispatching the actual action.
    # NOOP preserves the field when already awake; never repeat the typed edit.
    _kodi_text_rpc("Input.ExecuteAction", {"action": "noop"}, deadline)
    wake_until = min(deadline, time.monotonic() + 0.6)
    while active():
        if time.monotonic() >= wake_until:
            error_message = "Kodi keyboard wake was not observed"
            raise TimeoutError(error_message)
        time.sleep(0.03)


def _kodi_eventserver_listening(port: int) -> bool:
    # Linux /proc reports the socket actually bound now, rather than merely a
    # configured preference. Only a socket reachable through IPv4 loopback fits
    # this local-only transport. This never enables a listener or changes a bind.
    try:
        lines = Path("/proc/net/udp").read_text().splitlines()[1:]
    except OSError:
        return False
    for line in lines:
        fields = line.split()
        if len(fields) < _PROC_UDP_MIN_FIELDS:
            continue
        local = fields[1].split(":")
        if len(local) == _PROC_SOCKET_ADDRESS_PARTS and local[0] in {
            "0100007F",
            "00000000",
        }:
            try:
                if int(local[1], 16) == port:
                    return True
            except ValueError:
                continue
    return False


def _kodi_unicode_packet(codepoint: int) -> bytes:
    # Kodi21.2 EventPacket.h + EventClient::OnPacketBUTTON:
    # PT_BUTTON=3, PTB_UNICODE|NO_REPEAT|QUEUE|DOWN=0x232, no named keymap.
    payload = struct.pack("!HHH2s", codepoint, 0x232, 0, b"\0\0")
    return (
        struct.pack(
            "!4sBBHIIHI10s",
            b"XBMC",
            2,
            0,
            3,
            1,
            1,
            len(payload),
            _KODI_TEXT_CLIENT_ID,
            bytes(10),
        )
        + payload
    )


def _kodi_text_edit_observed(action: str, before: str, after: str, char: str) -> bool:
    # Compare a single edit at any cursor position. Do not assume the caret is at
    # the end and do not hold a stale mirror of the user's text between presses.
    if action == "add" and len(after) == len(before) + 1:
        return any(
            after[i] == char and after[:i] + after[i + 1 :] == before
            for i in range(len(after))
        )
    if action == "cycle" and len(after) == len(before) and after != before:
        return any(
            after[i] == char
            and before[:i] == after[:i]
            and before[i + 1 :] == after[i + 1 :]
            for i in range(len(before))
        )
    if action == "backspace" and len(after) + 1 == len(before):
        return any(before[:i] + before[i + 1 :] == after for i in range(len(before)))
    return False


def _validate_kodi_text(payload: dict[str, Any]) -> tuple[str, str]:
    """Reject unsupported edits before acquiring the lock or performing I/O."""
    if not isinstance(payload, dict):
        error_message = "Kodi text input requires a JSON object"
        raise InvalidInput(error_message)
    action = payload.get("action")
    if not isinstance(action, str) or action not in {
        "add",
        "cycle",
        "backspace",
        "commit",
    }:
        error_message = "Unsupported Kodi text input action"
        raise InvalidInput(error_message)
    char = ""
    allowed_keys = {"action"}
    if action in {"add", "cycle"}:
        allowed_keys.add("char")
        char = payload.get("char")
        if (
            not isinstance(char, str)
            or len(char) != 1
            or ord(char) > _UNICODE_BMP_MAX
            or not char.isprintable()
            or unicodedata.category(char).startswith("C")
        ):
            error_message = (
                "Kodi text input requires one printable BMP Unicode character"
            )
            raise InvalidInput(error_message)
    if set(payload) != allowed_keys:
        error_message = "Unexpected Kodi text input fields"
        raise InvalidInput(error_message)
    return action, char


def _verify_kodi_cycle(
    action: str, before: str, previous: tuple[bytes, float] | None
) -> None:
    """Require the immediately preceding confirmed edit before replacement."""
    if action == "cycle" and (
        not before
        or previous is None
        or time.monotonic() - previous[1] > _KODI_TEXT_CONTEXT_TTL
        or not hmac.compare_digest(previous[0], _kodi_text_fingerprint(before))
    ):
        error_message = "Kodi cycle has no current confirmed character"
        raise RuntimeError(error_message)


def _send_kodi_text(action: str, char: str, before: str, deadline: float) -> None:
    """Recheck focus and send each Unicode event once on the existing socket."""
    enabled = _kodi_text_rpc(
        "Settings.GetSettingValue", {"setting": "services.esenabled"}, deadline
    )
    port_result = _kodi_text_rpc(
        "Settings.GetSettingValue", {"setting": "services.esport"}, deadline
    )
    port = port_result.get("value") if isinstance(port_result, dict) else None
    if not isinstance(enabled, dict) or enabled.get("value") is not True:
        error_message = "Kodi EventServer is disabled"
        raise RuntimeError(error_message)
    if (
        type(port) is not int
        or not 0 < port < _UDP_PORT_LIMIT
        or not _kodi_eventserver_listening(port)
    ):
        error_message = "Kodi EventServer loopback socket is not available"
        raise RuntimeError(error_message)
    # Settings/socket checks take time. Re-read the actual text as well
    # as focus immediately before sending; our lock cannot stop external
    # remotes or Kodi itself from editing the field.
    if _kodi_keyboard_text(deadline) != before:
        error_message = "Kodi keyboard field changed before input"
        raise RuntimeError(error_message)
    if time.monotonic() >= deadline:
        error_message = "Kodi text input deadline exceeded"
        raise TimeoutError(error_message)
    codes = ([8] if action == "cycle" else []) + [ord(char)]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.settimeout(0.2)
        for code in codes:
            packet = _kodi_unicode_packet(code)
            if udp.sendto(packet, ("127.0.0.1", port)) != len(packet):
                error_message = "Kodi EventServer packet was not sent completely"
                raise RuntimeError(error_message)


def _observe_kodi_text(
    action: str, before: str, char: str, deadline: float
) -> dict[str, Any]:
    """Publish verification only after observing the existing field-change proof."""
    # The shared lock protects this bounded, process-local HMAC witness.
    global _KODI_TEXT_CONTEXT  # noqa: PLW0603
    observe_until = min(deadline, time.monotonic() + 1.2)
    while time.monotonic() < observe_until:
        if action == "commit":
            if not _kodi_keyboard_window(deadline):
                return {"kodi": "text_input", "action": action, "verified": True}
        else:
            after = _kodi_keyboard_text(deadline)
            if _kodi_text_edit_observed(action, before, after, char):
                if action in {"add", "cycle"}:
                    _KODI_TEXT_CONTEXT = (
                        _kodi_text_fingerprint(after),
                        time.monotonic(),
                    )
                return {"kodi": "text_input", "action": action, "verified": True}
        time.sleep(0.03)
    # UDP delivery is not an acknowledgement. Never retry a text edit: a late
    # packet plus a retry would duplicate/delete input. Surface lack of proof.
    error_message = "Kodi text edit was sent but its field change was not observed"
    raise TimeoutError(error_message)


def kodi_text_input(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Edit only an observable Kodi keyboard; never start Kodi or change power.

    EventServer carries one BMP code point per 16-bit button. Reject unsupported
    non-BMP/control/format characters explicitly, rather than truncating them.
    Results contain no field text, entered character, or keyboard heading.
    """
    action, char = _validate_kodi_text(payload)
    # The shared lock protects this bounded, process-local HMAC witness.
    global _KODI_TEXT_CONTEXT  # noqa: PLW0603
    deadline = time.monotonic() + 4.0
    if not _KODI_TEXT_LOCK.acquire(timeout=0.5):
        error_message = "Kodi text input is busy"
        raise RuntimeError(error_message)
    try:
        previous = _KODI_TEXT_CONTEXT
        # Invalidate before any I/O. A timeout, failed send, foreign edit or focus
        # loss must never authorize a later backspace against pre-existing text.
        _KODI_TEXT_CONTEXT = None
        if not kodi_running():
            error_message = "Kodi is not running"
            raise RuntimeError(error_message)
        before = _kodi_keyboard_text(deadline)
        _verify_kodi_cycle(action, before, previous)
        if action == "backspace" and not before:
            return {"kodi": "text_input", "action": action, "verified": True}
        _kodi_wake_keyboard(deadline)
        if _kodi_keyboard_text(deadline) != before:
            error_message = "Kodi keyboard field changed while waking"
            raise RuntimeError(error_message)
        if action in {"add", "cycle"}:
            _send_kodi_text(action, char, before, deadline)
        else:
            _kodi_text_rpc(
                "Input.ExecuteAction",
                {"action": "enter" if action == "commit" else "backspace"},
                deadline,
            )
        return _observe_kodi_text(action, before, char, deadline)
    finally:
        _KODI_TEXT_LOCK.release()


# Private phone keyboard API. Its plaintext snapshot is returned only to the
# authenticated HA provider, never an HA REST command/entity or diagnostic log.
_KODI_PHONE: dict[str, Any] = {}


# The existing class name is emitted as error_class in the audit protocol.
class KeyboardFailure(Exception):  # noqa: N818
    """Preserve the keyboard API error code and its existing public type name."""

    def __init__(self, code: str) -> None:
        """Retain the protocol error code and Exception message."""
        self.code = code
        super().__init__(code)


def _phone_context(kind: str, status: str | None = None) -> None:
    _KODI_PHONE.clear()
    _KODI_PHONE.update(
        kind=kind,
        session_id=uuid4().hex,
        revision=0,
        text=None,
        status=status or ("sensitive" if kind == "password" else "idle"),
        fresh_at=None,
    )


def _phone_snapshot() -> dict[str, Any]:
    state = _KODI_PHONE
    ready = state.get("status") == "ready" and isinstance(state.get("text"), str)
    result = {
        "session_id": state.get("session_id"),
        "revision": state.get("revision", 0),
        "focused": ready or state.get("status") == "sensitive",
        "sensitive": state.get("status") == "sensitive",
        "selection": None,
        "capabilities": {
            "edit": ready,
            "commit": ready,
            "close": ready,
            "selection": False,
        },
        "status": state.get("status", "idle"),
        "fresh_at": state.get("fresh_at"),
    }
    if ready:
        result["text"] = state["text"]
    return result


def _phone_valid_text(value: Any) -> str:
    if not isinstance(value, str) or "\x00" in value:
        error_message = "invalid_input"
        raise KeyboardFailure(error_message)
    try:
        if len(value.encode("utf-16-le")) // 2 > _KODI_PHONE_MAX_UTF16_UNITS:
            error_message = "invalid_input"
            raise KeyboardFailure(error_message)
    except UnicodeError as exc:
        error_message = "invalid_input"
        raise KeyboardFailure(error_message) from exc
    return value


def _phone_observe(deadline: float) -> dict[str, Any]:
    if _KODI_PHONE.get("kind") != "keyboard":
        return _phone_snapshot()
    if not kodi_running():
        _phone_context("closed", "unavailable")
        return _phone_snapshot()
    if not _kodi_keyboard_window(deadline):
        _phone_context("closed")
        return _phone_snapshot()
    values = _kodi_text_rpc(
        "XBMC.GetInfoLabels", {"labels": [_KODI_KEYBOARD_LABEL]}, deadline
    )
    text = _phone_valid_text(
        values.get(_KODI_KEYBOARD_LABEL) if isinstance(values, dict) else None
    )
    if text != _KODI_PHONE.get("text"):
        _KODI_PHONE["revision"] += 1
        _KODI_PHONE["text"] = text
    _KODI_PHONE["status"] = "ready"
    _KODI_PHONE["fresh_at"] = datetime.now().astimezone().isoformat()
    return _phone_snapshot()


def _phone_validate(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        error_message = "invalid_input"
        raise KeyboardFailure(error_message)
    action = payload.get("action")
    allowed = {
        "context": {"action", "kind"},
        "snapshot": {"action"},
        "edit": {"action", "session_id", "expected_revision", "text"},
        "commit": {"action", "session_id", "expected_revision"},
        "close": {"action", "session_id", "expected_revision"},
    }
    if (
        not isinstance(action, str)
        or action not in allowed
        or set(payload) != allowed[action]
    ):
        error_message = "invalid_input"
        raise KeyboardFailure(error_message)
    if action == "context" and (
        not isinstance(payload["kind"], str)
        or payload["kind"] not in {"keyboard", "password", "closed", "unknown"}
    ):
        error_message = "invalid_input"
        raise KeyboardFailure(error_message)
    if action in {"edit", "commit", "close"} and (
        not isinstance(payload["session_id"], str)
        or not re.fullmatch("[0-9a-f]{32}", payload["session_id"])
        or type(payload["expected_revision"]) is not int
        or payload["expected_revision"] < 0
    ):
        error_message = "invalid_input"
        raise KeyboardFailure(error_message)
    if action == "edit":
        _phone_valid_text(payload["text"])
    return action


def _phone_guard(payload: dict[str, Any], current: dict[str, Any]) -> None:
    if not current["capabilities"]["edit"]:
        raise KeyboardFailure("sensitive" if current["sensitive"] else "not_focused")
    if payload["session_id"] != current["session_id"]:
        error_message = "session_changed"
        raise KeyboardFailure(error_message)
    if payload["expected_revision"] != current["revision"]:
        error_message = "stale_revision"
        raise KeyboardFailure(error_message)


def _phone_mutate(
    action: str, payload: dict[str, Any], before: dict[str, Any], deadline: float
) -> dict[str, Any]:
    # The shared lock protects this bounded, process-local HMAC witness.
    global _KODI_TEXT_CONTEXT  # noqa: PLW0603
    # Wake through a harmless action before commit/close can be consumed.
    # Re-read after wake and CAS processing, immediately before the write. Kodi
    # has no server CAS; our shared lock cannot stop another physical input.
    _kodi_wake_keyboard(deadline)
    if _kodi_keyboard_text(deadline) != before["text"]:
        _phone_observe(deadline)
        error_message = "conflict"
        raise KeyboardFailure(error_message)
    _KODI_TEXT_CONTEXT = None
    if action == "edit":
        if payload["text"] == before["text"]:
            return {"snapshot": before, "verified": True}
        _kodi_text_rpc(
            "Input.SendText", {"text": payload["text"], "done": False}, deadline
        )
    elif action == "commit":
        _kodi_text_rpc("Input.ExecuteAction", {"action": "enter"}, deadline)
    else:
        _kodi_text_rpc("Input.Back", {}, deadline)
    observe_until = min(deadline, time.monotonic() + 1.2)
    while time.monotonic() < observe_until:
        current = _phone_observe(deadline)
        if action != "edit":
            if current["status"] == "idle":
                return {"snapshot": current, "verified": True}
        elif current.get("text") == payload["text"]:
            return {"snapshot": current, "verified": True}
        elif current.get("text") not in {before["text"], payload["text"]}:
            error_message = "conflict"
            raise KeyboardFailure(error_message)
        time.sleep(0.03)
    _phone_context("keyboard", "stale")
    error_message = "unconfirmed"
    raise KeyboardFailure(error_message)


def kodi_keyboard(payload: dict[str, Any]) -> dict[str, Any]:
    """Observed non-sensitive phone field on the same lock as T9 operations."""
    # The shared lock protects this bounded, process-local HMAC witness.
    global _KODI_TEXT_CONTEXT  # noqa: PLW0603
    action = _phone_validate(payload)
    if not _KODI_TEXT_LOCK.acquire(timeout=0.5):
        error_message = "busy"
        raise KeyboardFailure(error_message)
    try:
        deadline = time.monotonic() + 4.0
        if action == "context":
            # The HA provider sends only the actual Kodi Input.OnInputRequested
            # type, or OnInputFinished. Unknown/control types never permit text.
            _KODI_TEXT_CONTEXT = None
            _phone_context(payload["kind"])
        if not _KODI_PHONE:
            _phone_context("unknown")
        current = _phone_observe(deadline)
        if action in {"snapshot", "context"}:
            return {"snapshot": current}
        _phone_guard(payload, current)
        return _phone_mutate(action, payload, current, deadline)
    except KeyboardFailure:
        raise
    except Exception as exc:
        _phone_context("closed", "unavailable")
        error_message = "unavailable"
        raise KeyboardFailure(error_message) from exc
    finally:
        _KODI_TEXT_LOCK.release()


def _transmission_request(body: bytes, session_id: str) -> urllib.request.Request:
    """Build one authenticated request for the current session handshake."""
    # This endpoint is host configuration, never a request-supplied URL.
    request = urllib.request.Request(  # noqa: S310
        TRANSMISSION_RPC_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if session_id:
        request.add_header("X-Transmission-Session-Id", session_id)
    if TRANSMISSION_RPC_USERNAME or TRANSMISSION_RPC_PASSWORD:
        credentials = (
            f"{TRANSMISSION_RPC_USERNAME}:{TRANSMISSION_RPC_PASSWORD}".encode()
        )
        request.add_header(
            "Authorization",
            "Basic " + __import__("base64").b64encode(credentials).decode("ascii"),
        )
    return request


def transmission_rpc(
    method: str, arguments: dict[str, Any] | None = None, timeout: float = 10.0
) -> dict[str, Any]:
    """Call Transmission with its existing session-challenge retry behavior."""
    payload = {"method": method, "arguments": arguments or {}}
    body = json.dumps(payload).encode("utf-8")
    session_id = ""
    for _ in range(2):
        request = _transmission_request(body, session_id)
        try:
            # The request uses only the owner-configured Transmission endpoint.
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                decoded = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == _HTTP_CONFLICT:
                session_id = exc.headers.get("X-Transmission-Session-Id", "")
                if session_id:
                    continue
            raise
        else:
            if decoded.get("result") != "success":
                raise RuntimeError(decoded.get("result") or decoded)
            return decoded
    error_message = "Transmission RPC did not return a session id"
    raise RuntimeError(error_message)


def transmission_torrents() -> list[dict[str, Any]]:
    """Return tracker metadata for the existing completed-media workflow."""
    response = transmission_rpc(
        "torrent-get",
        {
            "fields": [
                "id",
                "name",
                "downloadDir",
                "files",
                "fileStats",
                "labels",
                "status",
                "percentDone",
                "error",
                "errorString",
            ]
        },
    )
    torrents = response.get("arguments", {}).get("torrents", [])
    return torrents if isinstance(torrents, list) else []


def kodi_jsonrpc_quit() -> dict[str, Any]:
    """Request graceful Kodi exit and retain transport failure diagnostics."""
    try:
        return kodi_jsonrpc("Application.Quit", timeout=2)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def stop_kodi() -> dict[str, Any]:
    """Wait for graceful exit before requesting the configured unit to stop."""
    if not kodi_running():
        return {"kodi": "not_running"}

    rpc_result = kodi_jsonrpc_quit()
    # Kodi needs a few seconds to shut down on this board; killing the unit mid-
    # shutdown also
    # kills its virtual X server and Kodi then dies with an X IO error (unit left
    # "failed").
    deadline = time.monotonic() + 8
    while kodi_running() and time.monotonic() < deadline:
        time.sleep(0.5)
    if not kodi_running():
        return {"kodi": "stopped", "method": "jsonrpc", "jsonrpc": rpc_result}

    stop = run(["sudo", "-n", "systemctl", "stop", KODI_UNIT], timeout=20)
    return {
        "kodi": "stopped" if not kodi_running() else "stop_requested",
        "method": "systemctl_stop",
        "jsonrpc": rpc_result,
        "systemctl_returncode": stop.returncode,
    }


def projector_on() -> dict[str, Any]:
    """Preserve the compatibility host hook that starts Kodi."""
    return {"kodi": start_kodi()}


def projector_off() -> dict[str, Any]:
    """Preserve the compatibility host hook that stops Kodi."""
    return {"kodi": stop_kodi()}


def video_candidates(root: Path) -> list[Path]:
    """Select local videos using the configured bridge size and suffix policy."""
    return _video_candidates(
        root, minimum_size=MIN_VIDEO_SIZE, video_suffixes=VIDEO_SUFFIXES
    )


def choose_video_file(root: Path, media_type: str) -> Path:
    """Find and choose a complete video below the requested local path."""
    candidates = video_candidates(root)
    if not candidates:
        error_message = f"no completed video files under {root}"
        raise FileNotFoundError(error_message)
    return choose_video_file_from_candidates(candidates, media_type)


def completed_video_files_for_torrent(torrent: dict[str, Any]) -> list[Path]:
    """Select completed videos using the configured size and suffix policy."""
    return completed_video_files(
        torrent, minimum_size=MIN_VIDEO_SIZE, video_suffixes=VIDEO_SUFFIXES
    )


def completed_video_files_for_payload(
    payload: dict[str, Any], root: Path
) -> tuple[list[Path], dict[str, Any] | None, list[dict[str, Any]]]:
    """Find completed files and metadata for the requested local download."""
    torrents = transmission_torrents()
    for torrent in torrents:
        if torrent_matches_payload(torrent, payload, root):
            completed = [
                path
                for path in completed_video_files_for_torrent(torrent)
                if path_contains(root, path)
                or path_contains(torrent_root_path(torrent), path)
            ]
            return completed, torrent, torrents
    return [], None, torrents


def library_destination(src: Path, torrent: dict[str, Any], media_type: str) -> Path:
    """Build the existing Kodi series or movie library destination."""
    torrent_name = str(torrent.get("name") or src.parent.name)
    if media_type == "series":
        show_title = clean_media_title(torrent_name, src.parent.name)
        season = season_number(src) or 1
        return KODI_SERIES_DIR / show_title / f"Season {season:02d}" / src.name

    movie_title = clean_media_title(torrent_name, src.stem)
    return KODI_MOVIES_DIR / movie_title / src.name


def latest_kodi_video_db() -> Path | None:
    """Find the highest-version Kodi video database in the configured folder."""
    database_dir = KODI_USERDATA_DIR / "Database"
    databases = sorted(
        database_dir.glob("MyVideos*.db"),
        key=lambda path: (
            int(re.search(r"(\d+)", path.stem).group(1))
            if re.search(r"(\d+)", path.stem)
            else 0
        ),
    )
    return databases[-1] if databases else None


def ensure_kodi_sources_xml() -> dict[str, Any]:
    """Update managed sources at the configured Kodi user-data location."""
    return ensure_sources_xml(KODI_USERDATA_DIR, KODI_SERIES_DIR, KODI_MOVIES_DIR)


def ensure_kodi_video_db_sources() -> dict[str, Any]:
    """Maintain the existing managed sources in the owner Kodi database."""
    db_path = latest_kodi_video_db()
    if db_path is None:
        return {"changed": False, "error": "Kodi video database was not found"}

    backup = db_path.with_suffix(db_path.suffix + ".ha-host-actions.bak")
    if not backup.exists():
        shutil.copy2(db_path, backup)

    changed = False
    rows: list[dict[str, Any]] = []
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        with conn:
            conn.execute("PRAGMA busy_timeout=5000")
            for path, content, scraper, use_folder_names in (
                (
                    KODI_SERIES_DIR,
                    "tvshows",
                    "metadata.tvshows.themoviedb.org.python",
                    0,
                ),
                (KODI_MOVIES_DIR, "movies", "metadata.themoviedb.org.python", 1),
            ):
                path_text = trailing_slash(path)
                row = conn.execute(
                    "select idPath, strContent, strScraper, scanRecursive, "
                    "useFolderNames, noUpdate, exclude from path where strPath = ?",
                    (path_text,),
                ).fetchone()
                if row:
                    desired = (content, scraper, 2147483647, use_folder_names, 0, 0)
                    current = (row[1], row[2], row[3], row[4], row[5], row[6])
                    updated = current != desired
                    if updated:
                        conn.execute(
                            """
                        update path
                           set strContent = ?, strScraper = ?, scanRecursive = ?,
                               useFolderNames = ?, noUpdate = ?, exclude = ?
                         where strPath = ?
                        """,
                            (*desired, path_text),
                        )
                        changed = True
                    rows.append(
                        {"path": path_text, "idPath": row[0], "updated": updated}
                    )
                else:
                    conn.execute(
                        """
                    insert into path
                      (strPath, strContent, strScraper, scanRecursive,
                       useFolderNames, noUpdate, exclude, dateAdded)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            path_text,
                            content,
                            scraper,
                            2147483647,
                            use_folder_names,
                            0,
                            0,
                            # Kodi dateAdded retains its existing naive local
                            # timestamp format.
                            datetime.now().isoformat(timespec="seconds"),  # noqa: DTZ005
                        ),
                    )
                    changed = True
                    rows.append(
                        {
                            "path": path_text,
                            "idPath": conn.execute(
                                "select last_insert_rowid()"
                            ).fetchone()[0],
                            "updated": False,
                        }
                    )
            conn.commit()
    finally:
        conn.close()

    return {"path": str(db_path), "changed": changed, "rows": rows}


def ensure_kodi_library_setup() -> dict[str, Any]:
    """Prepare managed source metadata and retain both update results."""
    result: dict[str, Any] = {}
    result["sources_xml"] = ensure_kodi_sources_xml()
    try:
        result["video_db"] = ensure_kodi_video_db_sources()
    except Exception as exc:  # noqa: BLE001 - report DB lock instead of hiding useful symlink sync.
        result["video_db"] = {"changed": False, "error": repr(exc)}
    return result


def _sync_completed_torrent(
    torrent: dict[str, Any],
    linked: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    touched_roots: set[Path],
) -> None:
    """Accumulate one torrent while retaining per-file error isolation."""
    candidates = completed_video_files_for_torrent(torrent)
    if not candidates:
        return
    root = torrent_root_path(torrent)
    media_type = detect_media_type(
        root, candidates, {"name": torrent.get("name")}, torrent
    )
    for src in candidates:
        if path_contains(KODI_LIBRARY_DIR, src):
            continue
        try:
            dest = library_destination(src, torrent, media_type)
            status = ensure_symlink(src, dest)
            if status == "created":
                touched_roots.add(
                    KODI_SERIES_DIR if media_type == "series" else KODI_MOVIES_DIR
                )
            linked.append(
                {
                    "source": str(src),
                    "library_path": str(dest),
                    "media_type": media_type,
                    "status": status,
                }
            )
        except Exception as exc:  # noqa: BLE001 - continue syncing other completed files.
            skipped.append({"source": str(src), "error": repr(exc)})


def _scan_completed_library(
    payload: dict[str, Any], setup: dict[str, Any], touched_roots: set[Path]
) -> list[dict[str, Any]]:
    """Scan Kodi under the existing changed-library conditions."""
    scan_result: list[dict[str, Any]] = []
    should_scan = bool(payload.get("scan", True))
    if should_scan and payload.get("start_kodi"):
        start_kodi()
    if (
        should_scan
        and kodi_running()
        and (
            touched_roots
            or setup.get("sources_xml", {}).get("changed")
            or setup.get("video_db", {}).get("changed")
        )
    ):
        try:
            wait_for_kodi_jsonrpc(timeout=20)
            scan_result.extend(
                {
                    "directory": str(root),
                    "result": kodi_jsonrpc(
                        "VideoLibrary.Scan",
                        {"directory": trailing_slash(root)},
                        timeout=3,
                    ),
                }
                for root in sorted(
                    touched_roots or {KODI_SERIES_DIR, KODI_MOVIES_DIR}, key=str
                )
            )
        except Exception as exc:  # noqa: BLE001 - return scan diagnostics to HA.
            scan_result.append({"error": repr(exc)})

    return scan_result


def sync_completed_files_to_kodi(
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Link completed downloads and scan only affected Kodi library roots."""
    payload = payload or {}
    setup = ensure_kodi_library_setup()
    torrents = transmission_torrents()
    linked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    touched_roots: set[Path] = set()

    for torrent in torrents:
        _sync_completed_torrent(torrent, linked, skipped, touched_roots)
    scan_result = _scan_completed_library(payload, setup, touched_roots)

    return {
        "library_dir": str(KODI_LIBRARY_DIR),
        "setup": setup,
        "linked_count": len([item for item in linked if item["status"] == "created"]),
        "known_count": len(linked),
        "linked": linked[:50],
        "skipped": skipped[:20],
        "scan": scan_result,
    }


def wait_for_kodi_jsonrpc(timeout: float = 25.0) -> dict[str, Any]:
    """Wait for the configured Kodi API to become available."""
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            return kodi_jsonrpc(
                "Application.GetProperties",
                {"properties": ["name", "version"]},
                timeout=2,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
            time.sleep(1)
    error_message = f"Kodi JSON-RPC is not ready: {last_error}"
    raise RuntimeError(error_message)


def kodi_play_path(payload: dict[str, Any]) -> dict[str, Any]:
    """Choose a completed local video and request confirmed Kodi playback."""
    raw_path = str(payload.get("path") or "").strip()
    if not raw_path:
        error_message = "path is required"
        raise ValueError(error_message)

    root = Path(raw_path).expanduser()
    if not root.exists():
        error_message = f"path does not exist: {root}"
        raise FileNotFoundError(error_message)

    rpc_error = ""
    matched_torrent: dict[str, Any] | None = None
    try:
        completed_candidates, matched_torrent, _ = completed_video_files_for_payload(
            payload, root
        )
    except Exception as exc:  # noqa: BLE001 - allow manual local playback without Transmission RPC.
        completed_candidates = []
        rpc_error = repr(exc)

    if matched_torrent is not None:
        candidates = completed_candidates
        if not candidates:
            error_message = (
                f"no fully downloaded video files are available under {root}"
            )
            raise FileNotFoundError(error_message)
    else:
        candidates = video_candidates(root)

    media_type = detect_media_type(root, candidates, payload, matched_torrent)
    selected = choose_video_file_from_candidates(candidates, media_type)
    sync_result = sync_completed_files_to_kodi({"scan": False})
    start_result = start_kodi()
    ready = wait_for_kodi_jsonrpc()
    scan_dir = str(selected.parent if selected.is_file() else selected)
    scan_result = kodi_jsonrpc("VideoLibrary.Scan", {"directory": scan_dir}, timeout=3)
    open_result = kodi_jsonrpc(
        "Player.Open", {"item": {"file": str(selected)}}, timeout=8
    )
    time.sleep(2)
    players = kodi_jsonrpc("Player.GetActivePlayers", timeout=3)
    active_players = []
    body = players.get("body")
    if isinstance(body, dict):
        active_players = body.get("result") or []
    if not active_players:
        error_message = f"Kodi did not start playback for {selected}: {open_result}"
        raise RuntimeError(error_message)
    return {
        "kodi": "playing",
        "requested_path": str(root),
        "selected_file": str(selected),
        "media_type": media_type,
        "transmission_rpc_error": rpc_error,
        "library_sync": {
            "linked_count": sync_result.get("linked_count"),
            "known_count": sync_result.get("known_count"),
            "skipped": sync_result.get("skipped", [])[:3],
        },
        "start": start_result,
        "ready": ready,
        "scan": scan_result,
        "open": open_result,
        "players": players,
    }


def agent_browser(
    args: list[str], timeout: float = 30.0, input_data: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the configured browser helper within its existing named session."""
    if not AGENT_BROWSER.exists():
        return subprocess.CompletedProcess(
            [str(AGENT_BROWSER), *args],
            127,
            "",
            f"{AGENT_BROWSER} is not installed",
        )
    return run(
        [str(AGENT_BROWSER), "--session", RUTRACKER_BROWSER_SESSION, *args],
        timeout=timeout,
        input_data=input_data,
    )


def browser_cookie_header(stdout: str) -> str:
    """Extract the existing site cookie header from browser JSON output."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return ""

    cookies = payload
    if isinstance(payload, dict):
        cookies = payload.get("data", {}).get("cookies", [])
    if not isinstance(cookies, list):
        return ""

    parts: list[str] = []
    seen: set[str] = set()
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "")
        if "rutracker.org" not in domain:
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def rutracker_browser_login(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the existing owner-requested browser login workflow."""
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        return {"ok": False, "error": "username_and_password_required"}

    opened = agent_browser(["open", RUTRACKER_LOGIN_URL], timeout=45)
    if opened.returncode != 0:
        return {
            "ok": False,
            "error": "agent_browser_open_failed",
            "stderr": opened.stderr.strip()[-500:],
        }

    script = f"""
(() => {{
  const username = {json.dumps(username)};
  const password = {json.dumps(password)};
  const pick = (selectors) => selectors
    .map((selector) => document.querySelector(selector)).find(Boolean);
  const setValue = (element, value) => {{
    element.focus();
    const descriptor = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype, "value");
    if (descriptor && descriptor.set) {{
      descriptor.set.call(element, value);
    }} else {{
      element.value = value;
    }}
    element.dispatchEvent(new Event("input", {{ bubbles: true }}));
    element.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }};

  const user = pick([
    'input[name="login_username"]',
    'input[name="username"]',
    'input[name="user"]',
    'input[type="email"]',
    'input[type="text"]'
  ]);
  const pass = pick([
    'input[name="login_password"]',
    'input[name="password"]',
    'input[type="password"]'
  ]);
  if (!user || !pass) {{
    return {{
      ok: false,
      reason: "login_fields_not_found",
      url: location.href,
      title: document.title,
      text: (document.body && document.body.innerText || "").slice(0, 300)
    }};
  }}

  setValue(user, username);
  setValue(pass, password);
  const form = pass.form || user.form || document.querySelector("form");
  const submit = form && form.querySelector(
    'input[type="submit"], button[type="submit"], ' +
    'input[name="login"], button[name="login"]');
  if (submit) {{
    submit.click();
  }} else if (form && typeof form.submit === "function") {{
    form.submit();
  }} else {{
    pass.dispatchEvent(new KeyboardEvent("keydown", {{
      key: "Enter", code: "Enter", bubbles: true }}));
  }}
  return {{ ok: true, url: location.href, title: document.title }};
}})()
"""
    submitted = agent_browser(
        ["--json", "eval", "--stdin"], timeout=15, input_data=script
    )
    if submitted.returncode != 0:
        return {
            "ok": False,
            "error": "agent_browser_submit_failed",
            "stderr": submitted.stderr.strip()[-500:],
        }

    agent_browser(["wait", "6000"], timeout=10)
    cookies = agent_browser(["--json", "cookies", "get"], timeout=15)
    cookie_header = browser_cookie_header(cookies.stdout)
    current_url = agent_browser(["get", "url"], timeout=10)
    title = agent_browser(["get", "title"], timeout=10)

    if not cookie_header:
        return {
            "ok": False,
            "error": "no_rutracker_cookies_returned",
            "url": current_url.stdout.strip(),
            "title": title.stdout.strip(),
        }

    return {
        "ok": True,
        "method": "agent-browser",
        "cookie_header": cookie_header,
        "cookies_count": cookie_header.count("="),
        "url": current_url.stdout.strip(),
        "title": title.stdout.strip(),
        "message": "agent-browser login produced RuTracker.org cookies",
    }


def health() -> dict[str, Any]:
    """Report current host process and desktop probe status."""
    active = dbus_call(
        [
            "--dest",
            "org.gnome.ScreenSaver",
            "--object-path",
            "/org/gnome/ScreenSaver",
            "--method",
            "org.gnome.ScreenSaver.GetActive",
        ],
        # HA's health probe gives up after 3 s; a stuck gnome-shell must not stall it.
        timeout=1.5,
    )
    return {
        "ok": True,
        "kodi_running": kodi_running(),
        "screen_active_raw": active.get("stdout"),
        "password_file_present": PASSWORD_FILE.exists(),
    }


# Network devices (router-cli). HA's network-devices card reads the inventory and
# pins/unpins DHCP leases through these endpoints. Every argument is validated and
# passed as an argv list — never through a shell. `dry_run: true` in a body makes
# pin/unpin pass --dry-run so the flow can be tested without touching the router.
ROUTER_BIN = (
    os.environ.get("ROUTER_BIN")
    or shutil.which("router")
    or str(Path.home() / ".local/bin/router")
)
MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
SINCE_RE = re.compile(r"^\d{1,4}[smhd]$")
MDI_RE = re.compile(r"^mdi:[a-z0-9-]{1,64}$")
INVENTORY_FILTERS = {"recent", "active", "all", "reserved", "new"}
SCAN_WAIT = 20.0
_scan_lock = threading.Lock()
_scan_state: dict[str, Any] = {"running": None, "started_at": None, "last": None}


# The existing class name is emitted as error_class in the audit protocol.
class InvalidInput(ValueError):  # noqa: N818
    """Bad request data (reported to HA as HTTP 400)."""


def router_cmd(
    args: list[str], timeout: float = 25.0
) -> subprocess.CompletedProcess[str]:
    """Run the configured router utility with a validated argument list."""
    if not Path(ROUTER_BIN).exists():
        error_message = f"router CLI not found at {ROUTER_BIN}"
        raise RuntimeError(error_message)
    # The configured executable receives validated arguments without a shell.
    return subprocess.run(  # noqa: S603
        [ROUTER_BIN, *args],
        text=True,
        capture_output=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def router_result(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Decode the existing router result or retain bounded error output."""
    if proc.returncode != 0:
        error_message = (
            f"router failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[-500:]}"
        )
        raise RuntimeError(error_message)
    out = proc.stdout.strip()
    try:
        parsed: Any = json.loads(out) if out else None
    except json.JSONDecodeError:
        parsed = None
    result: dict[str, Any] = {
        "returncode": proc.returncode,
        "stderr": proc.stderr.strip()[-800:],
    }
    if isinstance(parsed, (dict, list)):
        result["result"] = parsed
    else:
        result["stdout"] = out[-2000:]
    return result


def first_value(value: Any) -> Any:
    # GET query params arrive as lists (parse_qs); JSON bodies as scalars.
    """Unwrap the first query parameter value when it is a list."""
    return value[0] if isinstance(value, list) and value else value


def valid_mac(value: Any) -> str:
    """Validate and normalize a single hardware address."""
    mac = str(first_value(value) or "").strip().lower().replace("-", ":")
    if not MAC_RE.match(mac):
        error_message = "invalid_mac"
        raise InvalidInput(error_message)
    return mac


def valid_ip(value: Any) -> str:
    """Validate the requested address against the existing IPv4 policy."""
    try:
        ip = ipaddress.ip_address(str(first_value(value) or "").strip())
    except ValueError as exc:
        error_message = "invalid_ip"
        raise InvalidInput(error_message) from exc
    if (
        ip.version != _IPV4_VERSION
        or not ip.is_private
        or ip.is_loopback
        or ip.is_multicast
        or ip.is_unspecified
    ):
        error_message = "invalid_ip"
        raise InvalidInput(error_message)
    return str(ip)


def valid_name(value: Any) -> str:
    """Validate the existing reservation-name length and character policy."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(first_value(value) or "")).strip()[:64]
    if name.startswith("-"):
        error_message = "invalid_name"
        raise InvalidInput(error_message)
    return name


def is_dry_run(payload: dict[str, Any]) -> bool:
    """Read the existing explicit dry-run option from a request."""
    return str(first_value(payload.get("dry_run", False))).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _inventory_request(payload: dict[str, Any]) -> tuple[list[str], bool]:
    """Validate the inventory query and select its favicon fast path."""
    flt = str(first_value(payload.get("filter")) or "all")
    if flt not in INVENTORY_FILTERS:
        error_message = "invalid_filter"
        raise InvalidInput(error_message)
    args = ["inventory", "list", "--json", "--filter", flt]
    since = str(first_value(payload.get("since")) or "")
    if since:
        if not SINCE_RE.match(since):
            error_message = "invalid_since"
            raise InvalidInput(error_message)
        args += ["--since", since]
    no_favicons = str(first_value(payload.get("favicons", "1"))).lower() in {
        "0",
        "false",
        "no",
    }
    if no_favicons:
        # Fast path: the CLI never even reads the favicon blobs (the full answer is
        # ~100 KB).
        args.append("--no-favicons")
    return args, no_favicons


def _clear_inventory_favicons(data: dict[str, Any]) -> None:
    """Keep the no-favicon contract with older CLI responses."""
    for device in data["devices"]:
        for service in device.get("services") or []:
            service["favicon_data_url"] = None


def network_inventory(payload: dict[str, Any]) -> dict[str, Any]:
    """Read the local inventory with validated filter and favicon options."""
    args, no_favicons = _inventory_request(payload)
    proc = router_cmd(args, timeout=20)
    if proc.returncode != 0:
        error_message = (
            f"router inventory failed ({proc.returncode}): {proc.stderr.strip()[-500:]}"
        )
        raise RuntimeError(error_message)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        error_message = "router inventory returned invalid JSON"
        raise RuntimeError(error_message) from exc
    if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        error_message = "router inventory returned unexpected JSON"
        # Preserve the existing RuntimeError response for invalid upstream data.
        raise RuntimeError(error_message)  # noqa: TRY004
    if no_favicons:
        _clear_inventory_favicons(data)
    data["scan"] = {
        "running": _scan_state["running"],
        "started_at": _scan_state["started_at"],
        "last": _scan_state["last"],
    }
    return data


# History / stats (router-cli `inventory history|stats`, local DB only — never the
# router).
# Responses are capped: at most HISTORY_MAX_BUCKETS buckets (the bucket grows to fit the
# requested days) and at most STATS_MAX_HOURS entries in per_hour (averaged down to
# fit).
BUCKET_RE = re.compile(r"^\d{1,3}[mhd]$")
HISTORY_MAX_BUCKETS = 400
STATS_MAX_HOURS = 336
_BUCKET_STEPS = [
    (300, "5m"),
    (900, "15m"),
    (1800, "30m"),
    (3600, "1h"),
    (10800, "3h"),
    (21600, "6h"),
    (43200, "12h"),
    (86400, "1d"),
]


def valid_days(value: Any, default: float = 7.0) -> float:
    """Validate the existing history duration and bounds."""
    raw = first_value(value)
    if raw in (None, ""):
        return default
    try:
        days = float(str(raw))
    except ValueError as exc:
        error_message = "invalid_days"
        raise InvalidInput(error_message) from exc
    if not 0 < days <= _HISTORY_MAX_DAYS:
        error_message = "invalid_days"
        raise InvalidInput(error_message)
    return days


def valid_device(value: Any) -> str:
    """Validate a device identifier or address for inventory queries."""
    raw = str(first_value(value) or "").strip()
    if not raw:
        error_message = "device_required"
        raise InvalidInput(error_message)
    try:
        return valid_mac(raw)
    except InvalidInput:
        pass
    try:
        return valid_ip(raw)
    except InvalidInput:
        pass
    name = valid_name(raw)
    if not name:
        error_message = "invalid_device"
        raise InvalidInput(error_message)
    return name


def _bucket_seconds(text: str) -> int:
    return int(text[:-1]) * {"m": 60, "h": 3600, "d": 86400}[text[-1]]


def network_history(payload: dict[str, Any]) -> dict[str, Any]:
    """Read the requested inventory history with the configured bucket size."""
    device = valid_device(payload.get("device") or payload.get("mac"))
    days = valid_days(payload.get("days"))
    bucket = str(first_value(payload.get("bucket")) or "1h")
    if (
        not BUCKET_RE.match(bucket)
        or not _HISTORY_MIN_BUCKET_SECONDS
        <= _bucket_seconds(bucket)
        <= _SECONDS_PER_DAY
    ):
        error_message = "invalid_bucket"
        raise InvalidInput(error_message)
    for seconds, label in _BUCKET_STEPS:
        if (
            seconds >= _bucket_seconds(bucket)
            and days * 86400 / seconds <= HISTORY_MAX_BUCKETS
        ):
            bucket = label
            break
    args = [
        "inventory",
        "history",
        device,
        "--json",
        "--days",
        f"{days:g}",
        "--bucket",
        bucket,
    ]
    result = router_result(router_cmd(args, timeout=20))
    data = result.get("result")
    if not isinstance(data, dict) or not isinstance(data.get("buckets"), list):
        error_message = (
            "router inventory history failed: "
            f"{result.get('stderr') or result.get('stdout')}"
        )
        # Preserve the existing RuntimeError response for invalid upstream data.
        raise RuntimeError(error_message)  # noqa: TRY004
    return data


def _downsample_hours(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(rows) <= limit:
        return rows
    step = -(-len(rows) // limit)
    out = []
    for i in range(0, len(rows), step):
        group = rows[i : i + step]
        values = [r["online"] for r in group if r.get("online") is not None]
        out.append(
            {
                "t": group[0]["t"],
                "online": round(sum(values) / len(values), 1) if values else None,
                "samples": sum(int(r.get("samples") or 0) for r in group),
                "hours": len(group),
            }
        )
    return out


def network_stats(payload: dict[str, Any]) -> dict[str, Any]:
    """Read inventory statistics and bound the returned hourly samples."""
    days = valid_days(payload.get("days"))
    result = router_result(
        router_cmd(["inventory", "stats", "--json", "--days", f"{days:g}"], timeout=30)
    )
    data = result.get("result")
    if not isinstance(data, dict) or not isinstance(data.get("per_hour"), list):
        error_message = (
            "router inventory stats failed: "
            f"{result.get('stderr') or result.get('stdout')}"
        )
        # Preserve the existing RuntimeError response for invalid upstream data.
        raise RuntimeError(error_message)  # noqa: TRY004
    data["per_hour"] = _downsample_hours(data["per_hour"], STATS_MAX_HOURS)
    return data


def _scan_worker(args: list[str], label: str) -> None:
    try:
        # The existing validated router argument list is executed without a shell.
        proc = subprocess.run(  # noqa: S603
            [ROUTER_BIN, *args],
            text=True,
            capture_output=True,
            timeout=1800,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        _scan_state["last"] = {
            "target": label,
            "returncode": proc.returncode,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "stderr": proc.stderr.strip()[-300:],
        }
    except Exception as exc:  # noqa: BLE001 - background thread, record and move on.
        _scan_state["last"] = {"target": label, "error": repr(exc)}
    finally:
        _scan_state["running"] = None
        _scan_state["started_at"] = None
        _scan_lock.release()


def network_scan(payload: dict[str, Any]) -> dict[str, Any]:
    """Start one existing validated router scan and report its worker state."""
    ip = first_value(payload.get("ip"))
    if ip:
        ip = valid_ip(ip)
        args, label = ["scan", "--ip", ip, "--json"], ip
    else:
        args, label = ["scan", "--all-online", "--json"], "all-online"
    if not Path(ROUTER_BIN).exists():
        error_message = f"router CLI not found at {ROUTER_BIN}"
        raise RuntimeError(error_message)
    if not _scan_lock.acquire(blocking=False):
        return {"started": False, "busy": True, "running": _scan_state["running"]}
    _scan_state["running"] = label
    _scan_state["started_at"] = (
        datetime.now().astimezone().isoformat(timespec="seconds")
    )
    worker = threading.Thread(target=_scan_worker, args=(args, label), daemon=True)
    worker.start()
    # A single-host scan usually finishes quickly: wait briefly so the card can refresh
    # right away. Full scans (and slow hosts) keep running in the background.
    worker.join(SCAN_WAIT if label != "all-online" else 0)
    done = not worker.is_alive()
    return {
        "started": True,
        "target": label,
        "done": done,
        "last": _scan_state["last"] if done else None,
    }


# Static-lease writes are serialized. router-cli's reserve/unreserve change the router
# but
# not the local inventory DB (its reserved_ip is refreshed only by `inventory
# update`), so a
# successful write is followed by exactly one `router inventory update` — otherwise
# the card
# would keep showing the old state until the hourly poll.
_write_lock = threading.Lock()
SLOTS_FULL_RE = re.compile(r"all (\d+) static lease slots are in use")


def _stdout_line(message: str) -> None:
    """Write one existing CLI diagnostic with its original newline and flush."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _refresh_after_write() -> dict[str, Any]:
    if not _update_lock.acquire(timeout=60):
        return {"refreshed": False, "reason": "busy"}
    try:
        result = router_result(
            router_cmd(["inventory", "update", "--json", "--wait", "40"], timeout=60)
        )
        summary = result.get("result") if isinstance(result.get("result"), dict) else {}
        _stdout_line(
            "network write: inventory refreshed, "
            f"{summary.get('reservations')} reservations"
        )
        return {
            "refreshed": True,
            "last_poll": summary.get("at"),
            "reservations": summary.get("reservations"),
        }
    except Exception as exc:  # noqa: BLE001 - the write itself succeeded; report, don't fail it.
        _stdout_line(f"network write: inventory refresh failed: {exc!r}")
        return {"refreshed": False, "error": str(exc)[-300:]}
    finally:
        _update_lock.release()


def _reserve(mac: str, ip: str, name: str, *, dry_run: bool) -> dict[str, Any]:
    args = ["reserve", mac, ip, "--yes", "--json"]
    if name:
        args += ["--name", name]
    if dry_run:
        args.append("--dry-run")
    proc = router_cmd(args, timeout=60)
    if proc.returncode != 0:
        m = SLOTS_FULL_RE.search(f"{proc.stderr}\n{proc.stdout}")
        if m:
            # Structured for the card: it offers to replace one of the existing
            # reservations.
            error_message = f"slots_full:{m.group(1)}"
            raise RuntimeError(error_message)
    return router_result(proc)


def _unreserve(mac: str, *, dry_run: bool) -> dict[str, Any]:
    args = ["unreserve", mac, "--yes", "--json"]
    if dry_run:
        args.append("--dry-run")
    return router_result(router_cmd(args, timeout=60))


def network_pin(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply a validated static reservation under the existing write lock."""
    mac, ip = valid_mac(payload.get("mac")), valid_ip(payload.get("ip"))
    name = valid_name(payload.get("name"))
    dry_run = is_dry_run(payload)
    with _write_lock:
        out = {
            "action": "pin",
            "mac": mac,
            "ip": ip,
            "dry_run": dry_run,
            **_reserve(mac, ip, name, dry_run=dry_run),
        }
        if not dry_run:
            out["inventory"] = _refresh_after_write()
    return out


def network_unpin(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove a validated static reservation under the existing write lock."""
    mac = valid_mac(payload.get("mac"))
    dry_run = is_dry_run(payload)
    with _write_lock:
        out = {
            "action": "unpin",
            "mac": mac,
            "dry_run": dry_run,
            **_unreserve(mac, dry_run=dry_run),
        }
        if not dry_run:
            out["inventory"] = _refresh_after_write()
    return out


def _reserved_devices() -> dict[str, dict[str, Any]]:
    """Read current reservations from the local inventory DB without router traffic."""
    proc = router_cmd(
        ["inventory", "list", "--json", "--filter", "reserved", "--no-favicons"],
        timeout=20,
    )
    if proc.returncode != 0:
        error_message = (
            f"router inventory failed ({proc.returncode}): {proc.stderr.strip()[-300:]}"
        )
        raise RuntimeError(error_message)
    data = json.loads(proc.stdout)
    return {
        str(d.get("mac")): d for d in data.get("devices") or [] if d.get("reserved_ip")
    }


def network_pin_replace(payload: dict[str, Any]) -> dict[str, Any]:
    """
    All static-lease slots are taken: free `replace_mac`'s slot, then pin `mac` to `ip`.

    If the new reservation fails, the old one is restored (same MAC and IP).
    """
    old_mac = valid_mac(payload.get("replace_mac"))
    mac, ip = valid_mac(payload.get("mac")), valid_ip(payload.get("ip"))
    name = valid_name(payload.get("name"))
    dry_run = is_dry_run(payload)
    if old_mac == mac:
        error_message = "same_device"
        raise InvalidInput(error_message)
    with _write_lock:
        old = _reserved_devices().get(old_mac)
        if not old:
            error_message = "not_reserved"
            raise InvalidInput(error_message)
        if old.get("is_self"):
            # This server's LAN IP is hardcoded in HA's config (internal_url, go2rtc).
            error_message = "protected_reservation"
            raise InvalidInput(error_message)
        old_ip = valid_ip(old.get("reserved_ip"))
        out: dict[str, Any] = {
            "action": "pin-replace",
            "replace_mac": old_mac,
            "replace_ip": old_ip,
            "mac": mac,
            "ip": ip,
            "dry_run": dry_run,
        }
        _stdout_line(
            f"network pin-replace: {old_mac} {old_ip} -> {mac} {ip} (dry_run={dry_run})"
        )
        out["unreserve"] = _unreserve(old_mac, dry_run=dry_run)
        try:
            out["reserve"] = _reserve(mac, ip, name, dry_run=dry_run)
        except Exception as exc:
            if dry_run:
                raise
            _stdout_line(
                f"network pin-replace: reserve failed ({exc!r}); "
                f"restoring {old_mac} {old_ip}"
            )
            try:
                _reserve(old_mac, old_ip, "", dry_run=False)
                rollback = "restored"
            except Exception as exc2:  # noqa: BLE001 - report both failures.
                rollback = f"FAILED: {exc2}"
            _refresh_after_write()
            error_message = f"reserve failed: {exc}; old reservation {rollback}"
            raise RuntimeError(error_message) from exc
        if not dry_run:
            out["inventory"] = _refresh_after_write()
    return out


def network_alias(payload: dict[str, Any]) -> dict[str, Any]:
    """Update the local inventory alias and icon after input validation."""
    mac = valid_mac(payload.get("mac"))
    name = valid_name(payload.get("name"))
    icon = str(first_value(payload.get("icon")) or "").strip().lower()
    if icon and not MDI_RE.match(icon):
        error_message = "invalid_icon"
        raise InvalidInput(error_message)
    if not name and not icon:
        error_message = "name_or_icon_required"
        raise InvalidInput(error_message)
    args = ["alias", mac, "--json"]
    if name:
        args += ["--name", name]
    if icon:
        args += ["--icon", icon]
    return {
        "action": "alias",
        "mac": mac,
        **router_result(router_cmd(args, timeout=15)),
    }


# The router (Ubee) hangs when polled often. It is polled ONLY by the hourly
# router-inventory-update.timer and by this endpoint (the card's "Обновить" button / the
# button.network_devices_update entity). Requests are serialized, and a poll younger
# than
# NETWORK_UPDATE_MIN_AGE seconds is reused instead of hitting the router again.
NETWORK_UPDATE_MIN_AGE = float(os.environ.get("NETWORK_UPDATE_MIN_AGE", "60"))
ROUTER_DB = Path(
    os.environ.get("ROUTER_CLI_DB")
    or Path.home() / ".local/share/router-cli/inventory.sqlite3"
)
_update_lock = threading.Lock()


def last_poll() -> tuple[str | None, float | None]:
    """(ISO time, age in seconds) of the last inventory poll, read from the local DB."""
    try:
        con = sqlite3.connect(f"file:{ROUTER_DB}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT value FROM meta WHERE key = 'last_poll'"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None, None
    if not row or not row[0]:
        return None, None
    try:
        age = time.time() - datetime.fromisoformat(str(row[0])).timestamp()
    except ValueError:
        return str(row[0]), None
    return str(row[0]), age


def network_update(_payload: dict[str, Any]) -> dict[str, Any]:
    """Coalesce inventory refreshes and preserve freshness diagnostics."""
    at, age = last_poll()
    if age is not None and age < NETWORK_UPDATE_MIN_AGE:
        _stdout_line(f"network update: skipped, last poll {age:.0f}s ago")
        return {
            "polled": False,
            "reason": "fresh",
            "last_poll": at,
            "age_s": round(age),
        }
    if not _update_lock.acquire(timeout=45):
        return {"polled": False, "reason": "busy", "last_poll": at}
    try:
        # Another request may have polled while this one waited for the lock.
        at, age = last_poll()
        if age is not None and age < NETWORK_UPDATE_MIN_AGE:
            _stdout_line(f"network update: joined a poll {age:.0f}s old")
            return {
                "polled": False,
                "reason": "fresh",
                "last_poll": at,
                "age_s": round(age),
            }
        _stdout_line("network update: polling the router (router inventory update)")
        started = time.monotonic()
        result = router_result(
            router_cmd(["inventory", "update", "--json", "--wait", "40"], timeout=60)
        )
        took = time.monotonic() - started
        summary = result.get("result") if isinstance(result.get("result"), dict) else {}
        _stdout_line(f"network update: done in {took:.1f}s, seen {summary.get('seen')}")
        return {
            "polled": not summary.get("skipped", False),
            "last_poll": summary.get("at") or last_poll()[0],
            "seen": summary.get("seen"),
            "new": summary.get("new"),
            "went_offline": summary.get("went_offline"),
            "took_s": round(took, 1),
        }
    finally:
        _update_lock.release()


def network_summary(_query: dict[str, Any]) -> dict[str, Any]:
    """Build a compact local-database view without querying the router."""
    proc = router_cmd(["inventory", "list", "--json", "--filter", "all"], timeout=20)
    if proc.returncode != 0:
        error_message = (
            f"router inventory failed ({proc.returncode}): {proc.stderr.strip()[-300:]}"
        )
        raise RuntimeError(error_message)
    data = json.loads(proc.stdout)
    devices = data.get("devices") or []
    online = [d for d in devices if d.get("online")]
    day_ago = time.time() - 86400

    def ts(value: Any) -> float:
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except ValueError:
            return 0.0

    def label(d: dict[str, Any]) -> str:
        return str(d.get("hostname") or d.get("vendor") or d.get("mac"))

    return {
        "last_poll": data.get("last_poll"),
        "online_count": len(online),
        "known_count": len(devices),
        "reserved_count": sum(1 for d in devices if d.get("reserved_ip")),
        "new_24h": [
            {"name": label(d), "mac": d.get("mac"), "ip": d.get("ip")}
            for d in devices
            if d.get("first_seen") and ts(d["first_seen"]) >= day_ago
        ],
        "online": [
            {"name": label(d), "ip": d.get("ip"), "mac": d.get("mac")} for d in online
        ],
        "online_macs": [d.get("mac") for d in online],
        "online_names": sorted({label(d) for d in online}, key=str.casefold),
    }


ROUTES = {
    ("POST", "/projector/on"): projector_on,
    ("POST", "/projector/off"): projector_off,
    ("GET", "/kodi/status"): kodi_status,
    ("POST", "/kodi/start"): start_kodi,
    ("POST", "/kodi/stop"): stop_kodi,
    ("POST", "/kodi/text-input"): kodi_text_input,
    ("POST", "/kodi/keyboard"): kodi_keyboard,
    ("POST", "/kodi/play-path"): kodi_play_path,
    ("POST", "/kodi/sync-library"): sync_completed_files_to_kodi,
    ("POST", "/screen/unlock"): wake_and_unlock_screen,
    ("POST", "/screen/lock"): lock_screen,
    ("POST", "/rutracker/login"): rutracker_browser_login,
    ("GET", "/health"): health,
    ("GET", "/network/inventory"): network_inventory,
    ("POST", "/network/inventory"): network_inventory,
    ("POST", "/network/scan"): network_scan,
    ("POST", "/network/pin"): network_pin,
    ("POST", "/network/unpin"): network_unpin,
    ("POST", "/network/pin-replace"): network_pin_replace,
    ("POST", "/network/alias"): network_alias,
    ("POST", "/network/update"): network_update,
    ("GET", "/network/summary"): network_summary,
    ("GET", "/network/history"): network_history,
    ("POST", "/network/history"): network_history,
    ("GET", "/network/stats"): network_stats,
    ("POST", "/network/stats"): network_stats,
}

BODY_ROUTES = {
    ("POST", "/network/update"),
    ("POST", "/kodi/text-input"),
    ("POST", "/kodi/keyboard"),
    ("POST", "/kodi/play-path"),
    ("POST", "/kodi/sync-library"),
    ("POST", "/rutracker/login"),
    ("POST", "/network/inventory"),
    ("POST", "/network/scan"),
    ("POST", "/network/pin"),
    ("POST", "/network/unpin"),
    ("POST", "/network/pin-replace"),
    ("POST", "/network/alias"),
    ("POST", "/network/history"),
    ("POST", "/network/stats"),
}
QUERY_ROUTES = {
    ("GET", "/network/inventory"),
    ("GET", "/network/summary"),
    ("GET", "/network/history"),
    ("GET", "/network/stats"),
}


class Handler(BaseHTTPRequestHandler):
    """Serve the existing authenticated host routes with bounded audit records."""

    token = read_secret(TOKEN_FILE)

    def log_message(self, _fmt: str, *_args: Any) -> None:
        # BaseHTTPRequestHandler logs the raw request target, including query
        # strings. handle_route emits a sanitized record instead.
        """Suppress the standard raw-request log in favor of sanitized records."""
        return

    def authorized(self) -> bool:
        """Compare the supplied host token with the configured credential."""
        supplied = self.headers.get("X-HA-Host-Token", "")
        if not self.token:
            return False
        return hmac.compare_digest(supplied, self.token)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        """Write the existing UTF-8 JSON response and exact content length."""
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> dict[str, Any]:
        """Decode an optional JSON object using the existing error contract."""
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            error_message = "JSON body must be an object"
            # Preserve the existing ValueError JSON response and emitted audit class.
            raise ValueError(error_message)  # noqa: TRY004
        return parsed

    def handle_route(self) -> None:
        """Authorize and dispatch a registered route with sanitized audit metadata."""
        started = time.monotonic()
        url = urllib.parse.urlsplit(self.path)
        route_key = (self.command, url.path)
        route = ROUTES.get(route_key)
        label = url.path if route is not None else "unknown"
        status, error_class = 500, None
        try:
            if route is None:
                status = 404
                self.send_json(status, {"ok": False, "error": "not_found"})
                return
            if not self.authorized():
                status = 403
                self.send_json(status, {"ok": False, "error": "forbidden"})
                return
            if route_key in BODY_ROUTES:
                result = route(self.read_json_body())
            elif route_key in QUERY_ROUTES:
                result = route(urllib.parse.parse_qs(url.query))
            else:
                result = route()
            status = 200
            self.send_json(status, {"ok": True, **result})
        except KeyboardFailure as exc:
            status = 422 if exc.code == "invalid_input" else 409
            error_class = type(exc).__name__
            self.send_json(status, {"ok": False, "error": exc.code})
        except InvalidInput as exc:
            status, error_class = 400, type(exc).__name__
            self.send_json(status, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - keep bridge alive and report error to HA.
            status, error_class = 500, type(exc).__name__
            self.send_json(status, {"ok": False, "error": repr(exc)})
        finally:
            _audit(
                "request",
                route=label,
                method=self.command,
                status=status,
                error_class=error_class,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )

    def do_GET(self) -> None:
        """Dispatch a GET request through the shared authenticated route boundary."""
        self.handle_route()

    def do_POST(self) -> None:
        """Dispatch a POST request through the shared authenticated route boundary."""
        self.handle_route()


def main() -> None:
    """Serve the existing authenticated routes and flush private audit records."""
    path, started = _start_audit()

    # systemd uses SIGTERM; unwind through the same sanitized completion record.
    def stop_requested(_signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_requested)
    server = None
    outcome, error_class = "stopped", None
    try:
        bind = os.environ.get("HA_HOST_ACTIONS_BIND", "172.20.0.1")
        port = int(os.environ.get("HA_HOST_ACTIONS_PORT", "8787"))
        server = ThreadingHTTPServer((bind, port), Handler)
        _audit("listener", outcome="ready", log=str(path))
        server.serve_forever()
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)) and getattr(
            exc, "code", 0
        ) in {None, 0}:
            outcome = "stopped"
        else:
            outcome, error_class = "failed", type(exc).__name__
        raise
    finally:
        if server is not None:
            server.server_close()
        _audit(
            "completion",
            outcome=outcome,
            error_class=error_class,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            log=str(path),
        )
        for handler in _AUDIT.handlers:
            handler.flush()


if __name__ == "__main__":
    main()
