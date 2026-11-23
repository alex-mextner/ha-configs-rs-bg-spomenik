#!/usr/bin/env python3
# Copyright (c) 2026 Alex Mextner
"""Local credentials, sanitized playback evidence and bounded HTTP commands."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

HA = "http://127.0.0.1:8123"
MA = "http://127.0.0.1:8095"
HOME = Path.home()
CONFIG = HOME / ".config/ha-smart-home-recovery/config.json"
LOCAL_STATE = Path(
    os.environ.get(
        "HA_SMART_HOME_EVENT_STATE", str(HOME / ".local/state/ha-smart-home-events")
    )
)
MAINTENANCE = HOME / ".ma-maintenance.lock"
QUASAR = "http://192.168.0.56:31116"
ENTRIES = HOME / "homeassistant/.storage/core.config_entries"
MAX_RESPONSE = 4 * 1024 * 1024
MAX_HARDWARE_RESPONSE = 65536
IDENTITY = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")


def identity(value: object) -> str | None:
    """Admit bounded opaque identifiers, never paths, URLs or raw messages."""
    return value if isinstance(value, str) and IDENTITY.fullmatch(value) else None


def maintenance_status(path: Path = MAINTENANCE) -> str:
    """Recognize the existing exclusive-create owner without deleting its lock."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    owner = re.fullmatch(
        r"owner=chatgpt-ma-reconnect pid=([1-9][0-9]*) id=[a-f0-9-]+\n?", raw
    )
    if not owner:
        return "unknown"
    process = Path("/proc") / owner[1] / "cmdline"
    try:
        command = process.read_bytes()
    except FileNotFoundError:
        return "stale"
    except OSError:
        return "unknown"
    return "active" if b"deploy_reviewed.py" in command else "stale"


def hardware_state(physical_id: str) -> dict | None:
    """Read the exact connected studio's hardware state, never a sibling protocol."""
    if physical_id != "M00ZRW3008RAJG":
        return None
    request = Request(QUASAR + "/devices/" + physical_id + "/state")  # noqa: S310 - fixed local bridge and exact verified device.
    with build_opener(NoRedirect()).open(request, timeout=2) as response:
        raw = response.read(MAX_HARDWARE_RESPONSE + 1)
    if len(raw) > MAX_HARDWARE_RESPONSE:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("deviceId") != physical_id:
        return None
    state = data.get("deviceState")
    if not isinstance(state, dict) or type(state.get("SoundMuted")) is not bool:
        return None
    level = state.get("SoundLevel")
    return {
        "physical_id": physical_id,
        "muted": state["SoundMuted"],
        "level": level if type(level) is int else None,
    }


def hardware_mute(physical_id: str) -> bool | None:
    """Return only the exact device's fresh boolean mute observation."""
    state = hardware_state(physical_id)
    return state["muted"] if state else None


def atomic_json(path: Path, value: dict) -> None:
    """Durably replace one private state file before external effects."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".pending")
    fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class NoRedirect(HTTPRedirectHandler):
    """Prevent credentials from following any redirect."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        """Reject redirects before any authentication header can be forwarded."""
        msg = "redirect_refused"
        raise RuntimeError(msg)


def request_json(
    url: str,
    token: str | None = None,
    payload: dict | None = None,
    *,
    deadline_seconds: int = 4,
) -> object:
    """Call only the two fixed local services with bounded responses."""
    if not url.startswith((HA + "/", MA + "/")):
        msg = "unsupported_origin"
        raise ValueError(msg)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(  # noqa: S310 - only fixed local HTTP origins accepted above.
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
    )
    with build_opener(NoRedirect()).open(request, timeout=deadline_seconds) as response:
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        msg = "oversized_response"
        raise ValueError(msg)
    return json.loads(data) if data else None


def ha_token() -> str:
    """Read the existing credential locally without shell evaluation."""
    for line in (HOME / ".env").read_text().splitlines():
        key, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and key.strip() == "HA_TOKEN":
            return value.strip().strip("'\"")
    msg = "ha_credential_missing"
    raise RuntimeError(msg)


def ma_token() -> str:
    """Read the existing integration token; never change HA internal storage."""
    entries = json.loads(ENTRIES.read_text())["data"]["entries"]
    entry = next((e for e in entries if e.get("domain") == "music_assistant"), {})
    token = entry.get("data", {}).get("token")
    if not isinstance(token, str) or not token:
        msg = "ma_credential_missing"
        raise RuntimeError(msg)
    return token


def ma_command(
    command: str,
    args: dict | None = None,
    *,
    deadline_seconds: int = 4,
    message_id: str | None = None,
) -> object:
    """Use the supported authenticated Music Assistant JSON API."""
    result = request_json(
        MA + "/api",
        ma_token(),
        {
            "command": command,
            "args": args or {},
            **({"message_id": message_id} if message_id else {}),
        },
        deadline_seconds=deadline_seconds,
    )
    if isinstance(result, dict) and result.get("error"):
        msg = "ma_command_failed"
        raise RuntimeError(msg)
    return result


def item_identity(media: object) -> str | None:
    """Return a queue item ID or opaque URI digest without exporting its URI."""
    if not isinstance(media, dict):
        return None
    item = identity(media.get("queue_item_id"))
    if item:
        return item
    uri = media.get("uri")
    return (
        hashlib.sha256(uri.encode()).hexdigest()
        if isinstance(uri, str) and uri
        else None
    )


def normalize_queue(raw: dict) -> dict:
    """Keep only the queue identity needed to cancel an obsolete deadline."""
    return {
        "id": identity(raw.get("queue_id")),
        "state": raw.get("state"),
        "item_id": item_identity(raw.get("current_item")),
    }


def normalize_player(raw: dict, queues: dict, now: float) -> dict:
    """Sanitize a player without confusing a physical member with its queue."""
    media = raw.get("current_media") or {}
    queue_id = identity(media.get("source_id")) or identity(raw.get("active_source"))
    queue_id = queue_id if queue_id in queues else identity(raw.get("player_id"))
    queue = queues.get(queue_id, {})
    uri = media.get("uri", "")
    source = uri.partition("://")[0] if isinstance(uri, str) and "://" in uri else None
    return {
        "id": identity(raw.get("player_id")),
        "provider": identity(raw.get("provider")),
        "source_provider": identity(source),
        "queue_id": queue_id,
        "item_id": item_identity(media) or queue.get("item_id"),
        "state": raw.get("playback_state", raw.get("state")),
        "available": raw.get("available"),
        "enabled": raw.get("enabled"),
        "elapsed": raw.get("elapsed_time"),
        "duration": media.get("duration"),
        "updated_at": now,
        "members": [v for x in raw.get("group_members", []) if (v := identity(x))],
    }


def failure_reason(error: object) -> str:
    """Classify known error evidence; ambiguous failures stay unknown."""
    text = str(error).lower()
    if any(
        word in text
        for word in (
            "authentication",
            "unauthorized",
            "forbidden",
            "invalid token",
            "401",
            "403",
        )
    ):
        return "auth"
    if any(
        word in text for word in ("configuration", "invalid config", "not configured")
    ):
        return "configuration"
    if any(
        word in text
        for word in (
            "typeerror",
            "attributeerror",
            "keyerror",
            "already registered",
            "traceback",
            "wsmsgtype",
        )
    ):
        return "code"
    if any(
        word in text
        for word in (
            "timeout",
            "timed out",
            "connection",
            "network",
            "dns",
            "unreachable",
            "write error",
            "transport",
            "closed",
        )
    ):
        return "network"
    return "unknown"
