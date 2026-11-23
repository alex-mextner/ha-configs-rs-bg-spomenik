#!/usr/bin/env python3
# Copyright (c) 2026 Alex Mextner
"""Stream local Home Assistant and Music Assistant events; no health polling."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ha_smart_home_common import (
    CONFIG,
    LOCAL_STATE,
    MAINTENANCE,
    active_claim,
    atomic_json,
    failure_reason,
    ha_token,
    identity,
    ma_token,
    maintenance_status,
    normalize_player,
    normalize_queue,
    refresh_queue_sources,
)
from ha_smart_home_recovery import LOG, init_log
from ha_smart_home_watch import watch_marker

CONTAINER = "homeassistant-music-assistant-1"
INTENT_MARKER = "HA_RECOVERY_INTENT "
TRUSTED_INTENT = re.compile(
    r"^[^\n\[\]]{0,200}\[music_assistant\.ha_recovery_intent\]"
    r"\s+HA_RECOVERY_INTENT (.+)$"
)
TRUSTED_HASS_ORIGIN = re.compile(
    r"^[^\n\[\]]{0,200}\[music_assistant\.ha_recovery_intent\]"
    r"\s+HA_RECOVERY_HASS_ORIGIN (.+)$"
)
HA_ORIGIN_TIMEOUT = 30
MAX_PENDING_ORIGINS = 256
MAX_COMMAND_LENGTH = 100
INTENT_RETENTION_SECONDS = 600
MAX_LOG_DELAY = 5


@dataclass(frozen=True)
class Moment:
    """Capture source wall time for diagnostics and uptime for causal timing."""

    wall: float
    monotonic: float


def require_lists(*values: object) -> None:
    """Reject malformed reconciliation inventories instead of calling them healthy."""
    if not all(isinstance(value, list) for value in values):
        message = "invalid_reconciliation"
        raise TypeError(message)


HA_CONTROLS = {
    "media_play",
    "media_pause",
    "media_stop",
    "media_play_pause",
    "media_next_track",
    "media_previous_track",
    "play_media",
    "media_seek",
    "turn_off",
    "volume_mute",
    "volume_set",
    "select_source",
}


def log_events(line: str, players: dict) -> list:
    """Normalize scoped live errors; URLs and untrusted error text stay local."""
    if match := TRUSTED_INTENT.fullmatch(line):
        return intent_record(match[1])
    if match := TRUSTED_HASS_ORIGIN.fullmatch(line):
        return hass_origin_record(match[1])
    if not any(
        marker in line
        for marker in (
            "AudioError while streaming queue item",
            "AudioError while preparing queue item",
            "AudioError while streaming AudioSource",
            "was lost mid-stream",
            "Removing player",
        )
    ):
        return []
    events = []
    seen = set()
    for pid, player in players.items():
        uri = player.get("uri")
        uri_match = isinstance(uri, str) and bool(uri) and "(" + uri + ")" in line
        player_match = (
            "Removing player " + pid + " " in line
            or "transport for " + pid + " " in line
        )
        if not uri_match and not player_match:
            continue
        target = player.get("id", pid)
        if target in seen:
            continue
        seen.add(target)
        events.append(
            {
                "kind": "fault",
                "scope": "player:" + target,
                "provider": player.get("source_provider") or player.get("provider"),
                "reason": failure_reason(line),
                "player_ids": [target],
                "playback_fault": True,
            }
        )
    return events


def hass_origin_record(raw: str) -> list:
    """Parse only bounded private native context provenance."""
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    if (
        isinstance(value, dict)
        and value.get("domain") == "media_player"
        and identity(value.get("context_id"))
        and isinstance(value.get("service"), str)
        and isinstance(value.get("entity_ids"), list)
        and all(identity(x) for x in value["entity_ids"])
    ):
        return [{"kind": "ha_origin", **value}]
    return []


def intent_record(raw: str) -> list:
    """Accept bounded private intent records without forwarding arbitrary keys."""
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, dict) or data.get("phase") not in {"pending", "accepted"}:
        return []
    command = data.get("command")
    if not isinstance(command, str) or len(command) > MAX_COMMAND_LENGTH:
        return []
    return [
        {
            "kind": "intent",
            "command": command,
            "intent_at": data.get("observed_at"),
            "origin_tag": identity(data.get("origin_tag")),
            "player_ids": [v for x in data.get("player_ids", []) if (v := identity(x))],
            "queue_ids": [v for x in data.get("queue_ids", []) if (v := identity(x))],
        }
    ]


def provider_events(configs: list) -> list:
    """Normalize current provider configuration failures."""
    result = []
    for config in configs:
        if not isinstance(config, dict):
            continue
        iid = identity(config.get("instance_id"))
        if not iid:
            continue
        enabled = config.get("enabled")
        valid = type(enabled) is bool and (enabled is False or "last_error" in config)
        error = config.get("last_error") if enabled is True else None
        result.append(
            {
                "kind": "fault",
                "scope": "provider:" + iid,
                "provider": iid,
                "domain": identity(config.get("domain")),
                "reason": failure_reason(error) if valid else "unknown",
                "recovered": not bool(error) if valid else False,
            }
        )
    return result


def public_players(players: dict) -> list:
    """Remove source URLs before evidence leaves the credential host."""
    return [
        {k: v for k, v in player.items() if k != "uri"} for player in players.values()
    ]


def entity_targets(value: object) -> list:
    """Normalize bounded HA entity identifiers without trusting other arguments."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [target for item in value if (target := identity(item))]


