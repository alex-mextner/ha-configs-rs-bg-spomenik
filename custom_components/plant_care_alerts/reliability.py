"""Persistent evidence of numeric changes, separate from received packet times."""

from __future__ import annotations

import math
from typing import Any

MAX_INDEX = 100
UNCHANGED_SECONDS = 12 * 3600
FUTURE_TOLERANCE = 60
RETENTION_SECONDS = 14 * 86400
RECOVERY_SECONDS = 600
RECOVERY_SAMPLES = 3
VARIED_VALUES = 2
FROZEN_CAUSES = ("unchanged", "saturated", "lower_saturation", "raw_unchanged")
KEYS = (
    "reliability_valid",
    "reliability_reason",
    "reliability_incident_started_at",
    "reliability_incident_reason",
    "reliability_incident_value",
    "reliability_incident_raw",
    "reliability_incident_value_changed_at",
    "last_value_change_at",
    "last_observed_value",
    "raw_source_identity",
    "last_source_change_at",
    "last_source_value",
    "reliability_last_sample_at",
    "stagnant_seconds",
    "recovery_started_at",
    "recovery_sample_at",
    "recovery_sample_count",
    "recovery_values",
    "recovery_raw_values",
    "reliability_recovered_at",
    "reliability_invalid_intervals",
    "reliability_calibration_basis",
    "calibration_seen_revision",
    "calibration_rebase_pending",
)


