"""Private Kodi keyboard provider using the existing native WS connection.

Input.OnInputRequested.type is the only automatic field classification. Titles
and initial values never enter HA events, service calls, states or logs here.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import inspect
import json
import logging
from typing import Any
from uuid import uuid4

import aiohttp
from homeassistant.core import callback
from homeassistant.helpers import entity_registry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

_LOGGER = logging.getLogger(__name__)
_SAFE_ERRORS = {"stale_revision", "session_changed", "not_focused", "sensitive",
                "unsupported", "unconfirmed", "conflict", "unavailable", "invalid_input", "busy"}

class KodiKeyboardError(Exception):
    def __init__(self, code: str):
        self.code = code if code in _SAFE_ERRORS else "unavailable"
        super().__init__(self.code)

def _text(value: Any) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise KodiKeyboardError("invalid_input")
    try:
        if len(value.encode("utf-16-le")) // 2 > 16384:
            raise KodiKeyboardError("invalid_input")
    except UnicodeError as exc:
        raise KodiKeyboardError("invalid_input") from exc
    return value

def _blank(status: str = "idle") -> dict:
    return {"session_id": uuid4().hex, "revision": 0, "focused": status == "sensitive",
            "sensitive": status == "sensitive", "selection": None,
            "capabilities": {"edit": False, "commit": False, "close": False, "selection": False},
            "status": status, "fresh_at": None}

class KodiKeyboardProvider:
    def __init__(self, hass, native_entity_id: str, host_origin: str, host_token: str,
                 *, command_allowed: Callable[[], bool] | None = None):
        self.hass, self.native_entity_id = hass, native_entity_id
        self._url, self._token = host_origin.rstrip("/") + "/kodi/keyboard", host_token
        self._state = _blank("unavailable")
        self._kind, self._epoch = "unknown", 0
        self._lock = asyncio.Lock()
        self._listeners: set[Callable] = set()
        self._connection = self._server = None
        self._hooks: dict[str, tuple[Any, Any]] = {}
        self._binding_generation = 0
        self._remove_timer = None
        self._tasks: set[asyncio.Task] = set()
        self._poll_task = None
        self._connected = self._stopped = False
        self.command_allowed = command_allowed or (lambda: True)

    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.add(callback)
        return lambda: self._listeners.discard(callback)

    async def async_snapshot(self) -> dict:
        return {**self._state, "capabilities": dict(self._state["capabilities"])}

    def _publish(self) -> None:
        for listener in tuple(self._listeners):
            try:
                listener({**self._state, "capabilities": dict(self._state["capabilities"])})
            except Exception as exc:
                _LOGGER.warning("Kodi keyboard subscriber failed (%s)", type(exc).__name__)

    def _spawn(self, coroutine):
        task = self.hass.async_create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _resolve_connection(self):
        registration = entity_registry.async_get(self.hass).async_get(self.native_entity_id)
        if registration is None or registration.config_entry_id is None:
            return None
        entry = self.hass.config_entries.async_get_entry(registration.config_entry_id)
        runtime = getattr(entry, "runtime_data", None) if entry else None
        return getattr(runtime, "connection", None)

    def _remove_hooks(self) -> None:
        self._binding_generation += 1
        if self._server is not None:
            handlers = getattr(self._server, "_server_request_handlers", {})
            for name, (own, previous) in self._hooks.items():
                # jsonrpc_base exposes registration through attribute assignment
                # but no public unregister API. Restore only our own bindings.
                if handlers.get(name) is own:
                    if previous is None:
                        handlers.pop(name, None)
                    else:
                        setattr(self._server, name, previous)
        self._hooks.clear()
        self._server = None

    def _attach(self, connection) -> None:
        self._remove_hooks()
        self._connection, self._server = connection, connection.server
        handlers = getattr(self._server, "_server_request_handlers", {})
        generation, server = self._binding_generation, self._server
        for name, callback in (("Input.OnInputRequested", self._requested),
                               ("Input.OnInputFinished", self._finished)):
            previous = handlers.get(name)
            def chained(*args, own=callback, previous=previous, **kwargs):
                if (self._stopped or self._binding_generation != generation
                        or self._connection is not connection or self._server is not server
                        or not self._connected or not connection.connected):
                    return
                own(*args, **kwargs)
                if previous is not None:
                    try:
                        result = previous(*args, **kwargs)
                        if inspect.isawaitable(result):
                            self._spawn(result)
                    except Exception as exc:
                        _LOGGER.warning("Existing Kodi input callback failed (%s)", type(exc).__name__)
            self._hooks[name] = (chained, previous)
            setattr(self._server, name, chained)

    def _ensure_connection(self) -> bool:
        connection = self._resolve_connection()
        usable = connection is not None and connection.can_subscribe
        rebound = usable and (self._connection is not connection or self._server is not connection.server)
        if rebound:
            self._attach(connection)
        connected = bool(usable and connection.connected)
        if rebound or connected != self._connected:
            self._connected = connected
            self._kind, self._epoch = "unknown", self._epoch + 1
            self._state = _blank("idle" if connected else "unavailable")
            self._publish()
        return connected

    async def async_start(self) -> None:
        self._ensure_connection()
        self._remove_timer = async_track_time_interval(self.hass, self._tick, timedelta(seconds=0.4))

    def _requested(self, sender=None, data=None, **kwargs) -> None:
        kind = data.get("type") if isinstance(data, dict) else None
        self._begin_context(kind if kind in {"keyboard", "password"} else "unknown")

    def _finished(self, sender=None, data=None, **kwargs) -> None:
        self._begin_context("closed")

    def _begin_context(self, kind: str) -> None:
        if self._stopped:
            return
        self._kind, self._epoch = kind, self._epoch + 1
        epoch = self._epoch
        self._state = _blank("sensitive" if kind == "password" else "idle")
        self._publish()
        self._spawn(self._context(kind, epoch))

    async def _context(self, kind: str, epoch: int) -> None:
        async with self._lock:
            if epoch != self._epoch or self._stopped:
                return
            try:
                response = await self._post({"action": "context", "kind": kind})
                if epoch == self._epoch:
                    self._apply(response["snapshot"])
            except (KodiKeyboardError, KeyError):
                if epoch == self._epoch:
                    self._state = _blank("unavailable")
                    self._publish()

    def _apply(self, snapshot: dict) -> None:
        # The native Input event is the independent privacy gate. Even a bad
        # host response cannot expose a password/control field to subscribers.
        if self._kind != "keyboard":
            self._state = _blank("sensitive" if self._kind == "password" else "idle")
        elif not isinstance(snapshot, dict) or snapshot.get("status") != "ready":
            status = snapshot.get("status", "unavailable") if isinstance(snapshot, dict) else "unavailable"
            self._state = _blank(status if status in {"idle", "unavailable", "stale", "unsupported"} else "unavailable")
        else:
            self._state = self._ready_snapshot(snapshot)
        self._publish()

    @staticmethod
    def _ready_snapshot(snapshot: dict) -> dict:
        if snapshot.get("sensitive") is not False or snapshot.get("focused") is not True:
            raise KodiKeyboardError("unavailable")
        value = _text(snapshot.get("text"))
        session_id, revision = snapshot.get("session_id"), snapshot.get("revision")
        if not isinstance(session_id, str) or type(revision) is not int:
            raise KodiKeyboardError("unavailable")
        return {"session_id": session_id, "revision": revision, "focused": True,
                "sensitive": False, "text": value, "selection": None,
                "capabilities": {"edit": True, "commit": True, "close": True, "selection": False},
                "status": "ready", "fresh_at": snapshot.get("fresh_at")}

    @callback
    def _tick(self, now=None) -> None:
        if self._stopped or not self._ensure_connection() or self._kind != "keyboard":
            return
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = self._spawn(self._refresh(self._epoch))

    async def _refresh(self, epoch: int) -> None:
        async with self._lock:
            if epoch != self._epoch or self._stopped:
                return
            try:
                response = await self._post({"action": "snapshot"})
                if epoch == self._epoch:
                    self._apply(response["snapshot"])
                    if self._state["status"] != "ready":
                        self._kind = "unknown"
            except (KodiKeyboardError, KeyError):
                if epoch == self._epoch:
                    self._kind = "unknown"
                    self._state = _blank("unavailable")
                    self._publish()

    async def _post(self, payload: dict) -> dict:
        if payload.get("action") in {"edit", "commit", "close"} and not self.command_allowed():
            raise KodiKeyboardError("unavailable")
        try:
            session = async_get_clientsession(self.hass)
            async with session.post(self._url, json=payload,
                    headers={"X-HA-Host-Token": self._token},
                    timeout=aiohttp.ClientTimeout(total=5)) as response:
                raw = await response.content.read(131073)
                if len(raw) > 131072:
                    raise KodiKeyboardError("unavailable")
                reply = json.loads(raw)
                if not isinstance(reply, dict):
                    raise KodiKeyboardError("unavailable")
                if response.status != 200 or reply.get("ok") is not True:
                    raise KodiKeyboardError(reply.get("error", "unavailable"))
                return reply
        except KodiKeyboardError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise KodiKeyboardError("unavailable") from exc

    async def _operate(self, action: str, session_id: str, expected_revision: int, text=None) -> dict:
        async with self._lock:
            if not self.command_allowed():
                raise KodiKeyboardError("unavailable")
            if self._stopped or self._kind != "keyboard" or self._state["status"] != "ready":
                raise KodiKeyboardError("not_focused")
            payload = {"action": action, "session_id": session_id, "expected_revision": expected_revision}
            if action == "edit":
                payload["text"] = _text(text)
            epoch = self._epoch
            try:
                response = await self._post(payload)
                if response.get("verified") is not True:
                    raise KodiKeyboardError("unconfirmed")
                if epoch != self._epoch:
                    # Kodi announces InputFinished before replying to the host.
                    # A confirmed terminal ACK belongs to the captured operation;
                    # it must never replace a newly opened editor in this provider.
                    terminal = response.get("snapshot")
                    if action in {"commit", "close"} and isinstance(terminal, dict) and terminal.get("status") == "idle":
                        return _blank("idle")
                    raise KodiKeyboardError("session_changed")
                self._apply(response["snapshot"])
                return await self.async_snapshot()
            except KodiKeyboardError:
                # Observe current field after a conflict, without replaying input.
                if epoch == self._epoch:
                    self._spawn(self._refresh(epoch))
                raise

    async def async_edit(self, session_id: str, expected_revision: int, text: str, selection=None) -> dict:
        if selection is not None:
            raise KodiKeyboardError("unsupported")
        return await self._operate("edit", session_id, expected_revision, text)

    async def async_commit(self, session_id: str, expected_revision: int) -> dict:
        return await self._operate("commit", session_id, expected_revision)

    async def async_close(self, session_id: str, expected_revision: int) -> dict:
        return await self._operate("close", session_id, expected_revision)

    async def async_shutdown(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._epoch += 1
        if self._remove_timer is not None:
            self._remove_timer()
            self._remove_timer = None
        self._remove_hooks()
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._tasks.clear()
        self._kind = "closed"
        self._state = _blank("unavailable")
        self._publish()
        self._listeners.clear()
        try:
            await self._post({"action": "context", "kind": "closed"})
        except KodiKeyboardError:
            pass
