#!/usr/bin/env python3
# Copyright (c) 2026 Alex Mextner
"""Long-lived event consumer with independent speech and subscriber delivery."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import time
import uuid
from datetime import datetime
from pathlib import Path

from ha_smart_home_common import atomic_json, durable_delivery, identity
from ha_smart_home_recovery import LOG, Engine, init_log

ROOT = Path.home() / ".local/state/ha-smart-home-recovery"
REMOTE = "/home/ultra/.local/share/ha-smart-home-recovery"
SSH = [
    "/usr/bin/ssh",
    "-oBatchMode=yes",
    "-oConnectTimeout=8",
    "-oServerAliveInterval=10",
    "-oServerAliveCountMax=2",
    "odroidn2",
]
BROKER = str(Path.home() / ".local/bin/agent-sub")
STATE_SCHEMA = 2
LOSS_REASONS = {
    "process_exited",
    "subscription_released",
    "authority_vacant_after_restart",
}


def valid_number(value: object) -> bool:
    """Accept finite timestamps, excluding booleans and malformed JSON values."""
    return type(value) in (int, float) and math.isfinite(value)


def validate_state(state: dict) -> None:
    """Validate nested durable data before it can suppress an incident forever."""
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
        msg = "state_schema"
        raise ValueError(msg)
    for key in ("incidents", "outbox", "spoken", "resumed", "subscriber_losses"):
        if not isinstance(state.get(key, {}), dict):
            msg = "state_mapping"
            raise TypeError(msg)
    for incident in state.get("incidents", {}).values():
        validate_incident(incident)
    validate_delivery_state(state)


def validate_incident(incident: dict) -> None:
    """Validate an incident's identity and decision fields."""
    valid = (
        isinstance(incident, dict)
        and isinstance(incident.get("players"), list)
        and isinstance(incident.get("id"), str)
        and isinstance(incident.get("scope"), str)
        and isinstance(incident.get("published"), bool)
        and type(incident.get("revision", 0)) is int
        and all(isinstance(p, str) for p in incident.get("players", []))
        and incident.get("reason")
        in {"network", "auth", "configuration", "code", "unknown"}
    )
    if not valid or not valid_number(incident.get("opened")):
        msg = "incident_schema"
        raise ValueError(msg)
    validate_incident_times(incident)


def validate_incident_times(incident: dict) -> None:
    """Validate optional timestamps and exact prior-playback identity."""
    if any(
        incident.get(k) is not None and not valid_number(incident[k])
        for k in ("closed", "dispatch_at")
    ):
        msg = "incident_timestamp"
        raise ValueError(msg)
    playback = incident.get("playback", {})
    if not isinstance(playback, dict) or any(
        not valid_prior_playback(v) for v in playback.values()
    ):
        msg = "playback_contract"
        raise ValueError(msg)


def valid_prior_playback(value: object) -> bool:
    """Check the persisted evidence required before an exact queue resume."""
    return (
        isinstance(value, dict)
        and valid_number(value.get("interrupted_at"))
        and isinstance(value.get("item_id"), str)
        and isinstance(value.get("queue_id"), str)
    )


def validate_delivery_state(state: dict) -> None:
    """Validate effect and lifecycle receipts before processing durable work."""
    for eid, action in state.get("outbox", {}).items():
        if (
            not isinstance(action, dict)
            or action.get("type") != "publish"
            or not isinstance(action.get("event"), dict)
            or action["event"].get("event_id") != eid
        ):
            msg = "outbox_schema"
            raise ValueError(msg)
    for receipt in list(state.get("spoken", {}).values()) + list(
        state.get("resumed", {}).values()
    ):
        if not isinstance(receipt, dict) or not valid_number(
            receipt.get("attempted_at")
        ):
            msg = "speech_schema"
            raise ValueError(msg)
    for loss in state.get("subscriber_losses", {}).values():
        if (
            not isinstance(loss, dict)
            or type(loss.get("reconciled")) is not bool
            or not valid_number(loss.get("observed_at"))
        ):
            msg = "subscriber_loss_schema"
            raise ValueError(msg)


def load_state(root: Path) -> dict:
    """Quarantine invalid or superseded state; shared subscriber ownership survives."""
    path = root / "state.json"
    try:
        state = json.loads(path.read_text())
        validate_state(state)
    except FileNotFoundError:
        return {"schema": STATE_SCHEMA}
    except (ValueError, TypeError, AttributeError):
        path.replace(root / f"state-quarantined-{time.time_ns()}.json")
        return {"schema": STATE_SCHEMA}
    else:
        return state