def integration_events(entries: list, config: dict) -> list:
    """Map enabled critical entries without interpreting unknown state as health."""
    domains = config.get(
        "integration_domains",
        [
            "music_assistant",
            "yandex_station",
            "yandex_smart_home",
            "ekf_appliances",
            "aisis_speaker",
        ],
    )
    result = []
    for entry in entries:
        if entry.get("disabled_by") or entry.get("domain") not in domains:
            continue
        iid = identity(entry.get("entry_id"))
        state = entry.get("state")
        if not iid or state not in {"loaded", "setup_error", "setup_retry"}:
            continue
        result.append(
            {
                "kind": "fault",
                "scope": "ha.integration:" + iid,
                "provider": "ha:" + str(entry["domain"]),
                "reason": "network" if state == "setup_retry" else "configuration",
                "recovered": state == "loaded",
            }
        )
    return result


class Socket:
    """Multiplex request receipts and live events on one authenticated socket."""

    def __init__(self, ws: object, channel: str) -> None:
        """Create one request multiplexer for the authenticated connection."""
        self.ws, self.channel = ws, channel
        self.sequence = 10
        self.wire_sequence = 0
        self.commands: dict[str, str] = {}
        self.reply_sequences: dict[str, int] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.parts: dict[str, list] = {}
        self.events: asyncio.Queue = asyncio.Queue(maxsize=2048)

    async def call(self, command: str, args: dict | None = None) -> object:
        """Wait for the exact command receipt while the event reader continues."""
        self.sequence += 1
        key = "message_id" if self.channel == "ma" else "id"
        message = {key: self.sequence}
        if self.channel == "ma":
            message.update(command=command, args=args or {})
        else:
            message.update(type=command, **(args or {}))
        future = asyncio.get_running_loop().create_future()
        self.pending[str(self.sequence)] = future
        self.commands[str(self.sequence)] = command
        try:
            await self.ws.send(json.dumps(message))
            return await asyncio.wait_for(future, 8)
        finally:
            self.pending.pop(str(message[key]), None)
            self.parts.pop(str(message[key]), None)
            self.commands.pop(str(message[key]), None)

    async def read(self) -> None:
        """Consume all messages; an overflowing stream reconnects and reseeds."""
        async for raw in self.ws:
            self.receive(json.loads(raw))
        msg = "socket_closed"
        raise ConnectionError(msg)

    def receive(self, message: dict) -> None:
        """Route one parsed message to its request or the event queue."""
        self.wire_sequence += 1
        key = str(message.get("message_id" if self.channel == "ma" else "id"))
        result = (
            message.get("type") == "result"
            if self.channel == "ha"
            else "result" in message or "error_code" in message
        )
        if key in self.pending and result:
            self.command_result(key, message)
        else:
            self.events.put_nowait(
                (Moment(time.time(), time.monotonic()), message, self.wire_sequence)
            )

    def command_result(self, key: str, message: dict) -> None:
        """Resolve a live request, including streamed partial MA results."""
        future = self.pending[key]
        if future.done():
            return
        if message.get("success") is False or message.get("error_code"):
            future.set_exception(RuntimeError("api_command_rejected"))
        elif message.get("partial"):
            self.parts.setdefault(key, []).extend(message.get("result", []))
        else:
            value = message.get("result")
            if key in self.parts:
                value = self.parts[key] + (value or [])
            self.reply_sequences[self.commands[key]] = self.wire_sequence
            future.set_result(value)


