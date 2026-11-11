# Copyright (c) 2026 Alex Ultra
# SPDX-License-Identifier: MIT
"""Pure, restart-safe alert logic. Does not operate a pump or send notifications."""

from __future__ import annotations

import math
import statistics
from typing import Any

MIN_TRIM_SAMPLES = 5
MAX_MOISTURE = 100

DEFAULTS = {
    "confirm_seconds": 21600,
    "overdue_seconds": 86400,
    "window_seconds": 1800,
    "stale_seconds": 1200,
    "min_samples": 4,
    "recovery_margin": 10,
    "snooze_seconds": 86400,
}


def finite(value: Any) -> float | None:
    """Convert a value to a finite number, rejecting unknown sensor states."""
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def robust_mean(values: list[float]) -> float:
    """Trim the highest/lowest fifth; use median with fewer than five samples."""
    ordered = sorted(values)
    if len(ordered) < MIN_TRIM_SAMPLES:
        return float(statistics.median(ordered))
    cut = max(1, len(ordered) // 5)
    return statistics.mean(ordered[cut:-cut])


def evaluate(  # noqa: C901, PLR0912, PLR0915 - explicit tested state transitions
    previous: dict, reading: dict, now: float, settings: dict | None = None
) -> dict:
    """
    Advance one plant using UTC seconds and an independently timestamped sample.

    The caller persists the returned dictionary via Home Assistant Store.
    Time spent without fresh measurements never satisfies dry confirmation.
    """
    cfg = {**DEFAULTS, **(settings or {})}
    s = dict(previous)
    s["samples"] = list(previous.get("samples", []))
    identity = reading.get("source_identity")
    if "source_identity" in s and s["source_identity"] != identity:
        s.update(
            samples=[],
            dry_since=None,
            due_since=None,
            last_sample_at=None,
            notified_status=None,
        )
    s["source_identity"] = identity
    for key in (
        "dry_since",
        "due_since",
        "snoozed_until",
        "ack_until",
        "last_sample_at",
    ):
        s.setdefault(key, None)
    s.setdefault("samples", [])
    s.setdefault("watered_seen", 0)
    s.setdefault("snoozed_seen", 0)
    s["updated_at"] = now
    s["raw_moisture"] = None
    s["moisture"] = None
    s["sample_count"] = 0
    s["overdue_hours"] = 0
    s["status"] = "ok"

    watered = finite(reading.get("watered_at")) or 0
    if watered > s["watered_seen"] and watered <= now + 60:
        s.update(
            watered_seen=watered,
            dry_since=None,
            due_since=None,
            snoozed_until=None,
            ack_until=watered + cfg["confirm_seconds"],
            notified_status=None,
        )
    snoozed = finite(reading.get("snoozed_at")) or 0
    if snoozed > s["snoozed_seen"] and snoozed <= now + 60:
        s.update(snoozed_seen=snoozed, snoozed_until=snoozed + cfg["snooze_seconds"])
        if s["due_since"] is not None:
            s["due_since"] = s["snoozed_until"]
            s["notified_status"] = None

    if reading.get("has_sensor", True):
        value, stamp = finite(reading.get("moisture")), finite(reading.get("sample_at"))
        invalid = (
            value is None
            or not 0 <= value <= MAX_MOISTURE
            or stamp is None
            or stamp > now + 60
            or now - stamp > cfg["stale_seconds"]
        )
        if invalid:
            if s.get("invalid_since") is None:
                s["invalid_since"] = now
            if s["due_since"] is None and (
                s["last_sample_at"] is None
                or now - s["last_sample_at"] > cfg["stale_seconds"]
            ):
                s["dry_since"] = None
            s["status"] = "sensor_error"
            return s
        if s.get("invalid_since") is not None:
            if s["dry_since"] is not None and s["due_since"] is None:
                s["dry_since"] += max(0, now - s["invalid_since"])
            s["invalid_since"] = None
        last = s.get("last_sample_at")
        if last is not None and stamp - last > cfg["stale_seconds"]:
            s["dry_since"] = None
            s["samples"] = []
        if last is None or stamp > last:
            s["samples"].append([stamp, value])
            s["last_sample_at"] = stamp
        elif stamp == last and s["samples"]:
            s["samples"][-1] = [stamp, value]
        s["samples"] = [
            p for p in s["samples"] if now - cfg["window_seconds"] <= p[0] <= now + 60
        ][-256:]
        s["raw_moisture"] = value
        s["sample_count"] = len(s["samples"])
        if not s["samples"]:
            s["status"] = "sensor_error"
            return s
        avg = robust_mean([p[1] for p in s["samples"]])
        s["moisture"] = round(avg, 1)
        dry = finite(reading.get("dry_threshold"))
        dry = 25 if dry is None else min(100, max(0, dry))
        recovery = min(100, dry + cfg["recovery_margin"])
        s.update(dry_threshold=dry, recovery_threshold=recovery)
        if avg >= recovery:
            if s["due_since"] is None or len(s["samples"]) >= cfg["min_samples"]:
                s.update(dry_since=None, due_since=None, notified_status=None)
        elif avg > dry and s["due_since"] is None:
            s["dry_since"] = None
        elif (avg <= dry or s["due_since"] is not None) and (
            not s["ack_until"] or now >= s["ack_until"]
        ):
            if len(s["samples"]) >= cfg["min_samples"]:
                if s["dry_since"] is None:
                    s["dry_since"] = now
                if (
                    s["due_since"] is None
                    and now - s["dry_since"] >= cfg["confirm_seconds"]
                ):
                    s["due_since"] = max(
                        s["dry_since"] + cfg["confirm_seconds"],
                        s["snoozed_until"] or 0,
                    )
            if s["due_since"] is None:
                s["status"] = "checking"
    else:
        scheduled = finite(reading.get("next_watering_at"))
        if scheduled is not None and scheduled <= now:
            s["due_since"] = max(scheduled, s["snoozed_until"] or 0)
        else:
            s["due_since"] = None

    if s["ack_until"] and now < s["ack_until"]:
        s.update(dry_since=None, due_since=None, status="ok")
    if s["due_since"] is not None:
        elapsed = max(0, now - s["due_since"])
        s["overdue_hours"] = round(elapsed / 3600, 1)
        s["status"] = "overdue" if elapsed >= cfg["overdue_seconds"] else "due"
    if s["snoozed_until"] and now < s["snoozed_until"]:
        s["status"] = "snoozed"
    return s