async def command_json(
    argv: list[str], payload: dict | None = None, limit_seconds: int = 110
) -> dict:
    """Run a fixed command without a shell and bound its complete lifetime."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(
            process.communicate(json.dumps(payload).encode() if payload else None),
            limit_seconds,
        )
        if process.returncode != 0 or len(out) > 2 * 1024 * 1024:
            msg = "command_failed"
            raise RuntimeError(msg)
        result = json.loads(out)
        if not isinstance(result, dict):
            msg = "invalid_receipt"
            raise TypeError(msg)
        return result
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()


class Runtime:
    """Persist domain claims, while the shared broker alone owns repair agents."""

    def __init__(self, root: Path, engine: Engine | None = None) -> None:
        """Start with private state and no private agent ownership registry."""
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.engine = engine or Engine(load_state(root))
        self.engine.state["schema"] = STATE_SCHEMA
        self.inbox: asyncio.Queue = asyncio.Queue(maxsize=2048)
        self.effects: dict[str, asyncio.Task] = {}
        self.player_locks: dict[str, asyncio.Lock] = {}
        self.deadline: asyncio.TimerHandle | None = None
        self.retry_at: float | None = None
        self.stored: str | None = None
        self.source_writer: asyncio.StreamWriter | None = None
        self.refresh_deadline: float | None = None
        self.requested_losses: set[str] = set()
        self.ack_deadline: float | None = None

    def save(self) -> None:
        """Persist only changed domain state, always before effects."""
        serialized = json.dumps(self.engine.state, sort_keys=True, allow_nan=False)
        if serialized != self.stored:
            atomic_json(self.root / "state.json", self.engine.state)
            self.stored = serialized

    def submit(self, actions: list) -> None:
        """Start independent effects so slow agent binding cannot delay speech."""
        self.save()
        for action in actions:
            key = (
                action.get("claim")
                or action.get("event", {}).get("event_id")
                or "reconcile"
            )
            if key not in self.effects:
                self.effects[key] = asyncio.create_task(self.effect(key, action))

    async def effect(self, key: str, action: dict) -> None:
        """Route one effect and return a receipt without blocking event intake."""
        try:
            if action["type"] == "publish":
                path = (
                    self.root
                    / "events"
                    / (action["event"]["event_id"].replace(":", "-") + ".json")
                )
                atomic_json(path, action["event"])
                result = await command_json(
                    [
                        BROKER,
                        "publish",
                        "--source",
                        "smart-home",
                        "--event-file",
                        str(path),
                        "--launch-profile",
                        "smart-home",
                    ]
                )
            elif action["type"] == "reconcile":
                result = await command_json(
                    [
                        BROKER,
                        "reconcile",
                        "--source",
                        "smart-home",
                        "--launch-profile",
                        "smart-home",
                    ]
                )
            elif action["type"] == "watch_ack":
                result = await command_json(
                    [
                        BROKER,
                        "watch-ack",
                        "--source",
                        "smart-home",
                        "--event-id",
                        action["event_id"],
                    ],
                    limit_seconds=12,
                )
            else:
                result = await self.announce(action)
            await self.inbox.put(("receipt", (key, action, result)))
        except Exception as error:  # noqa: BLE001 - preserve ambiguous effects as pending; never retry speech.
            LOG.warning("Effect %s failed: %s", action["type"], type(error).__name__)
            await self.inbox.put(
                (
                    "receipt",
                    (
                        key,
                        action,
                        {"status": "pending", "reason": type(error).__name__},
                    ),
                )
            )

    def announcement_current(self, action: dict) -> bool:
        """Recheck cancellations before a remote process can submit any audio."""
        pid = action["player_id"]
        player = self.engine.players.get(pid, {})
        return (
            not self.engine.maintenance
            and time.time() <= action["not_after"]
            and self.engine.intents.get(pid, 0) <= time.time()
            and self.engine.stream
            and self.engine.stream[1] == action["stream_id"]
            and player.get("queue_id") == action["queue_id"]
            and player.get("item_id") == action["item_id"]
            and not (
                player.get("available") is True and player.get("state") == "playing"
            )
        )

    async def announce(self, action: dict) -> dict:
        """Speak only through the exact-target native route, never another room."""
        lock = self.player_locks.setdefault(action["player_id"], asyncio.Lock())
        async with lock:
            if not self.announcement_current(action):
                return {"status": "suppressed", "reason": "canceled_before_submission"}
            return await command_json(
                [*SSH, "python3", REMOTE + "/ha_smart_home_probe.py", "--execute"],
                action,
                35,
            )

    def receipt(self, key: str, action: dict, result: dict) -> None:
        """Record delivery acceptance separately from repair completion."""
        self.effects.pop(key, None)
        if action["type"] == "watch_ack":
            self._ack_receipt(action, result)
        elif action["type"] in {"announce", "resume"}:
            target = "spoken" if action["type"] == "announce" else "resumed"
            self.engine.state[target][key] = {
                "attempted_at": time.time(),
                "player_id": action.get("player_id"),
                **result,
            }
        else:
            self._broker_receipt(key, action, result)
        self.save()

    def _ack_receipt(self, action: dict, result: dict) -> None:
        loss = self.engine.state["subscriber_losses"].get(action["event_id"], {})
        accepted = (
            result.get("status") == "acknowledged"
            and result.get("event_id") == action["event_id"]
            and result.get("source") == "smart-home"
        )
        loss["acknowledged"] = accepted
        if not accepted:
            self.ack_deadline = time.time() + 5

    def _broker_receipt(self, key: str, action: dict, result: dict) -> None:
        for receipt in result.get("receipts", [result]):
            event_id = (
                action["event"]["event_id"]
                if action["type"] == "publish"
                else receipt.get("event_id", key)
            )
            self._store_delivery(event_id, receipt)
        retry = result.get("retry_at")
        try:
            due = (
                datetime.fromisoformat(retry).timestamp() if retry else time.time() + 5
            )
        except (TypeError, ValueError):
            due = time.time() + 5
        self.retry_at = (
            max(time.time() + 1, due) if self.engine.state["outbox"] else None
        )
        if action["type"] == "reconcile" and "receipts" in result:
            # Reconciliation precedes retries of an ambiguous immutable publish.
            self.submit(list(self.engine.state["outbox"].values()))

    def _store_delivery(self, event_id: str, receipt: dict) -> None:
        if durable_delivery(receipt, "smart-home", event_id):
            # The shared spool owns retries after its durable receipt.
            self.engine.state["outbox"].pop(event_id, None)
        atomic_json(
            self.root
            / "receipts"
            / (hashlib.sha256(str(event_id).encode()).hexdigest() + ".json"),
            receipt,
        )

    def reschedule(self) -> None:
        """Schedule one nearest deadline only while concrete work is pending."""
        if self.deadline:
            self.deadline.cancel()
        dates = [
            t
            for t in (
                self.engine.next_deadline(),
                self.retry_at,
                self.refresh_deadline,
                self.ack_deadline,
            )
            if t is not None
        ]
        self.deadline = None
        if dates:
            self.deadline = asyncio.get_running_loop().call_later(
                max(0, min(dates) - time.time()),
                self.inbox.put_nowait,
                ("deadline", None),
            )

    def handle(self, kind: str, data: object) -> None:
        """Process one event or receipt without awaiting slow effects."""
        handler = {
            "event": self._source_event,
            "subscriber_lost": self._subscriber_lost,
            "ready": self._shared_ready,
            "delivery_receipt": self._shared_delivery,
            "receipt": self._effect_receipt,
            "deadline": self._deadline,
        }.get(kind)
        if handler:
            handler(data)
        self.save()
        self.reschedule()

    def _source_event(self, data: dict) -> None:
        self.submit(self.engine.feed(data, time.time()))
        if data.get("kind") == "reconciliation":
            self.submit(self.engine.reconcile_subscriber_loss(data, time.time()))
            self.acknowledge_losses()
            self.requested_losses.discard(data.get("request_id"))
            self.refresh_deadline = (
                None if data.get("status") == "fresh" else time.time() + 5
            )
        elif data.get("kind") == "transport" and data.get("connected"):
            self.request_refresh()

    def _subscriber_lost(self, data: dict) -> None:
        event_id = identity(data.get("event_id"))
        if not (
            event_id
            and data.get("source") == "smart-home"
            and data.get("reason") in LOSS_REASONS
        ):
            return
        self.engine.state["subscriber_losses"].setdefault(
            event_id, {"observed_at": time.time(), "reconciled": False}
        )
        self.save()
        self.acknowledge_losses()
        self.request_refresh()

    def _shared_delivery(self, data: dict) -> None:
        if data.get("source") == "smart-home" and isinstance(data.get("receipt"), dict):
            receipt = data["receipt"]
            self.receipt(
                receipt.get("event_id", "shared"), {"type": "delivery_receipt"}, receipt
            )

    def _effect_receipt(self, data: tuple) -> None:
        self.receipt(*data)

    def _shared_ready(self, data: dict) -> None:
        if (
            data.get("source") != "smart-home"
            or data.get("native_state") != "vacant"
            or not isinstance(data.get("authority_id"), str)
            or not data["authority_id"]
            or not isinstance(data.get("revision"), str)
            or not data["revision"]
        ):
            return
        open_ids = sorted(
            i["id"]
            for i in self.engine.state["incidents"].values()
            if i.get("published") and i.get("closed") is None
        )
        if not open_ids:
            return
        identity_bytes = json.dumps(
            [data["authority_id"], data["revision"], open_ids]
        ).encode()
        event_id = "startup:" + hashlib.sha256(identity_bytes).hexdigest()
        self.engine.state["subscriber_losses"].setdefault(
            event_id,
            {
                "observed_at": time.time(),
                "reconciled": False,
                "native_ack_required": False,
            },
        )
        self.save()
        self.request_refresh()

    def _deadline(self, _data: object) -> None:
        self.submit(self.engine.due(time.time()))
        if self.retry_at is not None and self.retry_at <= time.time():
            self.retry_at = None
            self.submit([{"type": "reconcile"}])
        if self.refresh_deadline is not None and self.refresh_deadline <= time.time():
            self.refresh_deadline = None
            self.requested_losses.clear()
            self.request_refresh()
        if self.ack_deadline is not None and self.ack_deadline <= time.time():
            self.ack_deadline = None
            self.acknowledge_losses()

    def acknowledge_losses(self) -> None:
        """Ack only after domain reconciliation and its new envelopes are durable."""
        self.submit(
            [
                {"type": "watch_ack", "event_id": event_id, "claim": "ack:" + event_id}
                for event_id, loss in self.engine.state["subscriber_losses"].items()
                if loss.get("reconciled")
                and not loss.get("acknowledged")
                and loss.get("native_ack_required", True)
            ]
        )

    def request_refresh(self) -> None:
        """Ask the existing source once for fresh evidence of known orphaned work."""
        if self.source_writer is None or self.source_writer.is_closing():
            return
        for event_id, loss in self.engine.state["subscriber_losses"].items():
            if loss.get("reconciled") or event_id in self.requested_losses:
                continue
            self.source_writer.write(
                json.dumps({"type": "reconcile", "request_id": event_id}).encode()
                + b"\n"
            )
            self.requested_losses.add(event_id)
            self.refresh_deadline = time.time() + 30

    async def run(self) -> None:
        """Resume durable pending deliveries and consume source events forever."""
        # The shared spool can contain pending work even when local state is new.
        self.submit([{"type": "reconcile"}])
        self.acknowledge_losses()
        self.reschedule()
        producer = asyncio.create_task(self.stream())
        watcher = asyncio.create_task(self.watch_subscriber())
        try:
            while True:
                kind, data = await self.inbox.get()
                self.handle(kind, data)
        finally:
            producer.cancel()
            watcher.cancel()
            for task in self.effects.values():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await producer
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    async def watch_subscriber(self) -> None:
        """Consume shared native death events; never poll or own an agent locally."""
        delay = 1
        while True:
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    BROKER,
                    "watch",
                    "--source",
                    "smart-home",
                    "--launch-profile",
                    "smart-home",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=65536,
                )
                async for line in process.stdout:
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get("type") in {
                        "ready",
                        "subscriber_lost",
                        "delivery_receipt",
                    }:
                        await self.inbox.put((event["type"], event))
                    delay = 1
                await process.wait()
            except Exception as error:  # noqa: BLE001 - only supervise the shared observation process.
                LOG.warning(
                    "Subscriber observation unavailable: %s", type(error).__name__
                )
            finally:
                if process and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 2)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def stream(self) -> None:
        """Keep one SSH source; reconnect only after a known broken transport."""
        delay = 1
        local_id = uuid.uuid4().hex
        sequence = 0
        while True:
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *SSH,
                    REMOTE + "/venv/bin/python",
                    REMOTE + "/ha_smart_home_events.py",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=1024 * 1024,
                )
                self.source_writer = process.stdin
                async for line in process.stdout:
                    event = json.loads(line)
                    await self.inbox.put(("event", event))
                    delay = 1
                await process.wait()
                LOG.warning("Event source ended with status %s", process.returncode)
            except Exception as error:  # noqa: BLE001 - reconnect only this supervised source.
                LOG.warning("Event source disconnected: %s", type(error).__name__)
            finally:
                self.source_writer = None
                self.requested_losses.clear()
                self.refresh_deadline = None
                sequence += 1
                await self.inbox.put(
                    (
                        "event",
                        {
                            "kind": "transport",
                            "host": "odroidn2",
                            "boot_id": "consumer",
                            "stream_id": local_id,
                            "sequence": sequence,
                            "observed_at": time.time(),
                            "monotonic": time.monotonic(),
                            "channel": "stream",
                            "connected": False,
                            "reason": "network",
                        },
                    )
                )
                if process and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 3)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)


def main() -> None:
    """Run one externally supervised consumer, without a polling timer."""
    init_log()
    asyncio.run(Runtime(ROOT).run())


if __name__ == "__main__":
    main()
