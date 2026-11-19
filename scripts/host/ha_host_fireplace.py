"""Fireplace HTTP glue and bounded Kodi shutdown for the host bridge."""

from __future__ import annotations

import importlib.util
import re
import threading
import time
import urllib.error
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


def _load_host_module(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_fireplace_module = _load_host_module("ha_kodi_fireplace")
_manifest_module = _load_host_module("ha_kodi_youtube_manifest")
FireplaceError = _fireplace_module.FireplaceError
ManifestError = _manifest_module.ManifestError


class FireplaceEndpoints:
    """Bind existing controller operations to the bridge's native RPC and config."""

    def __init__(
        self,
        rpc: Callable[..., dict[str, Any]],
        config_dir: Path,
        invalid_input: type[Exception],
        *,
        audit: Callable[..., None] | None = None,
    ) -> None:
        """Capture configured host dependencies without starting playback."""
        self._rpc = rpc
        self.config_dir = config_dir
        self.invalid_input = invalid_input
        self._audit = audit
        self._failure_logged_at: dict[tuple[str, str, str], float] = {}
        self._controller = None
        self._lock = threading.Lock()

    def _audit_rpc(
        self,
        method: str,
        outcome: str,
        started: float,
        error: BaseException | None = None,
    ) -> None:
        """Emit bounded, redacted transport diagnostics through the host audit."""
        if self._audit is None:
            return
        error_class = type(error).__name__ if error is not None else "-"
        if method not in {"Player.Open", "Player.Stop", "Player.SetRepeat"}:
            key = (method, outcome, error_class)
            failure_log_interval = 5.0
            if (
                started - self._failure_logged_at.get(key, float("-inf"))
                < failure_log_interval
            ):
                return
            self._failure_logged_at[key] = started
        self._audit(
            "fireplace_rpc",
            method=method,
            outcome=outcome,
            elapsed=round(time.monotonic() - started, 3),
            error_class=error_class,
        )

    def rpc(self, method: str, params: dict[str, Any], deadline: float) -> Any:
        """Retain bounded transport and classify uncertain acknowledgements."""
        started = time.monotonic()
        remaining = deadline - started
        if remaining <= 0:
            message = "deadline"
            raise _fireplace_module.RpcNotDispatchedError(message)
        call_timeout = (
            8.0 if method in {"Player.Open", "Player.Stop", "Player.SetRepeat"} else 1.0
        )
        try:
            response = self._rpc(method, params, timeout=min(call_timeout, remaining))
        except urllib.error.HTTPError as exc:
            self._audit_rpc(method, "rejected", started, exc)
            exc.close()
            message = "rpc_rejected"
            raise FireplaceError(message) from exc
        except OSError as exc:
            self._audit_rpc(method, "transport_unconfirmed", started, exc)
            message = "rpc_transport_unconfirmed"
            raise _fireplace_module.RpcTransportError(message) from exc
        body = response.get("body")
        if (
            not response.get("ok")
            or not isinstance(body, dict)
            or "error" in body
            or "result" not in body
        ):
            self._audit_rpc(method, "rejected", started)
            message = "rpc_unavailable"
            raise FireplaceError(message)
        if method in {"Player.Open", "Player.Stop", "Player.SetRepeat"}:
            self._audit_rpc(method, "acknowledged", started)
        return body["result"]

    def controller(self) -> Any:
        """Create the existing shared controller once under its lock."""
        with self._lock:
            if self._controller is None:
                self._controller = _fireplace_module.FireplaceController(
                    self.rpc, self.config_dir / "fireplace-owner.json"
                )
                self._controller.start_monitor()
            return self._controller

    def command(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate before creating a controller or starting media."""
        if (
            set(payload) not in ({"action"}, {"action", "request_id"})
            or not isinstance(payload.get("action"), str)
            or payload["action"] not in {"start", "stop"}
            or (
                "request_id" in payload
                and (
                    payload["action"] != "start"
                    or not isinstance(payload["request_id"], str)
                    or re.fullmatch(r"[A-Za-z0-9_.:+-]{1,96}", payload["request_id"])
                    is None
                )
            )
        ):
            message = "invalid_fireplace_action"
            raise self.invalid_input(message)
        controller = self.controller()
        return (
            controller.start(payload.get("request_id"))
            if payload["action"] == "start"
            else controller.stop()
        )

    def status(self) -> dict[str, Any]:
        """Read the receipt without starting playback."""
        return self.controller().status()

    def manifest(self, query: dict[str, list[str]]) -> bytes:
        """Read only the currently authorized manifest generation."""
        return _manifest_module.read_manifest(self.config_dir / "youtube", query)

    def failure_status(self) -> dict[str, Any]:
        """Return existing ownership evidence without creating a controller."""
        return (
            self._controller.status()
            if self._controller is not None
            else {"state": "error"}
        )

    def recover(self, audit: Callable[..., None]) -> None:
        """Contain a corrupt receipt while retaining sanitized startup diagnostics."""
        try:
            self.controller().start_monitor()
        except Exception as exc:  # noqa: BLE001 - other host routes must remain available.
            audit(
                "fireplace_recovery",
                outcome="unavailable",
                error_class=type(exc).__name__,
            )

    def close(self) -> None:
        """Release monitor resources without inventing a user playback stop."""
        if self._controller is not None:
            self._controller.close()


class RouteResponse:
    """Dispatch after outer authorization and return status for outer auditing."""

    def __init__(
        self,
        body_routes: set[tuple[str, str]],
        query_routes: set[tuple[str, str]],
        fireplace: FireplaceEndpoints,
    ) -> None:
        """Retain the registered routes and the matching fireplace endpoint."""
        self.body_routes = body_routes
        self.query_routes = query_routes
        self.fireplace = fireplace

    def send(
        self,
        handler: Any,
        route_key: tuple[str, str],
        route: Callable[..., Any],
        query: str,
    ) -> tuple[int, str | None]:
        """Keep manifest bytes and fireplace errors inside their existing contract."""
        try:
            if route_key in self.body_routes:
                result = route(handler.read_json_body())
            elif route_key in self.query_routes:
                result = route(urllib.parse.parse_qs(query))
            else:
                result = route()
            if route_key == ("GET", "/kodi/youtube/manifest"):
                handler.send_response(200)
                handler.send_header("Content-Type", "application/dash+xml")
                handler.send_header("Content-Length", str(len(result)))
                handler.send_header("Cache-Control", "no-store")
                handler.end_headers()
                handler.wfile.write(result)
            else:
                handler.send_json(200, {"ok": True, **result})
        except ManifestError as exc:
            handler.send_json(404, {"ok": False, "error": str(exc)})
            return 404, type(exc).__name__
        except FireplaceError as exc:
            handler.send_json(
                503, {**self.fireplace.failure_status(), "ok": False, "error": str(exc)}
            )
            return 503, type(exc).__name__
        return 200, None


def _kodi_active_state(run: Callable[..., Any], unit: str) -> str:
    """Read the unit phase before cancelling an unfinished start."""
    proc = run(
        ["systemctl", "show", unit, "--property=ActiveState", "--value"],
        timeout=3,
    )
    state = proc.stdout.strip()
    if proc.returncode != 0 or not state:
        error_message = "Kodi unit state could not be read"
        raise RuntimeError(error_message)
    return state


def stop_kodi(
    run: Callable[..., Any],
    running: Callable[[], bool],
    quit_rpc: Callable[[], dict[str, Any]],
    unit: str,
) -> dict[str, Any]:
    """Request graceful shutdown before the bounded unit-stop fallback."""
    if not running():
        state = _kodi_active_state(run, unit)
        if state == "activating":
            stop = run(["sudo", "-n", "systemctl", "stop", unit], timeout=20)
            return {
                "kodi": (
                    "stopped"
                    if _kodi_active_state(run, unit) in {"inactive", "failed"}
                    else "stop_requested"
                ),
                "method": "systemctl_stop",
                "jsonrpc": None,
                "systemctl_returncode": stop.returncode,
            }
        if state not in {"active", "reloading"}:
            return {"kodi": "not_running"}

    rpc_result = quit_rpc()
    # Kodi needs a few seconds to shut down on this board; killing the unit mid-shutdown
    # also
    # kills its virtual X server and Kodi then dies with an X IO error (unit left
    # "failed").
    deadline = time.monotonic() + 8
    while running() and time.monotonic() < deadline:
        time.sleep(0.5)
    if not running():
        return {"kodi": "stopped", "method": "jsonrpc", "jsonrpc": rpc_result}

    stop = run(["sudo", "-n", "systemctl", "stop", unit], timeout=20)
    return {
        "kodi": "stopped" if not running() else "stop_requested",
        "method": "systemctl_stop",
        "jsonrpc": rpc_result,
        "systemctl_returncode": stop.returncode,
    }