def number(value: Any) -> float | None:
    """Return a finite numeric measurement, excluding Boolean values."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def history_clock(points: list, now: float, *, maximum: float | None = None) -> tuple:
    """Ignore repeated/restored states and outages without inventing a new change."""
    valid = {}
    for point in points:
        try:
            stamp, value = number(point[0]), number(point[1])
        except (TypeError, IndexError):
            continue
        if (
            stamp is not None
            and value is not None
            and stamp <= now
            and (maximum is None or 0 <= value <= maximum)
        ):
            valid.setdefault(stamp, value)
    value = changed = None
    for stamp, current in sorted(valid.items()):
        if current != value:
            value, changed = current, stamp
    return value, changed


def _same_source(previous: dict, reading: dict) -> bool:
    return "source_identity" not in previous or previous.get(
        "source_identity"
    ) == reading.get("source_identity")


def _history_matches(previous: dict, reading: dict) -> bool:
    source = previous.get("value_history_source")
    return (
        source == reading.get("source_identity")
        if source is not None
        else _same_source(previous, reading)
    )


def _initial_state(previous: dict, reading: dict, now: float) -> dict:
    same_source = _same_source(previous, reading)
    state = {key: previous.get(key) if same_source else None for key in KEYS}
    if state.get("last_value_change_at") is None and _history_matches(
        previous, reading
    ):
        value, changed = history_clock(
            previous.get("value_history", []), now, maximum=MAX_INDEX
        )
        if changed is not None:
            state.update(last_observed_value=value, last_value_change_at=changed)
    _raw_clock(state, previous, reading, now)
    return state


def _raw_clock(state: dict, previous: dict, reading: dict, now: float) -> None:
    """Keep an absent configured raw channel distinct from an unmapped index."""
    identity = reading.get("raw_source_identity")
    previous_identity = previous.get("raw_source_identity")
    state["raw_source_identity"] = identity
    if identity is None or previous_identity != identity:
        state.update(last_source_value=None, last_source_change_at=None)
    if identity is None:
        return
    if (
        state.get("last_source_change_at") is None
        and _same_source(previous, reading)
        and previous.get("adc_identity") == identity
    ):
        raw, changed = history_clock(previous.get("adc_samples", []), now)
        if changed is not None:
            state.update(last_source_value=raw, last_source_change_at=changed)


def _sample_reason(reading: dict, now: float, stale: float) -> str | None:
    value, stamp = number(reading.get("moisture")), number(reading.get("sample_at"))
    if (
        value is None
        or not 0 <= value <= MAX_INDEX
        or stamp is None
        or stamp > now + FUTURE_TOLERANCE
        or reading.get("source_invalid", False)
    ):
        return "invalid_sample"
    if now - stamp > stale:
        return "stale"
    return None


def _record_sample(state: dict, reading: dict, *, history_missing: bool) -> None:
    value, stamp = number(reading.get("moisture")), number(reading.get("sample_at"))
    raw = (
        number(reading.get("raw_source_value"))
        if state.get("raw_source_identity") is not None
        else None
    )
    if state.get("last_observed_value") != value:
        if state.get("last_observed_value") is not None or not history_missing:
            state["last_value_change_at"] = stamp
        state["last_observed_value"] = value
    if raw is not None and state.get("last_source_value") != raw:
        state.update(last_source_value=raw, last_source_change_at=stamp)
    state["reliability_last_sample_at"] = stamp


def _observe(
    state: dict, reading: dict, now: float, settings: dict, *, history_missing: bool
) -> tuple:
    reason = _sample_reason(reading, now, settings.get("stale_seconds", 1200))
    if reason is not None:
        return reason, False
    stamp = number(reading.get("sample_at"))
    previous_stamp = number(state.get("reliability_last_sample_at"))
    if previous_stamp is not None and stamp < previous_stamp:
        return "invalid_sample", False
    new_sample = previous_stamp is None or stamp > previous_stamp
    if new_sample:
        _record_sample(state, reading, history_missing=history_missing)
    if history_missing and state.get("last_value_change_at") is None:
        reason = "history_unavailable"
    return reason, new_sample


def _index_reason(value: float | None) -> str:
    if value == MAX_INDEX:
        return "saturated"
    return "lower_saturation" if value == 0 else "unchanged"


def _stagnation_reason(state: dict, now: float) -> str | None:
    changed = number(state.get("last_value_change_at"))
    raw_changed = number(state.get("last_source_change_at"))
    frozen = UNCHANGED_SECONDS
    state["stagnant_seconds"] = max(0, now - changed) if changed is not None else None
    if changed is not None and now - changed > frozen:
        return _index_reason(number(state.get("last_observed_value")))
    if (
        state.get("raw_source_identity") is not None
        and number(state.get("last_source_value")) is not None
        and raw_changed is not None
        and now - raw_changed > frozen
    ):
        return "raw_unchanged"
    return None


def _include_frozen_clock(state: dict, now: float) -> None:
    """Retain the earliest independently established freeze, including both clocks."""
    clocks = [number(state.get("last_value_change_at"))]
    if state.get("raw_source_identity") is not None:
        clocks.append(number(state.get("last_source_change_at")))
    since = min(
        (
            stamp
            for stamp in clocks
            if stamp is not None and now - stamp > UNCHANGED_SECONDS
        ),
        default=None,
    )
    previous = number(state.get("reliability_incident_value_changed_at"))
    if since is not None:
        state["reliability_incident_value_changed_at"] = (
            min(previous, since) if previous is not None else since
        )


def _reset_recovery(state: dict) -> None:
    state.update(
        recovery_started_at=None,
        recovery_sample_at=None,
        recovery_sample_count=0,
        recovery_values=[],
        recovery_raw_values=[],
    )


def _remember_fault_values(state: dict, reading: dict) -> None:
    reason = state.get("reliability_incident_reason")
    if reason in FROZEN_CAUSES:
        value, raw = state.get("last_observed_value"), state.get("last_source_value")
    else:
        value, raw = reading.get("moisture"), reading.get("raw_source_value")
    clock = (
        "last_source_change_at" if reason == "raw_unchanged" else "last_value_change_at"
    )
    state.update(
        reliability_incident_value=number(value),
        reliability_incident_raw=number(raw),
        reliability_incident_value_changed_at=number(state.get(clock)),
    )


def _record_invalid_interval(state: dict, reason: str) -> None:
    changed = number(state.get("reliability_incident_value_changed_at"))
    start = (
        changed
        if reason in FROZEN_CAUSES and changed is not None
        else state["reliability_incident_started_at"]
    )
    intervals = state["reliability_invalid_intervals"]
    if intervals and intervals[-1][1] is None:
        intervals[-1][0] = min(intervals[-1][0], start)
    else:
        intervals.append([start, None])


def _latch_fault(state: dict, reading: dict, reason: str, now: float) -> None:
    if number(state.get("reliability_incident_started_at")) is None:
        state.update(
            reliability_incident_started_at=now, reliability_incident_reason=reason
        )
        _remember_fault_values(state, reading)
    # A transport fault can reveal an older frozen value after history loads.
    if (
        reason in FROZEN_CAUSES
        and state.get("reliability_incident_reason") not in FROZEN_CAUSES
    ):
        state["reliability_incident_reason"] = reason
        _remember_fault_values(state, reading)
    if reason in FROZEN_CAUSES:
        _include_frozen_clock(state, now)
    # An absent physical measurement contributes no invalid numeric evidence.
    # Real gaps still break analytics continuity through stale_seconds.
    if not reading.get("measurement_missing", False) or reason in FROZEN_CAUSES:
        _record_invalid_interval(state, reason)
    _reset_recovery(state)
    state.update(reliability_valid=False, reliability_reason=reason)


def _recovery_candidate(state: dict, reading: dict, *, needs_change: bool) -> bool:
    if not needs_change:
        return True
    value = number(reading.get("moisture"))
    raw = number(reading.get("raw_source_value"))
    return (
        0 < value < MAX_INDEX
        and value != state.get("reliability_incident_value")
        and (raw is None or raw != state.get("reliability_incident_raw"))
    )


def _record_recovery(state: dict, reading: dict, settings: dict) -> None:
    stamp = number(reading.get("sample_at"))
    last = number(state.get("recovery_sample_at"))
    if last is None or stamp - last > settings.get("stale_seconds", 1200):
        _reset_recovery(state)
        state["recovery_started_at"] = stamp
    state["recovery_sample_count"] = (state.get("recovery_sample_count") or 0) + 1
    state["recovery_sample_at"] = stamp
    value, raw = (
        number(reading.get("moisture")),
        number(reading.get("raw_source_value")),
    )
    state["recovery_values"] = sorted({*(state.get("recovery_values") or []), value})
    if raw is not None:
        state["recovery_raw_values"] = sorted(
            {*(state.get("recovery_raw_values") or []), raw}
        )


def _recovery_complete(state: dict, reading: dict, *, needs_change: bool) -> bool:
    stamp = number(reading.get("sample_at"))
    varied = not needs_change or (
        len(state.get("recovery_values") or []) >= VARIED_VALUES
        and (
            number(reading.get("raw_source_value")) is None
            or len(state.get("recovery_raw_values") or []) >= VARIED_VALUES
        )
    )
    return (
        (state.get("recovery_sample_count") or 0) >= RECOVERY_SAMPLES
        and stamp - (state.get("recovery_started_at") or stamp) >= RECOVERY_SECONDS
        and varied
    )


def _recover(
    state: dict, reading: dict, now: float, settings: dict, *, new_sample: bool
) -> bool:
    original = state.get("reliability_incident_reason")
    needs_change = original in FROZEN_CAUSES or (
        original == "history_unavailable" and not reading.get("recorder_ready", False)
    )
    if not _recovery_candidate(state, reading, needs_change=needs_change):
        current_reason = (
            original
            if number(reading.get("moisture"))
            == state.get("reliability_incident_value")
            else "recovering"
        )
        state.update(reliability_valid=False, reliability_reason=current_reason)
        _reset_recovery(state)
        return False
    if new_sample:
        _record_recovery(state, reading, settings)
    if not _recovery_complete(state, reading, needs_change=needs_change):
        state.update(reliability_valid=False, reliability_reason="recovering")
        return False
    intervals = state["reliability_invalid_intervals"]
    if intervals and intervals[-1][1] is None:
        intervals[-1][1] = now
    state.update(
        reliability_incident_started_at=None,
        reliability_incident_reason=None,
        reliability_recovered_at=now,
        recovery_sample_count=0,
        recovery_values=[],
        recovery_raw_values=[],
    )
    return True


def _startup_absence(state: dict, reading: dict, stagnation: str | None) -> bool:
    return (
        reading.get("starting", False)
        and number(reading.get("moisture")) is None
        and stagnation is None
        and state.get("reliability_incident_started_at") is None
    )


CALIBRATION_ENDPOINTS = 2


def _calibration_basis(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != CALIBRATION_ENDPOINTS:
        return None
    dry, wet = (number(item) for item in value)
    return [dry, wet] if dry is not None and wet is not None and wet > dry else None


def _calibration_reading(state: dict, reading: dict, now: float) -> dict:
    """Keep an old fault's scale until independently qualified evidence replaces it."""
    current = _calibration_basis(reading.get("calibration_current"))
    if current is None:
        return reading
    basis = (
        _calibration_basis(state.get("reliability_calibration_basis"))
        or _calibration_basis(reading.get("calibration_previous"))
        or current
    )
    revision = reading.get("calibration_revision")
    if state.get("calibration_seen_revision") != revision:
        state["calibration_seen_revision"] = revision
        state["calibration_rebase_pending"] = (
            number(reading.get("calibration_effective_at")) is not None
        )
    barrier = number(reading.get("calibration_barrier_until")) or 0
    frozen = state.get("reliability_incident_reason") in FROZEN_CAUSES
    verified = (
        revision is not None
        and reading.get("calibration_verified_revision") == revision
    )
    if now >= barrier and (not frozen or verified):
        if state.get("calibration_rebase_pending") and number(
            reading.get("raw_source_value")
        ) == number(state.get("last_source_value")):
            # The same physical value in another scale is not a numeric change.
            state["last_observed_value"] = number(reading.get("moisture"))
        state["calibration_rebase_pending"] = False
        basis = current
    state["reliability_calibration_basis"] = basis
    raw = number(reading.get("raw_source_value"))
    if frozen and basis != current and raw is not None:
        value = max(0.0, min(MAX_INDEX, 100 * (raw - basis[0]) / (basis[1] - basis[0])))
        return {**reading, "moisture": value}
    return reading


