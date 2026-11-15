"""Authenticated, volatile keyboard sessions for the two configured players.

No device text is put into HA entities, events, services, URLs, or logs.
Providers own the transport lock and the final device revision comparison.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
from typing import Any

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.components import websocket_api
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN

PREFIX = DOMAIN + "/keyboard/"
PAIR_ENTITY = "input_select.universal_remote_pair"
TARGET_ENTITY = "sensor.universal_remote_target"
CAPABILITIES = ("edit", "commit", "close", "selection")
STATUSES = {"ready", "idle", "unavailable", "stale", "sensitive", "unsupported"}
ERRORS = {"stale_revision", "session_changed", "not_focused", "sensitive",
          "unsupported", "unconfirmed", "conflict", "unavailable", "invalid_input",
          "unauthorized", "busy"}


class KeyboardError(Exception):
    """An intentionally text-free error at the public API boundary."""

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code in ERRORS else "unavailable"
        super().__init__(self.code)


@dataclass(frozen=True)
class KeyboardTarget:
    entity_id: str
    provider: Any


def allowed(user, entity_id: str) -> bool:
    """Both observation and control are required, including on every update."""
    return bool(
        user is not None
        and user.is_active
        and entity_id
        and user.permissions.check_entity(entity_id, POLICY_READ)
        and user.permissions.check_entity(entity_id, POLICY_CONTROL)
    )


def validate_text(text: Any, selection: Any = None) -> tuple[str, dict | None]:
    """Validate Unicode and UTF-16 caret boundaries without echoing input."""
    if not isinstance(text, str) or "\x00" in text:
        raise KeyboardError("invalid_input")
    try:
        units = len(text.encode("utf-16-le")) // 2
    except UnicodeError:
        raise KeyboardError("invalid_input") from None
    if units > 16384:
        raise KeyboardError("invalid_input")
    if selection is None:
        return text, None
    if not isinstance(selection, dict) or set(selection) != {"start", "end"}:
        raise KeyboardError("invalid_input")
    start, end = selection["start"], selection["end"]
    if type(start) is not int or type(end) is not int or not 0 <= start <= end <= units:
        raise KeyboardError("invalid_input")
    boundaries, offset = {0}, 0
    for char in text:
        offset += 2 if ord(char) > 0xFFFF else 1
        boundaries.add(offset)
    if start not in boundaries or end not in boundaries:
        raise KeyboardError("invalid_input")
    return text, {"start": start, "end": end}


def _snapshot_identity(raw: Mapping) -> tuple[str | None, str, str]:
    """Normalize public identifiers without accepting malformed provider values."""
    session = raw.get("session_id")
    session = session if isinstance(session, str) and 0 < len(session) <= 128 else None
    revision = raw.get("revision")
    revision = str(revision) if type(revision) is int and revision >= 0 else ""
    status = raw.get("status", "unavailable")
    status = status if isinstance(status, str) and status in STATUSES else "unavailable"
    return session, revision, status


def _add_editable_text(result: dict, raw: Mapping, capabilities: Mapping) -> None:
    try:
        text, _ = validate_text(raw.get("text"))
    except KeyboardError:
        result["status"] = "unsupported"
        return
    result["text"] = text
    result["capabilities"] = {key: capabilities.get(key) is True for key in CAPABILITIES}
    if result["capabilities"]["selection"]:
        try:
            _, result["selection"] = validate_text(text, raw.get("selection"))
        except KeyboardError:
            result["capabilities"]["selection"] = False


def public_snapshot(pair: str, raw: Any) -> dict:
    """Fail closed if a provider's field classification is missing or malformed."""
    raw = raw if isinstance(raw, Mapping) else {}
    session, revision, status = _snapshot_identity(raw)
    focused = raw.get("focused") is True
    sensitive = raw.get("sensitive") is not False
    capabilities = raw.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, Mapping) else {}
    result = {
        "pair": pair, "session_id": session, "revision": revision,
        "focused": focused, "sensitive": sensitive, "selection": None,
        "capabilities": dict.fromkeys(CAPABILITIES, False),
        "status": status, "fresh_at": raw.get("fresh_at") if isinstance(raw.get("fresh_at"), str) else None,
    }
    safe = (focused and not sensitive and status == "ready" and session
            and revision and capabilities.get("edit") is True)
    if safe:
        _add_editable_text(result, raw, capabilities)
    elif sensitive:
        result["status"] = "sensitive" if raw.get("sensitive") is True else "unsupported"
    elif not focused and status == "ready":
        result["status"] = "idle"
    return result


