#!/usr/bin/env python3
# Copyright (c) 2026 Alex Mextner
"""Shared agent subscription adapter for the existing plant recovery policy."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ha_smart_home_common import atomic_json, durable_delivery

SOURCE = "plant-sensors"
BROKER = str(Path.home() / ".local/bin/agent-sub")
LOSS_REASONS = {
    "process_exited",
    "subscription_released",
    "authority_vacant_after_restart",
}


class LocalBackend(Protocol):
    """Keep the existing sensor operations and legacy process reconciliation."""

    root: Path
    remote_host: str

    def observe(self) -> dict:
        """Read the established reliability contract."""
        ...

    def refresh(self) -> dict:
        """Request the existing bounded physical refresh."""
        ...

    def restart_bridge(self) -> dict:
        """Restart only the dedicated bridge under the old domain policy."""
        ...

    def reconcile(self, incident: dict) -> dict:
        """Reconcile a worker started by a pre-migration version."""
        ...


class SharedBackend:
    """Keep deterministic sensor operations in their existing implementation."""

    def __init__(self, local: LocalBackend) -> None:
        """Delegate domain effects without creating a second ownership registry."""
        self.local = local

    def observe(self) -> dict:
        """Keep the sensor observation policy unchanged."""
        return self.local.observe()

    def refresh(self) -> dict:
        """Keep the existing deterministic refresh unchanged."""
        return self.local.refresh()

    def restart_bridge(self) -> dict:
        """Keep the existing dedicated bridge recovery unchanged."""
        return self.local.restart_bridge()

    def event_path(self, incident: dict) -> Path:
        """Resolve only an existing domain's bounded incident identity."""
        identity = incident.get("id")
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{20}", identity):
            message = "invalid_plant_incident"
            raise ValueError(message)
        revision = incident.get("subscription_revision")
        suffix = (
            "-" + hashlib.sha256(revision.encode()).hexdigest()[:20] if revision else ""
        )
        return (
            self.local.root
            / "incidents"
            / identity
            / ("subscription-event" + suffix + ".json")
        )

    def apply_loss(self, incident: dict) -> None:
        """Consume only shared positive-death evidence matching this delivery."""
        path = self.local.root / "subscriber-loss.json"
        if not path.exists():
            return
        loss = json.loads(path.read_text())
        receipt = incident.get("launch_receipt", {})
        if (
            loss.get("source") == SOURCE
            and loss.get("reason") in LOSS_REASONS
            and loss.get("subscriber_id")
            and loss.get("subscriber_id") == receipt.get("subscriber_id")
            and isinstance(loss.get("event_id"), str)
        ):
            incident["subscription_revision"] = loss["event_id"]
            incident["subscription_native_ack"] = loss.get("native_ack_required", True)

    def launch(self, incident: dict, observation: dict) -> dict:
        """Publish once to the shared native lifecycle, preserving immutable retries."""
        self.apply_loss(incident)
        path = self.event_path(incident)
        if not path.exists():
            legacy = (
                self.local.reconcile(incident)
                if not incident.get("subscription_revision")
                else {"status": "not_started"}
            )
            # Preserve an active or ambiguous old worker, and explicit cancellation.
            # Only authoritative absence permits migration to the shared source.
            if legacy.get("status") not in {"not_started", "resume_available"}:
                return legacy
            event = {
                "event_id": "plant:"
                + incident["id"]
                + ":"
                + hashlib.sha256(path.name.encode()).hexdigest()[:20],
                "source": SOURCE,
                "incident_id": incident["id"],
                "phase": "updated"
                if incident.get("subscription_revision")
                else "opened",
                "observed_at": datetime.fromtimestamp(
                    incident["started_at"], UTC
                ).isoformat(),
                "kind": "plant_sensor_measurement",
                "payload": {
                    "host": self.local.remote_host,
                    "started_at": incident["started_at"],
                    "evidence": observation,
                    "attempted_actions": incident.get("actions", []),
                    "current_state": str(self.local.root / "state.json"),
                    "report_path": str(path.parent / "report.md"),
                    "subscriber_loss": incident.get("subscription_revision"),
                },
            }
            atomic_json(path, event)
        event = json.loads(path.read_text())
        receipt_path = path.with_suffix(".receipt.json")
        prior = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        argv = (
            [BROKER, "status", "--source", SOURCE, "--event-id", event["event_id"]]
            if durable_delivery(prior, SOURCE, event["event_id"])
            else [
                BROKER,
                "publish",
                "--source",
                SOURCE,
                "--event-file",
                str(path),
                "--launch-profile",
                SOURCE,
            ]
        )
        result = subprocess.run(  # noqa: S603 - fixed trusted broker and scoped arguments.
            argv,
            text=True,
            capture_output=True,
            check=True,
            timeout=110,
        )
        receipt = json.loads(result.stdout)
        if not isinstance(receipt, dict):
            message = "invalid_subscription_receipt"
            raise TypeError(message)
        native_status = receipt.get("status")
        if durable_delivery(receipt, SOURCE, event["event_id"]):
            atomic_json(receipt_path, receipt)
            if incident.get("subscription_revision") and incident.get(
                "subscription_native_ack", True
            ):
                acknowledge_loss(incident["subscription_revision"])
        return {
            **receipt,
            "subscription_status": native_status,
            "status": "started" if native_status == "delivered" else "pending",
        }

    def reconcile(self, incident: dict) -> dict:
        """Retrieve the same immutable native receipt after an ambiguous response."""
        return self.launch(incident, incident.get("evidence", {}))

    def acknowledge_closed(self, result: dict) -> None:
        """Acknowledge loss without a new launch when fresh domain health permits."""
        path = self.local.root / "subscriber-loss.json"
        if result.get("status") == "healthy" and path.exists():
            event = json.loads(path.read_text())
            if (
                event.get("source") == SOURCE
                and isinstance(event.get("event_id"), str)
                and event.get("native_ack_required", True)
            ):
                acknowledge_loss(event["event_id"])