def _observe_barrier_raw(
    state: dict, reading: dict, now: float, settings: dict
) -> str | None:
    stamp, raw = (
        number(reading.get("sample_at")),
        number(reading.get("raw_source_value")),
    )
    last = number(state.get("reliability_last_sample_at"))
    if (
        stamp is None
        or raw is None
        or stamp > now + FUTURE_TOLERANCE
        or (last is not None and stamp < last)
    ):
        return "invalid_sample"
    if now - stamp > settings.get("stale_seconds", 1200):
        return "stale"
    if last is None or stamp > last:
        if state.get("last_source_value") != raw:
            state.update(last_source_value=raw, last_source_change_at=stamp)
        state["reliability_last_sample_at"] = stamp
    return None


def _calibration_barrier(
    state: dict, reading: dict, now: float, settings: dict
) -> bool:
    """Pause the derived smoothing window without opening a calibration incident."""
    until = number(reading.get("calibration_barrier_until"))
    if until is None or now >= until:
        return False
    reason = _observe_barrier_raw(state, reading, now, settings)
    reason = _stagnation_reason(state, now) or reason
    state["reliability_invalid_intervals"] = [
        list(pair) for pair in state.get("reliability_invalid_intervals") or []
    ]
    if reason is not None:
        _latch_fault(state, reading, reason, now)
    else:
        _reset_recovery(state)
        state.update(
            reliability_valid=False,
            reliability_reason=state.get("reliability_incident_reason")
            or "calibrating",
        )
    return True