def _private_mapping_frame(value: Mapping) -> bool:
    if str(value.get("type", "")).startswith(PREFIX):
        return True
    if {"pair", "session_id", "capabilities", "revision"} <= value.keys():
        return True
    return any(_private_frame(item) for item in value.values()
               if isinstance(item, (Mapping, list, tuple, bytes)))


def _private_encoded_frame(value: bytes | str) -> bool:
    # HA logs outgoing JSON bytes, including coalesced arrays and overflow.
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    return (PREFIX in text or all(key in text for key in
            ('"pair"', '"session_id"', '"capabilities"', '"revision"')))


def _private_frame(value: Any) -> bool:
    """Recognize our messages before HA's generic websocket debug formatting."""
    if isinstance(value, Mapping):
        return _private_mapping_frame(value)
    if isinstance(value, (list, tuple)):
        return any(_private_frame(item) for item in value)
    if isinstance(value, (bytes, str)):
        return _private_encoded_frame(value)
    return False


class KeyboardLogFilter(logging.Filter):
    """Suppress only private keyboard frames, including HA debug/overflow logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (_private_frame(record.args) or _private_frame(record.msg))


def _install_log_filter(hass) -> None:
    data = hass.data.setdefault(DOMAIN, {})
    if "keyboard_log_filter" in data:
        return
    log_filter = KeyboardLogFilter()
    for name in ("homeassistant.components.websocket_api.http",
                 "homeassistant.components.websocket_api.http.connection",
                 "homeassistant.components.websocket_api.connection"):
        logging.getLogger(name).addFilter(log_filter)
    # Keep the filter after unload: queued outgoing frames may still be logged.
    data["keyboard_log_filter"] = log_filter


class KeyboardCoordinator:
    """Route authorized subscribers and operations to fixed native targets."""

    def __init__(self, hass, targets: dict[str, KeyboardTarget]) -> None:
        self.hass, self.targets = hass, targets
        self._snapshots: dict[str, dict] = {}
        self._epochs = dict.fromkeys(targets, 0)
        self._subscribers: dict[tuple[int, int], tuple[Any, int, str]] = {}
        self._closed = False
        self._tasks: set[asyncio.Task] = set()
        self.notifications = None
        self._remove_providers = [
            target.provider.subscribe(lambda snapshot, pair=pair: self._observe(pair, snapshot))
            for pair, target in targets.items()
        ]
        self._remove_pair = async_track_state_change_event(hass, [PAIR_ENTITY, TARGET_ENTITY], self._pair_changed)

    def _spawn(self, coroutine) -> None:
        if self._closed:
            coroutine.close()
            return
        task = self.hass.async_create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _pair(self, requested: str) -> str:
        if requested != "auto":
            return requested
        if controller := self.hass.data.get(DOMAIN, {}).get("remote_controller"):
            return controller.resolve({})["pair"]
        state = self.hass.states.get(PAIR_ENTITY)
        return "projector" if state is not None and state.state == "projector" else "tv"

    def _check(self, user, pair: str) -> KeyboardTarget:
        if self._closed or pair not in self.targets:
            raise KeyboardError("unavailable")
        controller = self.hass.data.get(DOMAIN, {}).get("remote_controller")
        if pair == "projector" and controller is not None and controller.runtime.projector_shutting_down:
            raise KeyboardError("unavailable")
        target = self.targets[pair]
        if not allowed(user, target.entity_id):
            raise KeyboardError("unauthorized")
        return target

    @callback
    def _observe(self, pair: str, snapshot: Any) -> None:
        if self._closed:
            return
        self._epochs[pair] += 1
        safe = public_snapshot(pair, snapshot)
        self._snapshots[pair] = safe
        for connection, message_id, requested in tuple(self._subscribers.values()):
            if self._pair(requested) == pair:
                self._publish(connection, message_id, pair, safe)
        if self.notifications is not None:
            # Notifications need only classification/session metadata, never text.
            metadata = {key: value for key, value in safe.items() if key not in ("text", "selection")}
            self._spawn(self.notifications.async_refresh(pair, metadata))

    def _publish(self, connection, message_id: int, pair: str, snapshot: dict) -> None:
        if self._closed:
            return
        if allowed(connection.user, self.targets[pair].entity_id):
            connection.send_event(message_id, snapshot)
        else:
            connection.send_event(message_id, public_snapshot(pair, {"status": "unavailable"}))

    async def _snapshot(self, pair: str, user) -> dict:
        target = self._check(user, pair)
        epoch = self._epochs[pair]
        raw = await target.provider.async_snapshot()
        self._check(user, pair)
        if epoch == self._epochs[pair]:
            self._observe(pair, raw)
        return self._snapshots[pair]

    async def async_subscribe(self, connection, message_id: int, requested: str) -> None:
        # Pair selection can change while a native snapshot is awaited, before
        # this subscription exists to receive the helper's state-change event.
        for _attempt in range(3):
            pair = self._pair(requested)
            self._check(connection.user, pair)
            snapshot = await self._snapshot(pair, connection.user)
            if self._pair(requested) == pair:
                break
        else:
            raise KeyboardError("busy")
        key = (id(connection), message_id)
        self._subscribers[key] = (connection, message_id, requested)

        @callback
        def unsubscribe() -> None:
            self._subscribers.pop(key, None)

        connection.subscriptions[message_id] = unsubscribe
        connection.send_result(message_id)
        self._publish(connection, message_id, pair, snapshot)

    @callback
    def _pair_changed(self, event) -> None:
        previous, current = event.data.get("old_state"), event.data.get("new_state")
        if (previous is not None and current is not None and previous.state == current.state
                and all(previous.attributes.get(key) == current.attributes.get(key)
                        for key in ("pair", "projector_shutting_down"))):
            return
        for connection, message_id, requested in tuple(self._subscribers.values()):
            if requested == "auto":
                self._spawn(self._refresh_subscription(connection, message_id, requested))

    async def _refresh_subscription(self, connection, message_id: int, requested: str) -> None:
        pair = self._pair(requested)
        key = (id(connection), message_id)
        try:
            snapshot = await self._snapshot(pair, connection.user)
        except Exception:
            snapshot = public_snapshot(pair, {"status": "unavailable"})
        if key in self._subscribers and self._pair(requested) == pair:
            self._publish(connection, message_id, pair, snapshot)

    def _operation_session(self, request: dict) -> tuple[str, str, str]:
        session_id, revision = request.get("session_id"), request.get("expected_revision")
        if not isinstance(session_id, str) or not 0 < len(session_id) <= 128 or not isinstance(revision, str):
            raise KeyboardError("invalid_input")
        pair = next((pair for pair, snapshot in self._snapshots.items()
                     if snapshot["session_id"] == session_id), None)
        if pair is None:
            raise KeyboardError("session_changed")
        return pair, session_id, revision

    async def async_operate(self, user, action: str, request: dict) -> dict:
        pair, session_id, revision = self._operation_session(request)
        target = self._check(user, pair)
        snapshot = await self._snapshot(pair, user)
        self._validate_operation(snapshot, session_id, revision, action)
        if action == "edit":
            text, selection = validate_text(request.get("text"), request.get("selection"))
            if selection is not None and not snapshot["capabilities"]["selection"]:
                raise KeyboardError("unsupported")
        self._check(user, pair)
        # The provider does an atomic compare under the same lock used by T9.
        operation_epoch = self._epochs[pair]
        try:
            if action == "edit":
                raw = await target.provider.async_edit(session_id, int(revision), text, selection)
            else:
                raw = await getattr(target.provider, "async_" + action)(session_id, int(revision))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Refresh after a rejected mutation, but never retry the write.
            try:
                await self._snapshot(pair, user)
            except Exception:
                pass
            raise KeyboardError(getattr(exc, "code", "unavailable")) from None
        self._check(user, pair)
        if operation_epoch == self._epochs[pair]:
            self._observe(pair, raw)
        return self._snapshots[pair]

    @staticmethod
    def _validate_operation(snapshot: dict, session_id: str, revision: str, action: str) -> None:
        if snapshot["session_id"] != session_id:
            raise KeyboardError("session_changed")
        if snapshot["revision"] != revision:
            raise KeyboardError("stale_revision")
        if snapshot["sensitive"] is not False:
            raise KeyboardError("sensitive")
        if not snapshot["focused"]:
            raise KeyboardError("not_focused")
        if snapshot["status"] != "ready" or not snapshot["capabilities"].get(action):
            raise KeyboardError("unsupported")

    async def async_start(self, notifications=None) -> None:
        self.notifications = notifications
        for pair, target in self.targets.items():
            try:
                self._observe(pair, await target.provider.async_snapshot())
            except Exception:
                self._observe(pair, {"status": "unavailable"})

    async def async_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._remove_pair()
        for remove in self._remove_providers:
            remove()
        self._subscribers.clear()
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._tasks.clear()
        self._snapshots.clear()
        if self.notifications is not None:
            await self.notifications.async_close()


def _coordinator(hass) -> KeyboardCoordinator:
    coordinator = hass.data.get(DOMAIN, {}).get("keyboard_coordinator")
    if coordinator is None:
        raise KeyboardError("unavailable")
    return coordinator


async def _run(hass, connection, message: dict, action: str) -> None:
    try:
        accepted = {"id", "type", "pair"} if action == "subscribe" else {
            "id", "type", "session_id", "expected_revision"} | ({"text", "selection"} if action == "edit" else set())
        if set(message) - accepted:
            raise KeyboardError("invalid_input")
        coordinator = _coordinator(hass)
        if action == "subscribe":
            pair = message.get("pair", "auto")
            if pair not in ("auto", "tv", "projector"):
                raise KeyboardError("invalid_input")
            await coordinator.async_subscribe(connection, message["id"], pair)
        else:
            result = await coordinator.async_operate(connection.user, action, message)
            connection.send_result(message["id"], result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", "unavailable")
        code = code if isinstance(code, str) and code in ERRORS else "unavailable"
        connection.send_error(message["id"], code, code)


@websocket_api.websocket_command({
    vol.Required("type"): PREFIX + "subscribe",
    vol.Optional("pair", default="auto"): object,
})
@websocket_api.async_response
async def websocket_subscribe(hass, connection, message):
    await _run(hass, connection, message, "subscribe")


@websocket_api.websocket_command({
    vol.Required("type"): PREFIX + "edit",
    vol.Required("session_id"): object,
    vol.Required("expected_revision"): object,
    vol.Required("text"): object,
    vol.Optional("selection"): object,
})
@websocket_api.async_response
async def websocket_edit(hass, connection, message):
    await _run(hass, connection, message, "edit")


@websocket_api.websocket_command({
    vol.Required("type"): PREFIX + "commit",
    vol.Required("session_id"): object,
    vol.Required("expected_revision"): object,
})
@websocket_api.async_response
async def websocket_commit(hass, connection, message):
    await _run(hass, connection, message, "commit")


@websocket_api.websocket_command({
    vol.Required("type"): PREFIX + "close",
    vol.Required("session_id"): object,
    vol.Required("expected_revision"): object,
})
@websocket_api.async_response
async def websocket_close(hass, connection, message):
    await _run(hass, connection, message, "close")


@callback
def async_register_commands(hass) -> None:
    _install_log_filter(hass)
    for handler in (websocket_subscribe, websocket_edit, websocket_commit, websocket_close):
        websocket_api.async_register_command(hass, handler)
