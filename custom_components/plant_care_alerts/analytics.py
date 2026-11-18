"""Infer watering only from independent, trustworthy raw measurements."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

BASELINE_SAMPLES = 3
EVENT_SAMPLES = 6
RETENTION_SECONDS = 14 * 86400
FUTURE_TOLERANCE = 60
FORECAST_SAMPLES = 12
FORECAST_SECONDS = 12 * 3600
MIN_DRYING_SLOPE = -0.05
MIN_FIT = 0.8
MAX_FORECAST_HOURS = 30 * 24


@dataclass(frozen=True)
class AnalysisSettings:
    """Calibrated inference limits and the current evidence validity."""

    calibration: list | None = None
    dry_threshold: float = 25
    stale_seconds: float = 1200
    wet_hours: float = 72
    wet_percent: float = 90
    reliable: bool = True
    unreliable_since: float | None = None


def _valid_points(samples: list, now: float) -> list:
    valid = {}
    for point in samples:
        try:
            stamp, value = float(point[0]), float(point[1])
            if (
                math.isfinite(stamp)
                and math.isfinite(value)
                and stamp <= now + FUTURE_TOLERANCE
            ):
                valid.setdefault(stamp, value)
        except (ValueError, TypeError, IndexError):
            continue
    return sorted(valid.items())


def _quarantined(segment: list, intervals: list, now: float) -> bool:
    return any(
        start <= stamp <= (end if end is not None else now)
        for stamp, _ in segment
        for start, end in intervals
    )


def _event_segment(points: list, event: float) -> list:
    index = next((i for i, point in enumerate(points) if point[0] == event), None)
    if index is None or index < BASELINE_SAMPLES or index + 2 >= len(points):
        return []
    return points[index - BASELINE_SAMPLES : index + BASELINE_SAMPLES]


def _retract_automatic(
    result: dict,
    points: list,
    wet: float,
    now: float,
    *,
    unreliable_since: float | None,
) -> None:
    rejected = {
        value
        for value in result.get("rejected_watering_events", [])
        if isinstance(value, (int, float))
        and math.isfinite(value)
        and now - RETENTION_SECONDS <= value <= now
    }
    result["rejected_watering_events"] = sorted(rejected)
    event = result.get("last_watering_at")
    if result.get("last_watering_source") != "auto" or not isinstance(
        event, (int, float)
    ):
        return
    unsupported = event in rejected or (
        unreliable_since is not None and event >= unreliable_since
    )
    segment = _event_segment(points, event)
    if segment:
        unsupported = unsupported or any(
            point[1] >= wet for point in segment[:BASELINE_SAMPLES]
        )
        unsupported = unsupported or _quarantined(
            segment, result.get("reliability_invalid_intervals") or [], now
        )
    if unsupported:
        rejected.add(event)
        result.update(
            rejected_watering_events=sorted(rejected),
            untrusted_last_watering_at=event,
            last_watering_at=None,
            last_watering_source="unknown",
        )


def _watering_candidate(segment: list, calibration: list) -> bool:
    dry, wet = calibration
    span = wet - dry
    baseline = statistics.median(point[1] for point in segment[:BASELINE_SAMPLES])
    return (
        all(point[1] < wet for point in segment[:BASELINE_SAMPLES])
        and segment[2][1] - baseline < span * 0.4
        and segment[3][1] - baseline >= span * 0.5
        and all(point[1] - baseline >= span * 0.4 for point in segment[4:])
    )


def _detect_watering(
    result: dict, points: list, calibration: list, now: float, *, stale_seconds: float
) -> None:
    existing = result.get("last_watering_at")
    if not isinstance(existing, (int, float)):
        existing = None
    intervals = result.get("reliability_invalid_intervals") or []
    rejected = result.get("rejected_watering_events", [])
    for index in range(BASELINE_SAMPLES, len(points) - 2):
        segment = points[index - BASELINE_SAMPLES : index + BASELINE_SAMPLES]
        if _quarantined(segment, intervals, now):
            continue
        if any(b[0] - a[0] > stale_seconds for a, b in pairwise(segment)):
            continue
        event = segment[BASELINE_SAMPLES][0]
        if (
            event not in rejected
            and _watering_candidate(segment, calibration)
            and (existing is None or event > existing)
        ):
            existing = event
            result.update(last_watering_at=event, last_watering_source="auto")


def _trusted_tail(points: list, intervals: list, now: float) -> list:
    # A fault breaks continuity even if no raw sample falls inside its interval.
    cutoff = max(
        (end if end is not None else now for _, end in intervals), default=-math.inf
    )
    return [point for point in points if point[0] > cutoff]


def _wet_conditions(result: dict, points: list, settings: AnalysisSettings) -> None:
    calibration = settings.calibration
    wet_hours, wet_percent, stale_seconds = (
        settings.wet_hours,
        settings.wet_percent,
        settings.stale_seconds,
    )
    dry, wet = calibration
    wet_start = last = None
    wet_count = 0
    for stamp, raw in points:
        if last is not None and stamp - last > stale_seconds:
            wet_start, wet_count = None, 0
        if (raw - dry) * 100 / (wet - dry) >= wet_percent:
            if wet_start is None:
                wet_start = stamp
            wet_count += 1
        else:
            wet_start, wet_count = None, 0
        last = stamp
    if wet_start is None:
        result["wet_risk"] = "normal"
        return
    result["wet_since"] = wet_start
    result["wet_risk"] = (
        "prolonged"
        if wet_count >= BASELINE_SAMPLES
        and points[-1][0] - wet_start >= wet_hours * 3600
        else "checking"
    )


def _dry_segment(points: list, calibration: list, stale_seconds: float) -> list:
    dry, wet = calibration
    segment = []
    for point in reversed(points):
        if point[1] >= wet or point[1] <= dry:
            break
        if segment and segment[-1][0] - point[0] > stale_seconds:
            break
        segment.append(point)
    segment.reverse()
    return segment


def _forecast(
    result: dict,
    points: list,
    calibration: list,
    dry_threshold: float,
    *,
    stale_seconds: float,
) -> None:
    dry, wet = calibration
    segment = _dry_segment(points, calibration, stale_seconds)
    if (
        len(segment) < FORECAST_SAMPLES
        or segment[-1][0] - segment[0][0] < FORECAST_SECONDS
    ):
        return
    xs = [(p[0] - segment[0][0]) / 3600 for p in segment]
    ys = [(p[1] - dry) * 100 / (wet - dry) for p in segment]
    xm, ym = statistics.mean(xs), statistics.mean(ys)
    xx = sum((x - xm) ** 2 for x in xs)
    yy = sum((y - ym) ** 2 for y in ys)
    slope = sum((x - xm) * (y - ym) for x, y in zip(xs, ys, strict=True)) / xx
    if slope >= MIN_DRYING_SLOPE:
        result["forecast_status"] = "no_drying_trend"
        return
    if yy == 0 or slope * slope * xx / yy < MIN_FIT:
        result["forecast_status"] = "unstable"
        return
    remaining = (float(dry_threshold) - ys[-1]) / slope
    if remaining <= 0 or remaining > MAX_FORECAST_HOURS:
        result["forecast_status"] = "no_drying_trend"
        return
    result.update(
        forecast_status="available", forecast_due_at=points[-1][0] + remaining * 3600
    )


def _analyze_trend(
    result: dict, points: list, settings: AnalysisSettings, now: float
) -> None:
    points = _trusted_tail(
        points, result.get("reliability_invalid_intervals") or [], now
    )
    if not points:
        return
    _wet_conditions(result, points, settings)
    if points[-1][1] >= settings.calibration[1]:
        result["forecast_status"] = "saturated"
        return
    _forecast(
        result,
        points,
        settings.calibration,
        settings.dry_threshold,
        stale_seconds=settings.stale_seconds,
    )


def analyze(previous: dict, samples: list, now: float, **options: Any) -> dict:
    """Return estimates using the keyword options defined by AnalysisSettings."""
    settings = AnalysisSettings(**options)
    calibration, stale_seconds = settings.calibration, settings.stale_seconds
    result = dict(previous)
    points = _valid_points(samples, now)
    result.update(
        wet_risk="unknown",
        wet_since=None,
        forecast_status="insufficient_data",
        forecast_due_at=None,
        forecast_voice_at=None,
    )
    if not points:
        result["forecast_status"] = "stale"
        return result
    if not calibration or calibration[1] <= calibration[0]:
        return result
    _retract_automatic(
        result, points, calibration[1], now, unreliable_since=settings.unreliable_since
    )
    if not settings.reliable:
        result["forecast_status"] = "invalid_readings"
        return result
    if now - points[-1][0] > stale_seconds:
        result["forecast_status"] = "stale"
        return result
    _detect_watering(result, points, calibration, now, stale_seconds=stale_seconds)
    _analyze_trend(result, points, settings, now)
    return result
