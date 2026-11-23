#!/usr/bin/env python3
# Copyright (c) 2026 Alex Mextner
"""Event-driven playback incident policy and shared repair subscriber adapter."""

from __future__ import annotations

import copy
import logging
import os
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG = logging.getLogger("ha_smart_home_recovery")
ROOT = Path.home() / ".local/state/ha-smart-home-recovery"
REMOTE = "/home/ultra/.local/share/ha-smart-home-recovery"
FRESHNESS = 10
NETWORK_DELAY = 5
REASONS = {"network", "auth", "configuration", "code", "unknown"}
LOG_RETENTION_SECONDS = 172800


def init_log() -> Path:
    """Keep private, bounded rotating logs for the long-lived event service."""
    directory = Path(tempfile.gettempdir()) / f"ha-smart-home-events-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        msg = "untrusted_log_directory"
        raise PermissionError(msg)
    directory.chmod(0o700)
    for old in directory.glob("run-*.log*"):
        if old.is_file() and time.time() - old.stat().st_mtime > LOG_RETENTION_SECONDS:
            old.unlink()
    path = directory / f"run-{time.time_ns()}-{os.getpid()}.log"
    path.touch(mode=0o600)
    handler = RotatingFileHandler(path, maxBytes=1048576, backupCount=2)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), handler],
    )
    LOG.info("Starting; log=%s", path)
    return path


def incident_broken(
    scope: str, incident: dict, evidence: dict, players: dict, faults: dict
) -> bool | None:
    """Interpret fresh evidence without treating absence as service recovery."""
    if scope in faults:
        recovered = faults[scope].get("recovered")
        return not recovered if type(recovered) is bool else None
    if scope.startswith("monitor:"):
        current = evidence.get("connected", {}).get(scope.removeprefix("monitor:"))
        return not current if type(current) is bool else None
    if scope.startswith("player:"):
        pid = scope.removeprefix("player:")
        player = players.get(pid, {})
        prior = incident.get("playback", {}).get(pid, {})
        return interrupted_player(
            player, prior, evidence.get("intents", {}).get(pid, 0)
        )
    if scope == "maintenance:stale":
        status = evidence.get("maintenance")
        return status == "stale" if status in {"stale", "absent"} else None
    return None


def interrupted_player(player: dict, prior: dict, intent_at: float) -> bool | None:
    """Keep unknown playback evidence distinct from actual recovery."""
    if player.get("available") is False:
        return True
    if player.get("available") is not True or player.get("state") not in {
        "idle",
        "paused",
        "playing",
    }:
        return None
    return (
        player.get("state") == "idle"
        and bool(prior.get("item_id"))
        and player.get("queue_id") == prior.get("queue_id")
        and player.get("item_id") == prior.get("item_id")
        and intent_at < prior.get("interrupted_at", 0)
    )


def index_records(records: list, key: str) -> dict:
    """Index sanitized evidence records with an explicit identity."""
    return {
        record[key]: record
        for record in records
        if isinstance(record, dict) and key in record
    }