class Source:
    """Keep credentials on Odroid and export only scoped playback evidence."""

    def __init__(self, config: dict) -> None:
        """Initialize one fresh source epoch without synthetic playback events."""
        self.config = config
        self.stream_id = uuid.uuid4().hex
        self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.sequence = 0
        self.players: dict[str, dict] = {}
        self.queues: dict[str, dict] = {}
        self.connected: dict[str, bool] = {}
        self.intent_ready = False
        self.intents: dict[str, float] = {}
        self.maintenance = None
        self.provider_refresh: asyncio.Task | None = None
        self.sockets: dict[str, Socket] = {}
        self.seeded: dict[str, bool] = {}
        self.playback_lock = asyncio.Lock()
        self.baseline_sequences: dict[str, int] = {}
        self.pending_ha: dict[str, dict] = {}
        self.ha_origins: dict[str, dict] = {}
        self.save_intents()

    def save_intents(self) -> None:
        """Keep control intent in this boot's uptime, never a cross-host wall clock."""
        atomic_json(
            LOCAL_STATE / "intents.json",
            {
                "clock": "boot_monotonic",
                "boot_id": self.boot_id,
                "targets": self.intents,
                "pending_targets": sorted(
                    {
                        pid
                        for item in self.pending_ha.values()
                        for pid in item["player_ids"]
                    }
                ),
            },
        )

    def status(self) -> None:
        """Persist lifecycle transitions, not periodic health scans."""
        atomic_json(
            LOCAL_STATE / "status.json",
            {
                "pid": os.getpid(),
                "boot_id": self.boot_id,
                "stream_id": self.stream_id,
                "connected": self.connected,
                "intent_ready": self.intent_ready,
                "updated_at": time.time(),
            },
        )

    def emit(self, payload: dict, observed_at: float | Moment | None = None) -> None:
        """Sequence evidence at its source and never print tokens or raw errors."""
        active = maintenance_status() in {"active", "unknown"}
        if active != self.maintenance:
            self.maintenance = active
            self._write({"kind": "maintenance", "active": active}, time.time())
        stamp = time.time() if observed_at is None else observed_at
        if payload.get("kind") == "ha_origin":
            self.hass_origin(payload)
            return
        if payload.get("kind") == "intent":
            if self.own_control(payload):
                return
            payload = self.record_intent(payload, stamp)
        self._write(payload, stamp)

    def record_intent(self, payload: dict, stamp: float | Moment) -> dict:
        """Normalize physical aliases and persist only recent control intent."""
        aliases = {
            alias: pid
            for pid, cfg in self.config.get("players", {}).items()
            for alias in cfg.get("aliases", [])
        }
        payload = {
            **payload,
            "player_ids": sorted(
                {aliases.get(p, p) for p in payload.get("player_ids", [])}
            ),
        }
        observed_mono = (
            stamp.monotonic
            if isinstance(stamp, Moment)
            else time.monotonic() - max(0, time.time() - stamp)
        )
        for target in payload.get("player_ids", []) + payload.get("queue_ids", []):
            self.intents[target] = observed_mono
        self.intents = {
            k: v
            for k, v in self.intents.items()
            if time.monotonic() - v < INTENT_RETENTION_SECONDS
        }
        self.save_intents()
        return payload

    @staticmethod
    def own_control(payload: dict) -> bool:
        """Correlate only our unpredictable, locally claimed queue-resume request."""
        tag = payload.get("origin_tag")
        if payload.get("command") != "player_queues/resume" or not tag:
            return False
        path = (
            LOCAL_STATE
            / "controls"
            / (hashlib.sha256(tag.encode()).hexdigest() + ".json")
        )
        try:
            claim = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            return False
        return (
            claim.get("origin_tag") == tag
            and claim.get("queue_id") in payload.get("queue_ids", [])
            and active_claim(claim)
        )

    def _write(self, payload: dict, stamp: float | Moment) -> None:
        self.sequence += 1
        emitted = time.monotonic()
        frame = {
            "host": "odroidn2",
            "boot_id": self.boot_id,
            "stream_id": self.stream_id,
            "sequence": self.sequence,
            "observed_at": stamp.wall if isinstance(stamp, Moment) else stamp,
            "monotonic": stamp.monotonic
            if isinstance(stamp, Moment)
            else emitted - max(0, time.time() - stamp),
            "emitted_monotonic": emitted,
            **payload,
        }
        sys.stdout.write(
            json.dumps(frame, allow_nan=False, separators=(",", ":")) + "\n"
        )
        sys.stdout.flush()

    def transport(
        self, channel: str, *, connected: bool, reason: str = "network"
    ) -> None:
        """Publish a connection transition independently from player failure."""
        self.connected[channel] = connected
        self.status()
        self.emit(
            {
                "kind": "transport",
                "channel": channel,
                "connected": connected,
                "reason": reason,
            }
        )

    def player(self, raw: dict, stamp: float | Moment) -> None:
        """Emit the configured physical player, excluding hidden protocol aliases."""
        pid = raw.get("player_id")
        if pid not in self.config.get("players", {}):
            return
        clean = normalize_player(
            raw, self.queues, stamp.wall if isinstance(stamp, Moment) else stamp
        )
        media = raw.get("current_media") or {}
        if self.announcing(pid, raw):
            clean["announcement"] = True
        else:
            self.players[pid] = {**clean, "uri": media.get("uri")}
        self.emit({"kind": "player", "player": clean}, stamp)

    @staticmethod
    def announcing(pid: str, raw: dict) -> bool:
        """Exclude our own native or emulated clip from playback recovery evidence."""
        media = raw.get("current_media") or {}
        if media.get("media_type") == "announcement":
            return True
        marker = (
            LOCAL_STATE
            / "announcements"
            / (hashlib.sha256(pid.encode()).hexdigest() + ".json")
        )
        try:
            value = json.loads(marker.read_text())
            return value.get("active") is True and active_claim(value)
        except (OSError, ValueError, TypeError):
            return False

    def log_players(self) -> dict:
        """Resolve transport aliases to one configured physical endpoint."""
        result = dict(self.players)
        for pid, cfg in self.config.get("players", {}).items():
            if pid in self.players:
                for alias in cfg.get("aliases", []):
                    result[alias] = self.players[pid]
        return result

    async def providers(self, socket: Socket) -> None:
        """Refresh provider errors only on startup or a providers-updated event."""
        configs = await socket.call("config/providers")
        if not isinstance(configs, list):
            msg = "invalid_provider_inventory"
            raise TypeError(msg)
        for event in provider_events(configs):
            self.emit(event)

    async def read_playback(self, socket: Socket) -> tuple[dict, dict]:
        """Fetch one playback baseline or explicit lifecycle reconciliation."""
        queues, players = await asyncio.gather(
            socket.call("player_queues/all"), socket.call("players/all")
        )
        require_lists(queues, players)
        normalized = {q["id"]: q for raw in queues if (q := normalize_queue(raw))["id"]}
        selected = {}
        for raw in players:
            if raw.get("player_id") not in self.config.get("players", {}):
                continue
            clean = normalize_player(raw, normalized, time.time())
            selected[clean["id"]] = {
                **clean,
                "uri": (raw.get("current_media") or {}).get("uri"),
            }
        return normalized, selected

    async def seed_ma(self, socket: Socket) -> None:
        """Seed a new baseline that cannot manufacture an interruption."""
        self.seeded["ma"] = False
        await self.check_intent()
        async with self.playback_lock:
            self.queues, self.players = await self.read_playback(socket)
            self.baseline_sequences = {
                "player": socket.reply_sequences.get("players/all", 0),
                "queue": socket.reply_sequences.get("player_queues/all", 0),
            }
            self.emit(
                {
                    "kind": "snapshot",
                    "players": public_players(self.players),
                    "queues": list(self.queues.values()),
                }
            )
        await self.providers(socket)
        self.seeded["ma"] = True

    async def reconcile_current(self, request_id: str) -> None:
        """Read current domain evidence once after a shared subscriber dies."""
        payload = {
            "kind": "reconciliation",
            "request_id": request_id,
            "connected": dict(self.connected),
            "players": [],
            "faults": [],
            "maintenance": maintenance_status(),
        }
        try:
            if "ma" in self.sockets and self.seeded.get("ma"):
                await self.reconcile_ma(payload, self.sockets["ma"])
            if "ha" in self.sockets and self.seeded.get("ha"):
                entries = await self.sockets["ha"].call("config_entries/get")
                require_lists(entries)
                payload["faults"].extend(integration_events(entries, self.config))
            payload["intents"] = dict(self.intents)
            payload["status"] = (
                "fresh"
                if all(self.seeded.get(key) for key in ("ma", "ha"))
                else "incomplete"
            )
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, TimeoutError):
            payload["status"] = "incomplete"
        self.emit(payload)

    async def reconcile_ma(self, payload: dict, socket: Socket) -> None:
        """Read current MA state without draining or replacing live event history."""
        _, players = await self.read_playback(socket)
        configs = await socket.call("config/providers")
        require_lists(configs)
        payload["players"] = public_players(players)
        payload["faults"] = provider_events(configs)

    async def ma_event(
        self, socket: Socket, message: dict, stamp: float | Moment, sequence: int = 0
    ) -> None:
        """Route supported Music Assistant push events into scoped evidence."""
        async with self.playback_lock:
            kind = str(message.get("event", ""))
            family = (
                "player"
                if kind.startswith("player_")
                else "queue"
                if kind.startswith("queue_")
                else None
            )
            if family and sequence <= self.baseline_sequences.get(family, -1):
                return
            await self.apply_ma_event(socket, message, stamp)

    async def apply_ma_event(
        self, socket: Socket, message: dict, stamp: float | Moment
    ) -> None:
        """Apply a delta only after its corresponding snapshot response boundary."""
        kind, data = message.get("event"), message.get("data")
        if kind in {"player_added", "player_updated"} and isinstance(data, dict):
            self.player(data, stamp)
        elif kind in {"queue_added", "queue_updated"} and isinstance(data, dict):
            queue = normalize_queue(data)
            if queue["id"]:
                self.queues[queue["id"]] = queue
                refresh_queue_sources(self.players, queue)
                self.emit({"kind": "queue", "queue": queue}, stamp)
        elif kind == "player_removed" and message.get("object_id") in self.players:
            pid = message["object_id"]
            player = dict(self.players[pid])
            player.pop("uri", None)
            player["available"] = False
            self.emit({"kind": "player", "player": player}, stamp)
        elif kind == "providers_updated":
            await self.providers(socket)
        elif kind == "application_shutdown":
            self.transport("ma", connected=False)

    def ha_event(self, message: dict, stamp: float | Moment) -> None:
        """Handle enabled integration faults and explicit HA playback controls."""
        event = message.get("event", {})
        if isinstance(event, list):
            entries = [change.get("entry", {}) for change in event]
            for payload in integration_events(entries, self.config):
                self.emit(payload, stamp)
            return
        if event.get("event_type") != "call_service":
            return
        self.ha_control(event, stamp)

    def ha_control(self, event: dict, stamp: float | Moment) -> None:
        """Hold possible user controls until exact native provenance resolves."""
        data = event.get("data", {})
        if (
            data.get("domain") != "media_player"
            or data.get("service") not in HA_CONTROLS
        ):
            return
        if (
            data.get("service") == "play_media"
            and data.get("service_data", {}).get("announce") is True
        ):
            return
        targets = entity_targets(data.get("service_data", {}).get("entity_id", []))
        players = [
            pid
            for pid, cfg in self.config.get("players", {}).items()
            if not targets or set(targets) & set(cfg.get("entities", []))
        ]
        if not players:
            return
        payload = {
            "kind": "intent",
            "command": "ha:" + data["service"],
            "player_ids": players,
            "queue_ids": [],
        }
        self.hold_ha_control(payload, event.get("context") or {}, targets, stamp)

    def hold_ha_control(
        self, payload: dict, context: dict, targets: list, stamp: float | Moment
    ) -> None:
        """Keep each pending service/target identity until provenance or timeout."""
        context_id = identity(context.get("id"))
        if not context_id:
            self.emit(payload, stamp)
            return
        pending = {
            **payload,
            "context_id": context_id,
            "parent_id": identity(context.get("parent_id")),
            "service": payload["command"].removeprefix("ha:"),
            "entities": targets,
            "expires": time.monotonic() + HA_ORIGIN_TIMEOUT,
        }
        if any(
            self.origin_matches(pending, origin) for origin in self.ha_origins.values()
        ):
            return
        intent_id = hashlib.sha256(
            json.dumps(
                [context_id, payload["command"].removeprefix("ha:"), sorted(targets)]
            ).encode()
        ).hexdigest()
        previous = self.pending_ha.pop(intent_id, None)
        if previous:
            previous["timer"].cancel()
        if len(self.pending_ha) >= MAX_PENDING_ORIGINS:
            # Unresolved controls remain conservative user intent on overflow.
            self.expire_ha_intent(next(iter(self.pending_ha)))
        pending["timer"] = asyncio.get_running_loop().call_later(
            HA_ORIGIN_TIMEOUT, self.expire_ha_intent, intent_id
        )
        self.pending_ha[intent_id] = pending
        self.save_intents()
        self.emit(
            {
                **payload,
                "kind": "intent_pending",
                "context_id": context_id,
                "intent_id": intent_id,
            },
            stamp,
        )

    @staticmethod
    def origin_matches(pending: dict, origin: dict) -> bool:
        """Authorize only the proven context lineage and target subset."""
        targets, proven = (
            set(pending.get("entities", [])),
            set(origin.get("entity_ids", [])),
        )
        if not targets or not targets <= proven:
            return False
        context_id = origin.get("context_id")
        # HA may reuse the same Context for nested services; provenance follows
        # that exact lineage, never a household account or a time/target guess.
        direct = pending.get("context_id") == context_id
        return direct or pending.get("parent_id") == context_id

    def hass_origin(self, origin: dict) -> None:
        """Resolve tentative HA control only with matching trusted MA provenance."""
        now = time.monotonic()
        self.ha_origins = {
            key: item for key, item in self.ha_origins.items() if item["expires"] > now
        }
        previous = self.ha_origins.get(origin["context_id"], {})
        origin = {
            **origin,
            "entity_ids": sorted(
                set(origin["entity_ids"]) | set(previous.get("entity_ids", []))
            ),
        }
        if len(self.ha_origins) >= MAX_PENDING_ORIGINS:
            del self.ha_origins[next(iter(self.ha_origins))]
        self.ha_origins[origin["context_id"]] = {
            **origin,
            "expires": now + HA_ORIGIN_TIMEOUT,
        }
        for intent_id, pending in list(self.pending_ha.items()):
            if self.origin_matches(pending, origin):
                pending["timer"].cancel()
                del self.pending_ha[intent_id]
                self.save_intents()
                self.emit({"kind": "intent_resolved", "intent_id": intent_id})

    def expire_ha_intent(self, intent_id: str) -> None:
        """Unmatched or failed native provenance stays conservatively user intent."""
        pending = self.pending_ha.pop(intent_id, None)
        if pending is None:
            return
        pending["timer"].cancel()
        self.emit(
            {
                "kind": "intent",
                "command": pending["command"],
                "player_ids": pending["player_ids"],
                "queue_ids": [],
            }
        )
        self.save_intents()
        self.emit({"kind": "intent_resolved", "intent_id": intent_id})

    async def session(self, channel: str) -> None:
        """Authenticate normally, subscribe once, then consume push events."""
        from websockets.asyncio.client import (  # noqa: PLC0415 - optional wire runtime.
            connect,
        )

        url = (
            "ws://127.0.0.1:8095/ws"
            if channel == "ma"
            else "ws://127.0.0.1:8123/api/websocket"
        )
        async with connect(
            url,
            proxy=None,
            open_timeout=8,
            ping_interval=20,
            ping_timeout=10,
            max_size=4 * 1024 * 1024,
        ) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 8))
            if channel == "ha":
                if first.get("type") != "auth_required":
                    msg = "unexpected_ha_handshake"
                    raise RuntimeError(msg)
                await ws.send(json.dumps({"type": "auth", "access_token": ha_token()}))
            else:
                await ws.send(
                    json.dumps(
                        {
                            "message_id": 1,
                            "command": "auth",
                            "args": {"token": ma_token()},
                        }
                    )
                )
            reply = json.loads(await asyncio.wait_for(ws.recv(), 8))
            authenticated = (
                reply.get("type") == "auth_ok"
                if channel == "ha"
                else (reply.get("result") or {}).get("authenticated") is True
            )
            if not authenticated:
                msg = "authentication_failed"
                raise PermissionError(msg)
            socket = Socket(ws, channel)
            self.sockets[channel] = socket
            reader = asyncio.create_task(socket.read())
            try:
                if channel == "ma":
                    await self.seed_ma(socket)
                else:
                    await socket.call(
                        "subscribe_events", {"event_type": "call_service"}
                    )
                    await socket.call("config_entries/subscribe")
                    self.seeded["ha"] = True
                self.transport(channel, connected=True)
                await self.consume(socket, reader, channel)
            finally:
                self.sockets.pop(channel, None)
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader

    async def consume(self, socket: Socket, reader: asyncio.Task, channel: str) -> None:
        """Wake only for a live socket event or terminal reader failure."""
        while True:
            event_task = asyncio.create_task(socket.events.get())
            done, _ = await asyncio.wait(
                {event_task, reader}, return_when=asyncio.FIRST_COMPLETED
            )
            if reader in done:
                event_task.cancel()
                await reader
            stamp, message, sequence = await event_task
            if channel == "ma":
                await self.ma_event(socket, message, stamp, sequence)
            else:
                self.ha_event(message, stamp)

    async def reconnect(self, channel: str) -> None:
        """Use one-shot backoff only after a known connection failure."""
        delay = 1
        while True:
            self.seeded[channel] = False
            try:
                await self.session(channel)
            except Exception as error:  # noqa: BLE001 - supervised stream boundary; only error type escapes.
                if self.seeded.get(channel):
                    delay = 1
                self.seeded[channel] = False
                self.transport(
                    channel,
                    connected=False,
                    reason="auth"
                    if isinstance(error, PermissionError)
                    else failure_reason(type(error).__name__),
                )
                LOG.warning("Stream %s failed: %s", channel, type(error).__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def logs(self) -> None:
        """Subscribe only to new container output; old errors are never replayed."""
        delay = 1
        while True:
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    "docker",
                    "logs",
                    "--follow",
                    "--tail",
                    "0",
                    "--timestamps",
                    CONTAINER,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                await self.log_session(process)
            except (OSError, ValueError, ConnectionError) as error:
                LOG.warning("Log stream unavailable: %s", type(error).__name__)
            finally:
                if self.connected.get("logs"):
                    delay = 1
                if process and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 2)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                self.transport("logs", connected=False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def log_session(self, process: object) -> None:
        """Confirm usable output or a surviving log attachment."""
        reader = asyncio.create_task(self.read_logs(process.stdout))
        exited = asyncio.create_task(process.wait())
        attached = asyncio.create_task(asyncio.sleep(1))
        try:
            done, _ = await asyncio.wait(
                {reader, exited, attached}, return_when=asyncio.FIRST_COMPLETED
            )
            if attached in done and process.returncode is None:
                self.transport("logs", connected=True)
                await reader
            elif reader in done:
                await reader
            message = "log_stream_ended"
            raise ConnectionError(message)
        finally:
            for task in (reader, exited, attached):
                task.cancel()
            await asyncio.gather(reader, exited, attached, return_exceptions=True)

    async def read_logs(self, stream: asyncio.StreamReader) -> None:
        """Discard malformed log records without losing the source."""
        while True:
            try:
                raw = await stream.readline()
            except ValueError:
                LOG.warning("Oversized container log record discarded")
                continue
            if not raw:
                return
            stamp_text, _, body = raw.decode(errors="replace").strip().partition(" ")
            try:
                stamp = datetime.fromisoformat(stamp_text).timestamp()
            except ValueError:
                continue
            if not self.connected.get("logs"):
                self.transport("logs", connected=True)
            if 0 <= time.time() - stamp <= MAX_LOG_DELAY:
                for event in log_events(body, self.log_players()):
                    self.emit(event, stamp)

    async def check_intent(self) -> None:
        """Recheck installed telemetry for every new MA connection epoch."""
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            CONTAINER,
            "/app/venv/bin/python",
            "-c",
            "from music_assistant.helpers.ha_recovery_intent import VERSION; "
            "print(VERSION)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 8)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        self.intent_ready = process.returncode == 0 and output.strip() == b"2"
        self.status()

    async def run(self) -> None:
        """Start persistent streams; each connection proves its own readiness."""
        self.status()
        self.transport("stream", connected=True)
        workers = [
            asyncio.create_task(work)
            for work in (
                self.reconnect("ma"),
                self.reconnect("ha"),
                self.logs(),
                watch_marker(MAINTENANCE, self.maintenance_event),
                self.parent_lifetime(),
            )
        ]
        try:
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            for pending in self.pending_ha.values():
                pending["timer"].cancel()
            self.connected = dict.fromkeys(self.connected, False)
            self.status()

    async def maintenance_event(self, kind: str) -> None:
        """Hold repairs during maintenance and reseed once its lock is removed."""
        status = maintenance_status()
        active = status in {"active", "unknown"}
        if (kind == "expired" and active) or status == "stale":
            self.maintenance = active
            self._write({"kind": "maintenance", "active": active}, time.time())
            self.emit(
                {
                    "kind": "fault",
                    "scope": "maintenance:stale",
                    "provider": "maintenance",
                    "reason": "code",
                    "maintenance_fault": True,
                }
            )
            return
        was_active = self.maintenance
        self.maintenance = active
        self._write({"kind": "maintenance", "active": active}, time.time())
        if not active:
            self.emit(
                {
                    "kind": "fault",
                    "scope": "maintenance:stale",
                    "provider": "maintenance",
                    "recovered": True,
                }
            )
            if was_active:
                await self.reseed_after_maintenance()

    async def reseed_after_maintenance(self) -> None:
        """Recover every suppressed HA/MA edge from one current snapshot."""
        for channel, connected in tuple(self.connected.items()):
            self.transport(channel, connected=connected)
        for channel in ("ma", "ha"):
            socket = self.sockets.get(channel)
            if socket is None:
                continue
            try:
                if channel == "ma":
                    await self.seed_ma(socket)
                else:
                    entries = await socket.call("config_entries/get")
                    require_lists(entries)
                    for event in integration_events(entries, self.config):
                        self.emit(event)
            except (
                OSError,
                ValueError,
                RuntimeError,
                TimeoutError,
                TypeError,
            ) as error:
                self.transport(
                    channel,
                    connected=False,
                    reason=failure_reason(type(error).__name__),
                )
                LOG.warning(
                    "Maintenance reseed deferred for %s: %s",
                    channel,
                    type(error).__name__,
                )
                await socket.ws.close()

    async def parent_lifetime(self) -> None:
        """Read only explicit lifecycle refresh requests; EOF ends this source."""
        reader = asyncio.StreamReader(limit=1024)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
        )
        try:
            while raw := await reader.readline():
                try:
                    request = json.loads(raw)
                    request_id = identity(request.get("request_id"))
                except (ValueError, AttributeError):
                    continue
                if request.get("type") == "reconcile" and request_id:
                    await self.reconcile_current(request_id)
        finally:
            transport.close()


def main() -> None:
    """Run the source over one SSH stream; output contains no credentials."""
    init_log()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG)
    source = Source(json.loads(parser.parse_args().config.read_text()))
    asyncio.run(source.run())


if __name__ == "__main__":
    main()
