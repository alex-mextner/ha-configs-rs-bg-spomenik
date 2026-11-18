"""Infer watering from sustained raw changes, independently of percentage rescaling."""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left
from datetime import date
from itertools import pairwise

from .calibration_policy import ADC_MAX, canonical_digest

BASELINE_SECONDS = 3600
MIN_BASELINE_SECONDS = 2700
MIN_BASELINE_SAMPLES = 10
CONFIRM_SECONDS = 1800
MIN_CONFIRM_SAMPLES = 7
MAX_GAP_SECONDS = 660
MIN_RISE_ADC = 12
NOISE_MULTIPLIER = 6
REARM_SECONDS = 7200
WATERING_METADATA = (
    "precision",
    "window_start",
    "window_end",
    "detected_at",
    "retroactive",
)


def _median(points: list) -> float:
    return float(statistics.median(value for _, value in points))


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * fraction)]


def _uninterrupted(points: list, blocked: list, now: float, gap: float) -> bool:
    return (
        all(0 < value < ADC_MAX for _, value in points)
        and all(0 < b[0] - a[0] <= gap for a, b in pairwise(points))
        and not any(
            start <= points[-1][0] and (end if end is not None else now) >= points[0][0]
            for start, end in blocked
        )
    )


def _baseline(points: list) -> tuple[float, float] | None:
    if (
        len(points) < MIN_BASELINE_SAMPLES
        or points[-1][0] - points[0][0] < MIN_BASELINE_SECONDS
    ):
        return None
    median = _median(points)
    values = [value for _, value in points]
    noise = max(
        statistics.median(abs(value - median) for value in values),
        (_quantile(values, 0.8) - _quantile(values, 0.2)) / 2,
    )
    third = max(1, len(points) // 3)
    # Rising backgrounds must not be reclassified as repeated irrigation.
    if _median(points[-third:]) - _median(points[:third]) > max(3, 2 * noise):
        return None
    return median, max(MIN_RISE_ADC, NOISE_MULTIPLIER * noise)


def _sustained(points: list, baseline: float, threshold: float) -> bool:
    if (
        len(points) < MIN_CONFIRM_SAMPLES
        or points[-1][0] - points[0][0] < CONFIRM_SECONDS
    ):
        return False
    values = [value for _, value in points]
    third = len(points) // 3
    blocks = (points[:third], points[third : 2 * third], points[2 * third :])
    return (
        _median(points) - baseline >= threshold
        and _quantile(values, 0.2) - baseline >= threshold * 0.6
        and all(_median(block) - baseline >= threshold * 0.6 for block in blocks)
    )


def _candidate(
    points: list, timestamps: list, index: int, now: float, settings: dict
) -> tuple[dict | None, bool]:
    first = points[index][0]
    before = points[bisect_left(timestamps, first - BASELINE_SECONDS) : index]
    baseline = _baseline(before)
    if baseline is None:
        return None, False
    level, threshold = baseline
    if points[index][1] - level < threshold * 0.6:
        return None, False
    end = bisect_left(timestamps, first + CONFIRM_SECONDS)
    after = points[index : min(end + 1, len(points))]
    if not _uninterrupted(
        [*before, *after], settings["blocked"], now, settings["max_gap"]
    ):
        return None, False
    if not _sustained(after, level, threshold):
        return None, first + CONFIRM_SECONDS > points[-1][0]
    stable_before = [point for point in before if point[1] <= level + threshold * 0.2]
    start = stable_before[-1][0] if stable_before else before[0][0]
    return {
        "at": first,
        "window_start": start,
        "window_end": first,
        "confirmed_at": after[-1][0],
        "evidence": {
            "baseline_median": level,
            "wet_median": _median(after),
            "required_rise": threshold,
            "baseline_samples": len(before),
            "confirmation_samples": len(after),
        },
    }, False


def _known_event(history: list, at: float) -> bool:
    return any(
        event.get("source") == "auto"
        and isinstance(event.get("at"), (int, float))
        and abs(at - event["at"]) < REARM_SECONDS
        for event in history
    )


def _select_event(result: dict, event: dict) -> None:
    result.update(last_watering_at=event["at"], last_watering_source=event["source"])
    for key in WATERING_METADATA:
        result[f"last_watering_{key}"] = event.get(key)
    result["last_watering_note"] = event.get("note")


def detect_watering(
    result: dict, points: list, now: float, stale_seconds: float
) -> None:
    """Persist deduplicated estimates only after sustained physical observations."""
    _retain_reported_history(result)
    history = [dict(event) for event in result.get("watering_history", [])]
    result["watering_history"] = history
    cutoff = result.get("watering_blocked_until", -math.inf)
    points = [point for point in points if point[0] > cutoff]
    result["watering_detection_status"] = (
        "monitoring" if len(points) >= MIN_BASELINE_SAMPLES else "insufficient_history"
    )
    settings = {
        "blocked": [
            *(result.get("reliability_invalid_intervals") or []),
            *[[stamp, stamp] for stamp in result.get("watering_conflicts", [])],
        ],
        "max_gap": min(stale_seconds, MAX_GAP_SECONDS),
        "stale_seconds": stale_seconds,
    }
    _scan_candidates(result, history, points, now, settings)
    current = [
        event
        for event in history
        if event.get("source") == "auto" and event.get("valid", True)
    ]
    if current:
        _select_event(result, max(current, key=lambda event: event["at"]))


def _scan_candidates(
    result: dict, history: list, points: list, now: float, settings: dict
) -> None:
    timestamps = [point[0] for point in points]
    for index in range(MIN_BASELINE_SAMPLES, len(points)):
        event, confirming = _candidate(points, timestamps, index, now, settings)
        if confirming:
            result["watering_detection_status"] = "confirming_rise"
        if (
            event is None
            or _known_event(history, event["at"])
            or event["at"] in result.get("rejected_watering_events", [])
        ):
            continue
        event.update(
            event_id=canonical_digest(
                ["raw-watering", event["window_start"], event["at"]]
            ),
            source="auto",
            precision="interval",
            detected_at=now,
            retroactive=now - event["confirmed_at"] > settings["stale_seconds"],
            valid=True,
            note=None,
        )
        history.append(event)
        result["watering_detection_status"] = "monitoring"


def invalidate_watering(result: dict, event_at: float, now: float) -> None:
    """Retain an inference's provenance when later evidence makes it untrustworthy."""
    rejected = set(result.get("rejected_watering_events", []))
    rejected.add(event_at)
    result["rejected_watering_events"] = sorted(rejected)
    result["watering_history"] = [
        {**event, "valid": False, "retracted_at": now, "note": "unreliable_evidence"}
        if event.get("source") == "auto" and event.get("at") == event_at
        else dict(event)
        for event in result.get("watering_history", [])
    ]


def retract_untrusted_history(
    result: dict, now: float, unreliable_since: float | None
) -> None:
    """Retract all affected estimates even when a user report is currently latest."""
    intervals = list(result.get("reliability_invalid_intervals") or [])
    intervals.extend([stamp, stamp] for stamp in result.get("watering_conflicts", []))
    if unreliable_since is not None:
        intervals.append([unreliable_since, None])
    for event in list(result.get("watering_history", [])):
        if _untrusted_event(event, intervals, now):
            invalidate_watering(result, event["at"], now)
            _clear_current_estimate(result, event)


def _untrusted_event(event: dict, intervals: list, now: float) -> bool:
    if event.get("source") != "auto" or not event.get("valid", True):
        return False
    start = event.get("window_start", event["at"])
    end = event.get("confirmed_at", event["at"])
    return any(
        left <= end and (right if right is not None else now) >= start
        for left, right in intervals
    )


def _clear_current_estimate(result: dict, event: dict) -> None:
    if (
        result.get("last_watering_source") == "auto"
        and result.get("last_watering_at") == event["at"]
    ):
        result.update(
            untrusted_last_watering_at=event["at"],
            last_watering_at=None,
            last_watering_source="unknown",
        )


def record_report(
    result: dict, report: dict, now: float, *, select: bool = True
) -> None:
    """Keep a user's date and uncertainty; do not fabricate the watering time."""
    _retain_reported_history(result)
    event = {
        "event_id": canonical_digest(["watering-report", report]),
        "at": report["date"],
        "source": "user_report",
        "precision": report.get("precision", "day"),
        "detected_at": now,
        "window_start": None,
        "window_end": None,
        "retroactive": False,
        "note": report.get("note"),
        "timezone": report.get("timezone"),
        "valid": True,
    }
    history = [dict(row) for row in result.get("watering_history", [])]
    existing = next(
        (row for row in history if row["event_id"] == event["event_id"]), None
    )
    if existing is None:
        history.append(event)
    else:
        event = existing
    result["watering_history"] = history
    if select:
        _select_event(result, event)


def _retain_reported_history(result: dict) -> None:
    """Migrate a known reported day without inventing its original recording time."""
    at = result.get("last_watering_at")
    if result.get("last_watering_source") != "user_report" or not isinstance(at, str):
        return
    try:
        date.fromisoformat(at)
    except ValueError:
        return
    history = [dict(event) for event in result.get("watering_history", [])]
    if any(
        event.get("source") == "user_report" and event.get("at") == at
        for event in history
    ):
        return
    history.append(
        {
            "event_id": canonical_digest(["retained-watering-report", at]),
            "at": at,
            "source": "user_report",
            "precision": result.get("last_watering_precision") or "day",
            "detected_at": result.get("last_watering_detected_at"),
            "window_start": None,
            "window_end": None,
            "retroactive": False,
            "note": result.get("last_watering_note"),
            "timezone": None,
            "valid": True,
        }
    )
    result["watering_history"] = history


def pause_watering(result: dict, now: float, reason: str) -> None:
    """Never join physical windows across an observed outage or unreliable reading."""
    result["watering_blocked_until"] = max(result.get("watering_blocked_until", 0), now)
    result["watering_detection_status"] = reason
    result.setdefault("watering_history", [])
