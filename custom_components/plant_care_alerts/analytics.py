"""
Sample-based watering inference for the companion sensor.

Called on recorder bootstrap and fresh ADC reports. Timestamps identify physical
samples, not the instant water was applied; calibration changes cannot create a
raw ADC jump. This module never changes Adaptive Plant's dates.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_MIN_WET_SAMPLES = 3
_MIN_FORECAST_SAMPLES = 12
_MIN_FORECAST_HOURS = 12
_MIN_DRYING_SLOPE = -0.05
_MIN_R_SQUARED = 0.8
_MAX_FORECAST_HOURS = 30 * 24


@dataclass(frozen=True)
class _Settings:
    """Existing configured thresholds shared by the independent estimates."""

    dry_threshold: float
    stale_seconds: float
    wet_hours: float
    wet_percent: float


def _valid_points(samples: Iterable[Any], now: float) -> list[tuple[float, float]]:
    valid = {}
    for point in samples:
        try:
            stamp, value = float(point[0]), float(point[1])
            if math.isfinite(stamp) and math.isfinite(value) and stamp <= now + 60:
                valid.setdefault(stamp, value)
        except (ValueError, TypeError, IndexError):
            continue
    return sorted(valid.items())


def _record_watering(
    result: dict[str, Any],
    points: list[tuple[float, float]],
    span: float,
    stale_seconds: float,
) -> None:
    # Median baseline rejects a single low outlier; three elevated reports
    # reject an isolated spike. No segment may bridge a measurement outage.
    existing = result.get("last_watering_at")
    if not isinstance(existing, (int, float)):
        existing = None
    for i in range(3, len(points) - 2):
        segment = points[i - 3 : i + 3]
        if any(b[0] - a[0] > stale_seconds for a, b in pairwise(segment)):
            continue
        baseline = statistics.median(point[1] for point in segment[:3])
        first = segment[3]
        if (
            segment[2][1] - baseline < span * 0.4
            and first[1] - baseline >= span * 0.5
            and all(point[1] - baseline >= span * 0.4 for point in segment[4:])
            and (existing is None or first[0] > existing)
        ):
            existing = first[0]
            result.update(last_watering_at=existing, last_watering_source="auto")


def _wet_risk(
    points: list[tuple[float, float]], dry: float, span: float, settings: _Settings
) -> dict[str, Any]:
    wet_start = None
    wet_count = 0
    last = None
    for stamp, raw in points:
        if last is not None and stamp - last > settings.stale_seconds:
            wet_start, wet_count = None, 0
        if (raw - dry) * 100 / span >= settings.wet_percent:
            if wet_start is None:
                wet_start = stamp
            wet_count += 1
        else:
            wet_start, wet_count = None, 0
        last = stamp
    if wet_start is None:
        return {"wet_risk": "normal"}
    prolonged = (
        wet_count >= _MIN_WET_SAMPLES
        and points[-1][0] - wet_start >= settings.wet_hours * 3600
    )
    return {
        "wet_since": wet_start,
        "wet_risk": "prolonged" if prolonged else "checking",
    }


def _forecast(
    points: list[tuple[float, float]], dry: float, wet: float, settings: _Settings
) -> tuple[str, float | None]:
    # A clipped index contains no information about the underlying drying rate.
    if points[-1][1] >= wet:
        return "saturated", None
    segment = []
    for point in reversed(points):
        if point[1] >= wet or point[1] <= dry:
            break
        if segment and segment[-1][0] - point[0] > settings.stale_seconds:
            break
        segment.append(point)
    segment.reverse()
    if (
        len(segment) < _MIN_FORECAST_SAMPLES
        or segment[-1][0] - segment[0][0] < _MIN_FORECAST_HOURS * 3600
    ):
        return "insufficient_data", None
    xs = [(p[0] - segment[0][0]) / 3600 for p in segment]
    ys = [(p[1] - dry) * 100 / (wet - dry) for p in segment]
    xm, ym = statistics.mean(xs), statistics.mean(ys)
    xx = sum((x - xm) ** 2 for x in xs)
    yy = sum((y - ym) ** 2 for y in ys)
    slope = sum((x - xm) * (y - ym) for x, y in zip(xs, ys, strict=True)) / xx
    if slope >= _MIN_DRYING_SLOPE:
        return "no_drying_trend", None
    if yy == 0 or slope * slope * xx / yy < _MIN_R_SQUARED:
        return "unstable", None
    remaining = (float(settings.dry_threshold) - ys[-1]) / slope
    if remaining <= 0 or remaining > _MAX_FORECAST_HOURS:
        return "no_drying_trend", None
    return "available", points[-1][0] + remaining * 3600


def analyze(  # noqa: PLR0913 - preserve the deployed keyword configuration API
    previous: Mapping[str, Any],
    samples: Iterable[Any],
    now: float,
    *,
    calibration: tuple[float, float] | list[float] | None = None,
    dry_threshold: float = 25,
    stale_seconds: float = 1200,
    wet_hours: float = 72,
    wet_percent: float = 90,
) -> dict[str, Any]:
    """Return estimates from timestamped independent measurements only."""
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
    dry, wet = calibration
    _record_watering(result, points, wet - dry, stale_seconds)
    if now - points[-1][0] > stale_seconds:
        result["forecast_status"] = "stale"
        return result
    settings = _Settings(dry_threshold, stale_seconds, wet_hours, wet_percent)
    result.update(_wet_risk(points, dry, wet - dry, settings))
    status, due = _forecast(points, dry, wet, settings)
    result.update(forecast_status=status, forecast_due_at=due)
    return result