def startup_loss(event: dict, root: Path) -> dict | None:
    """Reconcile pre-watch shared work only after authoritative startup vacancy."""
    if (
        event.get("native_state") != "vacant"
        or event.get("source") != SOURCE
        or not isinstance(event.get("authority_id"), str)
        or not event["authority_id"]
        or not isinstance(event.get("revision"), str)
        or not event["revision"]
    ):
        return None
    try:
        state = json.loads((root / "state.json").read_text())
        incident = state.get("incidents", {}).get(state.get("active"), {})
        receipt = incident.get("launch_receipt", {})
    except (OSError, ValueError, AttributeError):
        return None
    if (
        receipt.get("source") != SOURCE
        or receipt.get("durable") is not True
        or receipt.get("subscription_status") != "delivered"
        or not receipt.get("subscriber_id")
    ):
        return None
    value = json.dumps(
        [event["authority_id"], event["revision"], incident.get("id")]
    ).encode()
    return {
        "type": "subscriber_lost",
        "source": SOURCE,
        "reason": "authority_vacant_after_restart",
        "subscriber_id": receipt["subscriber_id"],
        "event_id": "startup:" + hashlib.sha256(value).hexdigest(),
        "native_ack_required": False,
    }


def acknowledge_loss(event_id: str) -> None:
    """Keep an ambiguous ack retryable; never turn it into a sensor failure."""
    try:
        response = subprocess.run(  # noqa: S603 - fixed shared acknowledgement command.
            [BROKER, "watch-ack", "--source", SOURCE, "--event-id", event_id],
            check=True,
            capture_output=True,
            text=True,
            timeout=12,
        )
        receipt = json.loads(response.stdout)
        if receipt.get("status") != "acknowledged":
            logging.getLogger(__name__).warning("Subscriber acknowledgement deferred")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        logging.getLogger(__name__).warning(
            "Subscriber acknowledgement pending: %s", type(error).__name__
        )


def watch() -> None:
    """React to shared subscriber death using a fresh ordinary plant observation."""
    from ha_plant_sensor_recovery import (  # noqa: PLC0415 - avoids a circular host/probe dependency.
        STATE_ROOT,
        Controller,
        SystemBackend,
        configure_logging,
    )

    configure_logging()
    local = SystemBackend(STATE_ROOT)
    backend = SharedBackend(local)
    with subprocess.Popen(  # noqa: S603 - fixed trusted event subscription, no shell.
        [BROKER, "watch", "--source", SOURCE, "--launch-profile", SOURCE],
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        for line in process.stdout:
            event = json.loads(line)
            if event.get("type") == "ready":
                event = startup_loss(event, STATE_ROOT) or {}
            if (
                event.get("type") != "subscriber_lost"
                or event.get("source") != SOURCE
                or event.get("reason") not in LOSS_REASONS
            ):
                continue
            atomic_json(STATE_ROOT / "subscriber-loss.json", event)
            # This is a read-only source observation, not a fresh ADC measurement.
            # The existing controller retains every threshold and remedy count.
            try:
                result = Controller(STATE_ROOT / "state.json", backend).tick(
                    local.observe(), time.time()
                )
                backend.acknowledge_closed(result)
                logging.getLogger(__name__).info(
                    "Subscriber loss reconciled: %s", result.get("status")
                )
            except (
                OSError,
                ValueError,
                TypeError,
                subprocess.SubprocessError,
            ) as error:
                logging.getLogger(__name__).warning(
                    "Subscriber loss pending: %s", type(error).__name__
                )
                # The existing plant timer will consume the durable loss on its next
                # authorized observation; no new scan or agent ownership loop exists.
        message = "shared_subscription_watch_ended"
        raise RuntimeError(message)


if __name__ == "__main__":
    watch()