def assess(previous: dict, reading: dict, now: float, settings: dict) -> dict:
    """Latch a fault until independent healthy samples demonstrate recovery."""
    state = _initial_state(previous, reading, now)
    reading = _calibration_reading(state, reading, now)
    if _calibration_barrier(state, reading, now, settings):
        return state
    history_missing = (
        reading.get("history_ready") is False or not _history_matches(previous, reading)
    ) and state.get("last_value_change_at") is None
    reason, new_sample = _observe(
        state, reading, now, settings, history_missing=history_missing
    )
    stagnation = _stagnation_reason(state, now)
    # A missing packet cannot mask independently established frozen evidence.
    reason = stagnation or reason
    if _startup_absence(state, reading, stagnation):
        state.update(reliability_valid=False, reliability_reason="starting")
        return state
    state["reliability_invalid_intervals"] = [
        list(pair)
        for pair in state.get("reliability_invalid_intervals") or []
        if pair[1] is None or pair[1] >= now - RETENTION_SECONDS
    ]
    if reason is not None:
        _latch_fault(state, reading, reason, now)
        return state
    if number(
        state.get("reliability_incident_started_at")
    ) is not None and not _recover(
        state, reading, now, settings, new_sample=new_sample
    ):
        return state
    state.update(reliability_valid=True, reliability_reason=None)
    return state