class Engine:
    """Correlate actual playback transitions; no agent ownership is stored here."""

    def __init__(self, state: dict) -> None:
        """Restore durable domain receipts while requiring a fresh baseline."""
        self.state = copy.deepcopy(state)
        self.state.setdefault("incidents", {})
        self.state.setdefault("outbox", {})
        self.state.setdefault("spoken", {})
        self.state.setdefault("resumed", {})
        self.state.setdefault("subscriber_losses", {})
        self.players: dict[str, dict] = {}
        self.queues: dict[str, dict] = {}
        self.intents: dict[str, float] = {}
        self.pending: dict[str, dict] = {}
        self.restore: dict[str, dict] = {}
        self.faults: dict[str, dict] = {}
        self.stopped: dict[str, tuple[dict, float]] = {}
        self.stream = None
        self.sequence = 0
        self.maintenance = False

    def _admit(self, event: dict, now: float) -> bool:
        if not isinstance(event, dict):
            return False
        stamp, seq = event.get("observed_at"), event.get("sequence")
        if type(stamp) not in (float, int) or not 0 <= now - stamp <= FRESHNESS:
            self.pending.clear()
            self.restore.clear()
            return False
        if (
            type(seq) is not int
            or seq < 1
            or not event.get("boot_id")
            or not event.get("stream_id")
        ):
            return False
        stream = (event["boot_id"], event["stream_id"])
        if stream != self.stream:
            self.players.clear()
            self.queues.clear()
            self.pending.clear()
            self.restore.clear()
            self.faults.clear()
            self.stopped.clear()
            self.stream, self.sequence = stream, seq - 1
        if seq <= self.sequence:
            return False
        if seq != self.sequence + 1:
            self.players.clear()
            self.pending.clear()
            self.restore.clear()
        self.sequence = seq
        return True

    def _publish(self, incident: dict, phase: str, now: float) -> dict:
        incident["published"] = True
        incident.pop("dispatch_at", None)
        incident["revision"] = incident.get("revision", 0) + 1
        event_id = f"{incident['id']}:{incident['revision']}:{phase}"
        event = {
            "event_id": event_id,
            "phase": phase,
            "observed_at": datetime.fromtimestamp(now, UTC).isoformat(),
            "kind": incident["reason"],
            "source": "smart-home",
            "scope": incident["scope"],
            "incident_id": incident["id"],
            "affected_players": list(incident["players"]),
            "evidence": {"reason": incident["reason"], "scope": incident["scope"]},
        }
        action = {"type": "publish", "event": event}
        self.state["outbox"][event_id] = action
        return action

    def _open(
        self, scope: str, reason: str, now: float, player_id: str | None = None
    ) -> tuple[dict, list]:
        incidents = self.state["incidents"]
        incident = incidents.get(scope)
        fresh = not incident or incident.get("closed") is not None
        if fresh:
            incident = {
                "id": uuid.uuid4().hex,
                "scope": scope,
                "reason": reason,
                "opened": now,
                "closed": None,
                "players": [],
                "dispatch_at": now + NETWORK_DELAY,
                "published": False,
            }
            incidents[scope] = incident
        changed = reason != incident["reason"]
        incident["reason"] = reason
        if player_id and player_id not in incident["players"]:
            incident["players"].append(player_id)
            changed = True
        if reason == "network" and not incident["published"]:
            return incident, []
        return incident, (
            [
                self._publish(
                    incident, "updated" if incident["published"] else "opened", now
                )
            ]
            if fresh or changed or not incident["published"]
            else []
        )

    def _close(self, scope: str, now: float) -> list:
        incident = self.state["incidents"].get(scope)
        if not incident or incident.get("closed") is not None:
            return []
        incident["closed"] = now
        incident.pop("dispatch_at", None)
        if not incident.get("published"):
            return []
        return [self._publish(incident, "recovered", now)]

    def _cancel(self, player_id: str) -> None:
        self.restore.pop(player_id, None)
        pending = self.pending.pop(player_id, None)
        incident = self.state["incidents"].get("player:" + player_id)
        if pending and incident and not incident.get("published"):
            incident["closed"] = pending["interrupted_at"]
            incident.pop("dispatch_at", None)

    def _interrupt(self, previous: dict, now: float) -> list:
        identity = previous["id"]
        if self.intents.get(identity, 0) > now:
            return []
        if previous.get("state") != "playing" or not previous.get("item_id"):
            return []
        fault = self._cause(previous) or {}
        reason = fault.get("reason", "network")
        scope = fault.get("scope", "player:" + identity)
        incident, actions = self._open(scope, reason, now, identity)
        claim = incident["id"] + ":" + identity
        if claim in self.state["spoken"] or identity in self.pending:
            return actions
        self.pending[identity] = {
            "player_id": identity,
            "queue_id": previous.get("queue_id"),
            "item_id": previous["item_id"],
            "reason": reason,
            "incident_id": incident["id"],
            "claim": claim,
            "interrupted_at": now,
            "deadline": now + (NETWORK_DELAY if reason == "network" else 0),
        }
        self.restore[identity] = {**self.pending[identity], "ready": False}
        incident.setdefault("playback", {})[identity] = {
            "queue_id": previous.get("queue_id"),
            "item_id": previous["item_id"],
            "interrupted_at": now,
        }
        return actions + (self.due(now) if reason != "network" else [])

    def _player(self, player: dict, now: float) -> list:
        identity = player.get("id")
        if not isinstance(identity, str) or player.get("announcement"):
            return []
        previous = self.players.get(identity)
        self.players[identity] = player
        if not previous:
            return []
        if player.get("state") == "paused" or player.get("enabled") is False:
            self._cancel(identity)
            self.intents[identity] = now + 30
        elif player.get("item_id") != previous.get("item_id") or player.get(
            "queue_id"
        ) != previous.get("queue_id"):
            self._cancel(identity)
        elif player.get("available") is True and player.get("state") == "playing":
            self._cancel(identity)
            self.stopped.pop(identity, None)
            return self._close("player:" + identity, now)
        else:
            if (
                previous.get("available") is False
                and player.get("available") is True
                and identity in self.restore
            ):
                self.restore[identity]["ready"] = True
                return self._resume_actions(now)
            return self._player_failure(previous, player, now)
        return []

    def _player_failure(self, previous: dict, player: dict, now: float) -> list:
        if previous.get("state") == "playing" and player.get("state") == "idle":
            self.stopped[player["id"]] = (previous, now)
        fault = self._cause(player)
        broken = player.get("available") is False or (
            fault and player.get("state") == "idle"
        )
        if broken and previous.get("available") is True:
            return self._interrupt(previous, now)
        return []

    def _cause(self, player: dict) -> dict | None:
        for fault in reversed(list(self.faults.values())):
            if fault.get("players") and player.get("id") not in fault["players"]:
                continue
            sources = {player.get("provider"), player.get("source_provider")} - {None}
            if sources & {fault.get("provider"), fault.get("domain")}:
                return fault
        return None

    def _queue(self, queue: dict, _now: float) -> list:
        identity = queue.get("id")
        self.queues[identity] = queue
        for pid, pending in {**self.restore, **self.pending}.items():
            if pending["queue_id"] == identity and (
                queue.get("state") == "paused"
                or queue.get("item_id") != pending["item_id"]
            ):
                self._cancel(pid)
        return []

    def _intent(self, event: dict, now: float) -> list:
        players = set(event.get("player_ids", []))
        queues = set(event.get("queue_ids", []))
        players.update(
            pid for pid, p in self.players.items() if p.get("queue_id") in queues
        )
        for pid in players:
            self.intents[pid] = now + 30
            self._cancel(pid)
            self.stopped.pop(pid, None)
        return []

    def _fault(self, event: dict, now: float) -> list:
        scope, provider = event.get("scope"), event.get("provider")
        if not isinstance(scope, str):
            return []
        if event.get("recovered"):
            return self._recover_fault(scope, provider, now)
        reason = event.get("reason") if event.get("reason") in REASONS else "unknown"
        self.faults[scope] = {
            "reason": reason,
            "scope": scope,
            "provider": provider,
            "domain": event.get("domain"),
            "players": event.get("player_ids", []),
        }
        observed = self._correlate_playback_fault(event, now)
        if event.get("playback_fault") and reason == "network":
            return observed
        incident, actions = self._open(scope, reason, now)
        actions.extend(self._upgrade_pending_fault(scope, incident, reason, now))
        return observed + actions + self.due(now)

    def _recover_fault(self, scope: str, provider: str, now: float) -> list:
        self.faults.pop(scope, None)
        for pid, waiting in self.restore.items():
            player = self.players.get(pid, {})
            if provider in {player.get("provider"), player.get("source_provider")}:
                waiting["ready"] = True
        return self._close(scope, now) + self._resume_actions(now)

    def _correlate_playback_fault(self, event: dict, now: float) -> list:
        actions = []
        if event.get("playback_fault"):
            for pid in event.get("player_ids", []):
                prior, stamp = self.stopped.get(pid, ({}, 0))
                if prior and 0 <= now - stamp <= NETWORK_DELAY:
                    actions.extend(self._interrupt(prior, stamp))
        return actions

    def _upgrade_pending_fault(
        self, scope: str, incident: dict, reason: str, now: float
    ) -> list:
        actions = []
        for pid, pending in self.pending.items():
            if self._cause(self.players.get(pid, {})) != self.faults[scope]:
                continue
            actions.extend(self._close("player:" + pid, now))
            pending.update(
                reason=reason,
                incident_id=incident["id"],
                claim=incident["id"] + ":" + pid,
                deadline=now if reason != "network" else pending["deadline"],
            )
        return actions

    def _snapshot(self, event: dict, _now: float) -> list:
        self.players = {
            p["id"]: p
            for p in event.get("players", [])
            if isinstance(p, dict) and "id" in p
        }
        self.queues = {
            q["id"]: q
            for q in event.get("queues", [])
            if isinstance(q, dict) and "id" in q
        }
        self.pending.clear()
        self.restore.clear()
        self.faults.clear()
        self.stopped.clear()
        return []

    def _transport(self, event: dict, now: float) -> list:
        channel = event.get("channel", "stream")
        if event.get("connected"):
            return self._close("monitor:" + channel, now)
        if channel in {"ma", "stream"}:
            self.pending.clear()
            self.restore.clear()
            self.players.clear()
            self.faults.clear()
        _, actions = self._open(
            "monitor:" + channel, event.get("reason", "network"), now
        )
        return actions

    def feed(self, event: dict, now: float) -> list:
        """Consume one sequenced, fresh event without scanning services."""
        if not self._admit(event, now):
            return []
        now = event["observed_at"]
        kind = event.get("kind")
        if kind == "maintenance":
            self.maintenance = event.get("active") is True
            if self.maintenance:
                for pid in list(self.pending):
                    self._cancel(pid)
            self.pending.clear()
            self.restore.clear()
            self.players.clear()
            return []
        if kind == "snapshot":
            return self._snapshot(event, now)
        if self.maintenance and not event.get("maintenance_fault"):
            return []
        nested = {"player": self._player, "queue": self._queue}.get(kind)
        if nested:
            return nested(event.get(kind, {}), now)
        handler = {
            "intent": self._intent,
            "fault": self._fault,
            "transport": self._transport,
        }.get(kind)
        return handler(event, now) if handler else []

    def due(self, now: float) -> list:
        """Claim only scheduled interruption deadlines, never healthy scans."""
        if self.maintenance:
            return []
        actions = [
            self._publish(i, "opened", now)
            for i in self.state["incidents"].values()
            if i.get("closed") is None and i.get("dispatch_at", float("inf")) <= now
        ]
        for pid, pending in list(self.pending.items()):
            if pending["deadline"] > now:
                continue
            del self.pending[pid]
            if self.maintenance or self.intents.get(pid, 0) > now:
                continue
            claim = pending["claim"]
            if claim in self.state["spoken"]:
                continue
            self.state["spoken"][claim] = {
                "attempted_at": now,
                "status": "pending",
                "player_id": pid,
            }
            actions.append(
                {
                    "type": "announce",
                    **pending,
                    "stream_id": self.stream[1] if self.stream else None,
                    "not_after": pending["deadline"] + 3,
                }
            )
        return actions + self._resume_actions(now)

    def reconcile_subscriber_loss(self, evidence: dict, now: float) -> list:
        """Reissue open work only from fresh evidence after positive native death."""
        if not self._loss_ready(evidence, now):
            return []
        players = index_records(evidence.get("players", []), "id")
        faults = index_records(evidence.get("faults", []), "scope")
        actions = []
        for scope, incident in self.state["incidents"].items():
            if incident.get("closed") is not None:
                continue
            broken = incident_broken(scope, incident, evidence, players, faults)
            if broken is True:
                actions.append(self._publish(incident, "updated", now))
            elif broken is False:
                actions.extend(self._close(scope, now))
        self.state["subscriber_losses"][evidence["request_id"]]["reconciled"] = True
        return actions

    def _loss_ready(self, evidence: dict, now: float) -> bool:
        loss_id, stamp = evidence.get("request_id"), evidence.get("observed_at")
        return (
            type(stamp) in (int, float)
            and 0 <= now - stamp <= FRESHNESS
            and (evidence.get("boot_id"), evidence.get("stream_id")) == self.stream
            and evidence.get("status") == "fresh"
            and loss_id in self.state["subscriber_losses"]
            and not self.state["subscriber_losses"][loss_id].get("reconciled")
        )

    def _resume_actions(self, now: float) -> list:
        actions = []
        for pid, waiting in list(self.restore.items()):
            player = self.players.get(pid, {})
            if (
                not waiting["ready"]
                or now < waiting["interrupted_at"] + NETWORK_DELAY
                or player.get("available") is not True
                or player.get("state") != "idle"
                or self._cause(player)
                or self.intents.get(pid, 0) > now
            ):
                continue
            claim = "resume:" + waiting["claim"]
            if claim in self.state["resumed"]:
                del self.restore[pid]
                continue
            self.state["resumed"][claim] = {"attempted_at": now, "status": "claimed"}
            del self.restore[pid]
            actions.append(
                {
                    **waiting,
                    "type": "resume",
                    "claim": claim,
                    "deadline": now,
                    "not_after": now + 30,
                    "stream_id": self.stream[1] if self.stream else None,
                }
            )
        return actions

    def next_deadline(self) -> float | None:
        """Return the next one-shot deadline, or no timer at all."""
        if self.maintenance:
            return None
        deadlines = [p["deadline"] for p in self.pending.values()]
        deadlines.extend(
            p["interrupted_at"] + NETWORK_DELAY
            for pid, p in self.restore.items()
            if p["ready"]
            and self.players.get(pid, {}).get("available") is True
            and self.players.get(pid, {}).get("state") == "idle"
            and not self._cause(self.players.get(pid, {}))
        )
        deadlines.extend(
            i["dispatch_at"]
            for i in self.state["incidents"].values()
            if i.get("closed") is None and "dispatch_at" in i
        )
        return min(deadlines, default=None)
