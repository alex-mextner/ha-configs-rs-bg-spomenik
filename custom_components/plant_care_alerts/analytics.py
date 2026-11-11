"""Sample-based watering inference for the companion sensor.

Called on recorder bootstrap and fresh ADC reports. Timestamps identify physical
samples, not the instant water was applied; calibration changes cannot create a
raw ADC jump. This module never changes Adaptive Plant's dates.
"""
from __future__ import annotations

import math
import statistics


def analyze(previous, samples, now, *, calibration=None, dry_threshold=25,
            stale_seconds=1200, wet_hours=72, wet_percent=90):
    """Return estimates from timestamped independent measurements only."""
    result = dict(previous)
    valid = {}
    for point in samples:
        try:
            stamp, value = float(point[0]), float(point[1])
            if math.isfinite(stamp) and math.isfinite(value) and stamp <= now + 60:
                valid.setdefault(stamp, value)
        except (ValueError, TypeError, IndexError):
            continue
    points = sorted(valid.items())
    result.update(wet_risk="unknown", wet_since=None, forecast_status="insufficient_data",
                  forecast_due_at=None, forecast_voice_at=None)
    if not points:
        result["forecast_status"] = "stale"
        return result
    if not calibration or calibration[1] <= calibration[0]:
        return result
    dry, wet = calibration
    span = wet - dry
    # Median baseline rejects a single low outlier; three elevated reports
    # reject an isolated spike. No segment may bridge a measurement outage.
    existing = result.get("last_watering_at")
    if not isinstance(existing, (int, float)):
        existing = None
    for i in range(3, len(points) - 2):
        segment = points[i - 3:i + 3]
        if any(b[0] - a[0] > stale_seconds for a, b in zip(segment, segment[1:])):
            continue
        baseline = statistics.median(point[1] for point in segment[:3])
        first = segment[3]
        if (segment[2][1] - baseline < span * 0.4
                and first[1] - baseline >= span * 0.5
                and all(point[1] - baseline >= span * 0.4 for point in segment[4:])
                and (existing is None or first[0] > existing)):
            existing = first[0]
            result.update(last_watering_at=existing, last_watering_source="auto")
    if now - points[-1][0] > stale_seconds:
        result["forecast_status"] = "stale"
        return result
    wet_start = None
    wet_count = 0
    last = None
    for stamp, raw in points:
        if last is not None and stamp - last > stale_seconds:
            wet_start, wet_count = None, 0
        if (raw - dry) * 100 / span >= wet_percent:
            if wet_start is None:
                wet_start = stamp
            wet_count += 1
        else:
            wet_start, wet_count = None, 0
        last = stamp
    if wet_start is None:
        result["wet_risk"] = "normal"
    else:
        result["wet_since"] = wet_start
        result["wet_risk"] = ("prolonged" if wet_count >= 3 and
                              points[-1][0] - wet_start >= wet_hours * 3600 else "checking")
    # A clipped index contains no information about the underlying drying rate.
    if points[-1][1] >= wet:
        result["forecast_status"] = "saturated"
        return result
    segment = []
    for point in reversed(points):
        if point[1] >= wet or point[1] <= dry:
            break
        if segment and segment[-1][0] - point[0] > stale_seconds:
            break
        segment.append(point)
    segment.reverse()
    if len(segment) < 12 or segment[-1][0] - segment[0][0] < 12 * 3600:
        return result
    xs = [(p[0] - segment[0][0]) / 3600 for p in segment]
    ys = [(p[1] - dry) * 100 / span for p in segment]
    xm, ym = statistics.mean(xs), statistics.mean(ys)
    xx = sum((x - xm) ** 2 for x in xs)
    yy = sum((y - ym) ** 2 for y in ys)
    slope = sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / xx
    if slope >= -0.05:
        result["forecast_status"] = "no_drying_trend"
        return result
    if yy == 0 or slope * slope * xx / yy < 0.8:
        result["forecast_status"] = "unstable"
        return result
    remaining = (float(dry_threshold) - ys[-1]) / slope
    if remaining <= 0 or remaining > 30 * 24:
        result["forecast_status"] = "no_drying_trend"
        return result
    due = points[-1][0] + remaining * 3600
    result.update(forecast_status="available", forecast_due_at=due)
    return result
