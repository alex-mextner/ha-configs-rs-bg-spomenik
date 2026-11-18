"""Pure deterministic wet-endpoint calibration; runtime owns all writes."""

import hashlib
import json
import math
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from statistics import median
from typing import Literal, TypedDict


@dataclass(frozen=True)
class PhysicalSample:
    """One physical ADC burst, regardless of its number of ADC reads."""

    sampled_at: float
    raw: float
    burst: tuple[float, ...] | list[float]
    source_fingerprint: str
    reliable: bool = True


@dataclass(frozen=True)
class QualifiedWetReference:
    """A reference qualified by the runtime's configured independent source."""

    reference_id: str
    cycle_id: str
    start_at: float
    end_at: float
    source_fingerprint: str
    reference_source: str
    protocol: str
    reference_index: float


@dataclass(frozen=True)
class CalibrationState:
    """Immutable baseline and the persisted state of prior accepted changes."""

    original_dry: float
    original_wet: float
    current_wet: float
    revision: int
    last_applied_at: float | None
    last_evidence_id: str | None
    previous_wet: float | None = None
    source_fingerprint: str | None = None
    baseline_source_fingerprint: str | None = None
    blocked_evidence_ids: tuple[str, ...] | list[str] = ()
    current_dry: float | None = None
    wet_step: float = 1.0


class CalibrationDiagnostics(TypedDict, total=False):
    """Optional diagnostic fields; each decision includes only available facts."""

    qualified_cycles: int
    rejected_references: dict[str, str]
    block_evidence_ids: tuple[str, ...]
    holdout_error_before: float
    holdout_error_after: float
    target_wet: float


@dataclass(frozen=True)
class CalibrationDecision:
    """A recommendation only; observe never contains a writable endpoint."""

    status: Literal["observe", "propose", "rollback"]
    reason: str
    proposed_wet: float | None
    evidence_id: str | None
    reference_ids: tuple[str, ...]
    diagnostics: CalibrationDiagnostics


# All drift bounds use the immutable original span, never the last fitted span.
ADC_MAX = 4095
INDEX_MAX = 100
MIN_BURST_READS = 5
MAX_BURST_READS = 64
MIN_TRAINING_CYCLES = 3
MIN_TOTAL_CYCLES = 4
DAY = 86400.0
NOMINAL_SAMPLE_SECONDS = 300.0
# Allow 10% scheduling jitter, but never two near-boundary copies as two votes.
MIN_VOTE_SPACING = 270.0
MAX_TOTAL_DRIFT_FRACTION = 0.05
MAX_STEP_FRACTION = 0.01
WINDOW_SPREAD_FRACTION = 0.05
MIN_WINDOW_SECONDS = 3600.0
MAX_WINDOW_SECONDS = 7200.0
MIN_VOTES = 12
MAX_GAP_SECONDS = 600.0
TRAINING_SPAN_SECONDS = 14 * DAY
COOLDOWN_SECONDS = 7 * DAY
HISTORY_SECONDS = 90 * DAY
HOLDOUT_MAX_AGE = DAY
CURRENT_MAX_AGE = 1200.0
MAX_SAMPLES = 30000
MAX_REFERENCES = 256
EPSILON = 1e-9


@dataclass(frozen=True)
class _Window:
    reference: QualifiedWetReference
    median_raw: float
    q25_raw: float
    q75_raw: float
    sample_digest: str


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _quantile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _observe(
    reason: str, diagnostics: CalibrationDiagnostics | None = None
) -> CalibrationDecision:
    return CalibrationDecision("observe", reason, None, None, (), diagnostics or {})


def _within_envelope(value: float, state: CalibrationState) -> bool:
    span = state.original_wet - state.original_dry
    return (
        state.original_dry < value <= ADC_MAX
        and abs(value - state.original_wet) <= MAX_TOTAL_DRIFT_FRACTION * span + EPSILON
    )


def _valid_applied_clock(state: CalibrationState, now: float) -> bool:
    if state.last_applied_at is None:
        return (
            state.revision == 0
            and state.previous_wet is None
            and state.last_evidence_id is None
        )
    return _finite(state.last_applied_at) and 0 <= state.last_applied_at <= now


def _numeric_state_reason(state: CalibrationState, now: float) -> str | None:
    numbers = (
        state.original_dry,
        state.original_wet,
        state.current_wet,
        state.wet_step,
        now,
    )
    if not all(_finite(value) for value in numbers):
        return "invalid_state"
    if not (
        0 <= state.original_dry < state.original_wet <= ADC_MAX and state.wet_step > 0
    ):
        return "invalid_state"
    if not isinstance(state.revision, int) or state.revision < 0:
        return "invalid_state"
    if not _valid_applied_clock(state, now):
        return "invalid_state"
    if state.current_dry is None or not _finite(state.current_dry):
        return "invalid_state"
    return None


def _state_reason(state: CalibrationState, now: float) -> str | None:
    reason = _numeric_state_reason(state, now)
    if reason:
        return reason
    if (
        not state.source_fingerprint
        or state.source_fingerprint != state.baseline_source_fingerprint
    ):
        return "source_changed"
    if state.current_dry != state.original_dry or not _within_envelope(
        state.current_wet, state
    ):
        return "manual_override"
    span = state.original_wet - state.original_dry
    if state.previous_wet is not None and (
        not _finite(state.previous_wet)
        or not _within_envelope(state.previous_wet, state)
        or abs(state.previous_wet - state.current_wet)
        > MAX_STEP_FRACTION * span + EPSILON
    ):
        return "invalid_state"
    return None


def _sample_good(sample: PhysicalSample, state: CalibrationState) -> bool:
    span = state.original_wet - state.original_dry
    if (
        sample.reliable is not True
        or sample.source_fingerprint != state.source_fingerprint
    ):
        return False
    # Keep physical observations outside the fitted envelope. Filtering them
    # before robust window statistics could hide an out-of-bounds full target.
    if not _finite(sample.raw) or not 0 <= sample.raw <= ADC_MAX:
        return False
    burst = sample.burst
    if not MIN_BURST_READS <= len(burst) <= MAX_BURST_READS or not all(
        _finite(v) and 0 <= v <= ADC_MAX for v in burst
    ):
        return False
    center = median(burst)
    mad = median(abs(v - center) for v in burst)
    return (
        abs(sample.raw - center) <= 1.0
        and mad <= max(2.0, 0.02 * span)
        and _quantile(burst, 0.9) - _quantile(burst, 0.1) <= max(6.0, 0.1 * span)
    )


def _sample_unclipped(sample: PhysicalSample) -> bool:
    return 0 < sample.raw < ADC_MAX and 0 < median(sample.burst) < ADC_MAX


def _current_sample_good(sample: PhysicalSample, state: CalibrationState) -> bool:
    span = state.original_wet - state.original_dry
    return (
        _sample_good(sample, state)
        and _sample_unclipped(sample)
        and state.original_dry
        < sample.raw
        <= state.original_wet + MAX_TOTAL_DRIFT_FRACTION * span
    )


def _same_sample(left: PhysicalSample, right: PhysicalSample) -> bool:
    def burst_tuple(sample: PhysicalSample) -> tuple | None:
        return tuple(sample.burst) if isinstance(sample.burst, (tuple, list)) else None

    return (
        left.sampled_at == right.sampled_at
        and left.raw == right.raw
        and burst_tuple(left) == burst_tuple(right)
        and left.source_fingerprint == right.source_fingerprint
        and left.reliable is right.reliable
    )


def _current_reason(
    samples: Sequence[PhysicalSample], state: CalibrationState, now: float
) -> str | None:
    dated = [s for s in samples if _finite(s.sampled_at)]
    if not dated:
        return "stale_sample"
    latest = max(dated, key=lambda s: s.sampled_at)
    if any(
        not _same_sample(s, latest) for s in dated if s.sampled_at == latest.sampled_at
    ):
        return "conflicting_evidence"
    if latest.sampled_at > now:
        return "future_sample"
    if now - latest.sampled_at > CURRENT_MAX_AGE:
        return "stale_sample"
    if latest.source_fingerprint != state.source_fingerprint:
        return "source_changed"
    return None if _current_sample_good(latest, state) else "unreliable_sample"


def _unique_samples(
    samples: Sequence[PhysicalSample], state: CalibrationState, now: float
) -> list[PhysicalSample]:
    unique: dict[float, PhysicalSample] = {}
    conflicts: set[float] = set()
    for sample in samples:
        stamp = sample.sampled_at
        if not _finite(stamp) or not now - HISTORY_SECONDS <= stamp <= now:
            continue
        if stamp in unique and not _same_sample(sample, unique[stamp]):
            conflicts.add(stamp)
        else:
            unique[stamp] = sample
    return sorted(
        (s for t, s in unique.items() if t not in conflicts and _sample_good(s, state)),
        key=lambda s: s.sampled_at,
    )


def _reference_reason(
    reference: QualifiedWetReference, state: CalibrationState, now: float
) -> str | None:
    strings = (
        reference.reference_id,
        reference.cycle_id,
        reference.reference_source,
        reference.protocol,
    )
    if not all(isinstance(value, str) and value.strip() for value in strings):
        return "unqualified_reference"
    if reference.source_fingerprint != state.source_fingerprint:
        return "source_changed"
    if (
        not all(
            _finite(v)
            for v in (reference.start_at, reference.end_at, reference.reference_index)
        )
        or not 0 < reference.reference_index <= INDEX_MAX
    ):
        return "invalid_reference"
    if reference.start_at < now - HISTORY_SECONDS or reference.end_at > now:
        return "reference_time"
    if (
        not MIN_WINDOW_SECONDS
        <= reference.end_at - reference.start_at
        <= MAX_WINDOW_SECONDS
    ):
        return "reference_window"
    return None


def _coverage_good(
    samples: Sequence[PhysicalSample], reference: QualifiedWetReference
) -> bool:
    if len(samples) < MIN_VOTES:
        return False
    first, last = samples[0].sampled_at, samples[-1].sampled_at
    return (
        last - first >= MIN_WINDOW_SECONDS
        and first - reference.start_at <= NOMINAL_SAMPLE_SECONDS
        and reference.end_at - last <= NOMINAL_SAMPLE_SECONDS
        and all(
            b.sampled_at - a.sampled_at <= MAX_GAP_SECONDS for a, b in pairwise(samples)
        )
    )


def _stationary(values: Sequence[float], span: float) -> bool:
    center = median(values)
    mad = median(abs(value - center) for value in values)
    third = max(1, len(values) // 3)
    return (
        mad <= max(2.0, 0.02 * span)
        and _quantile(values, 0.9) - _quantile(values, 0.1)
        <= max(6.0, WINDOW_SPREAD_FRACTION * span)
        and abs(median(values[:third]) - median(values[-third:]))
        <= max(2.0, 0.02 * span)
    )


def _temporal_votes(samples: Sequence[PhysicalSample]) -> list[PhysicalSample]:
    selected: list[PhysicalSample] = []
    for sample in samples:
        if (
            not selected
            or sample.sampled_at - selected[-1].sampled_at >= MIN_VOTE_SPACING
        ):
            selected.append(sample)
    return selected


def _window(
    samples: Sequence[PhysicalSample],
    reference: QualifiedWetReference,
    state: CalibrationState,
) -> _Window | None:
    # Votes establish independence only; all physical observations inform statistics.
    selected = _temporal_votes(samples)
    if len(selected) < MIN_VOTES or not _coverage_good(samples, reference):
        return None
    values = [s.raw for s in samples]
    # Reported raw may differ by one count from the physical burst median.
    # Inspect both so that rounding cannot hide clipped evidence.
    if not all(_sample_unclipped(sample) for sample in samples):
        return None
    if not _stationary(values, state.original_wet - state.original_dry):
        return None
    digest = _digest(
        [
            (float(s.sampled_at), float(s.raw), tuple(float(v) for v in s.burst))
            for s in samples
        ]
    )
    return _Window(
        reference=reference,
        median_raw=median(values),
        q25_raw=_quantile(values, 0.25),
        q75_raw=_quantile(values, 0.75),
        sample_digest=digest,
    )


def _reference_copies(
    references: Sequence[QualifiedWetReference],
) -> dict[str, QualifiedWetReference] | None:
    copies: dict[str, QualifiedWetReference] = {}
    for reference in references:
        if (
            reference.reference_id in copies
            and reference != copies[reference.reference_id]
        ):
            return None
        copies[reference.reference_id] = reference
    return copies


def _collect_windows(
    samples: Sequence[PhysicalSample],
    references: Sequence[QualifiedWetReference],
    state: CalibrationState,
    now: float,
) -> tuple[list[_Window], CalibrationDiagnostics, bool]:
    copies = _reference_copies(references)
    if copies is None:
        return [], {}, True
    unique: dict[str, QualifiedWetReference] = {}
    rejected: dict[str, str] = {}
    for reference in copies.values():
        reason = _reference_reason(reference, state, now)
        if reason:
            rejected[reference.reference_id] = reason
        else:
            unique[reference.reference_id] = reference
    physical = _unique_samples(samples, state, now)
    stamps = [s.sampled_at for s in physical]
    windows: list[_Window] = []
    cycles: set[str] = set()
    for reference in sorted(
        unique.values(), key=lambda r: (r.start_at, r.end_at, r.reference_id)
    ):
        subset = physical[
            bisect_left(stamps, reference.start_at) : bisect_right(
                stamps, reference.end_at
            )
        ]
        window = _window(subset, reference, state)
        if window is None:
            rejected[reference.reference_id] = "coverage_or_stationarity"
        elif reference.cycle_id in cycles or (
            windows and reference.start_at <= windows[-1].reference.end_at
        ):
            rejected[reference.reference_id] = "dependent_cycle"
        else:
            cycles.add(reference.cycle_id)
            windows.append(window)
    return (
        windows,
        {"qualified_cycles": len(windows), "rejected_references": rejected},
        False,
    )


def _implied_wet(window: _Window, dry: float) -> float:
    return dry + (window.median_raw - dry) * 100.0 / window.reference.reference_index


def _error(raw: float, reference_index: float, dry: float, wet: float) -> float:
    # Deliberately unclipped: a capped 100 must not conceal calibration error.
    return abs(100.0 * (raw - dry) / (wet - dry) - reference_index)


def _window_error(window: _Window, dry: float, wet: float) -> float:
    return _error(window.median_raw, window.reference.reference_index, dry, wet)


def _evidence_id(windows: Sequence[_Window], state: CalibrationState, kind: str) -> str:
    facts = [
        [
            w.reference.reference_id,
            w.reference.cycle_id,
            float(w.reference.start_at),
            float(w.reference.end_at),
            w.reference.reference_source,
            w.reference.protocol,
            float(w.reference.reference_index),
            w.sample_digest,
        ]
        for w in windows
    ]
    return _digest(
        [
            kind,
            state.source_fingerprint,
            float(state.original_dry),
            float(state.original_wet),
            facts,
        ]
    )


def _reused(evidence_id: str, state: CalibrationState) -> bool:
    return (
        evidence_id == state.last_evidence_id
        or evidence_id in state.blocked_evidence_ids
    )


def _rollback(
    windows: Sequence[_Window],
    state: CalibrationState,
    now: float,
    diagnostics: CalibrationDiagnostics,
) -> CalibrationDecision | None:
    if (
        state.previous_wet is None
        or state.last_applied_at is None
        or state.last_evidence_id in state.blocked_evidence_ids
    ):
        return None
    later = [
        w
        for w in windows
        if w.reference.start_at > state.last_applied_at
        and now - w.reference.end_at <= HOLDOUT_MAX_AGE
    ]
    if not later:
        return None
    window = later[-1]
    if not _within_envelope(_implied_wet(window, state.original_dry), state):
        return None
    # Require agreement across the middle half, not one noisy median alone.
    if not all(
        _error(
            raw,
            window.reference.reference_index,
            state.original_dry,
            state.previous_wet,
        )
        + EPSILON
        < _error(
            raw, window.reference.reference_index, state.original_dry, state.current_wet
        )
        for raw in (window.q25_raw, window.median_raw, window.q75_raw)
    ):
        return None
    evidence = _evidence_id([window], state, "rollback")
    if _reused(evidence, state):
        return _observe("evidence_reused", diagnostics)
    blocked = tuple(v for v in (state.last_evidence_id, evidence) if v)
    details: CalibrationDiagnostics = {
        **diagnostics,
        "block_evidence_ids": blocked,
        "holdout_error_before": _window_error(
            window, state.original_dry, state.current_wet
        ),
        "holdout_error_after": _window_error(
            window, state.original_dry, state.previous_wet
        ),
    }
    return CalibrationDecision(
        "rollback",
        "contradiction",
        state.previous_wet,
        evidence,
        (window.reference.reference_id,),
        details,
    )


def _training_reason(windows: Sequence[_Window], now: float) -> str | None:
    if len(windows) < MIN_TRAINING_CYCLES:
        return "awaiting_cycles"
    training = windows[:-1] if len(windows) >= MIN_TOTAL_CYCLES else windows
    if (
        training[-1].reference.start_at - training[0].reference.start_at
        < TRAINING_SPAN_SECONDS
    ):
        return "insufficient_span"
    if (
        len(windows) < MIN_TOTAL_CYCLES
        or now - windows[-1].reference.end_at > HOLDOUT_MAX_AGE
    ):
        return "awaiting_holdout"
    return None


def _candidate(
    windows: Sequence[_Window],
    state: CalibrationState,
    diagnostics: CalibrationDiagnostics,
) -> CalibrationDecision:
    training, holdout = windows[:-1], windows[-1]
    span = state.original_wet - state.original_dry
    targets = [_implied_wet(w, state.original_dry) for w in training]
    target = median(targets)
    details: CalibrationDiagnostics = {**diagnostics, "target_wet": target}
    if any(
        not _within_envelope(value, state)
        for value in [*targets, _implied_wet(holdout, state.original_dry)]
    ):
        return _observe("target_out_of_bounds", details)
    evidence = _evidence_id(windows, state, "propose")
    if _reused(evidence, state):
        return _observe("evidence_reused", details)
    difference = target - state.current_wet
    if abs(difference) <= max(2.0, 0.005 * span):
        return _observe("deadband", details)
    # No EWMA. Quantization is our policy using the configured helper increment,
    # not a claim that HA set_value cannot store fractional ADC counts.
    steps = math.floor(
        (min(abs(difference), MAX_STEP_FRACTION * span) + EPSILON) / state.wet_step
    )
    if steps < 1:
        return _observe("helper_resolution", details)
    proposed = round(
        state.current_wet + math.copysign(steps * state.wet_step, difference), 10
    )
    return _verify_candidate(windows, state, proposed, evidence, details)


def _verify_candidate(
    windows: Sequence[_Window],
    state: CalibrationState,
    proposed: float,
    evidence: str,
    details: CalibrationDiagnostics,
) -> CalibrationDecision:
    training, holdout = windows[:-1], windows[-1]
    dry = state.original_dry
    before = _window_error(holdout, dry, state.current_wet)
    after = _window_error(holdout, dry, proposed)
    training_before = median(_window_error(w, dry, state.current_wet) for w in training)
    training_after = median(_window_error(w, dry, proposed) for w in training)
    details = {**details, "holdout_error_before": before, "holdout_error_after": after}
    if after + EPSILON >= before or training_after + EPSILON >= training_before:
        return _observe("holdout_failed", details)
    ids = tuple(w.reference.reference_id for w in [*training, holdout])
    return CalibrationDecision(
        "propose", "qualified_drift", proposed, evidence, ids, details
    )


def _preflight(
    samples: Sequence[PhysicalSample],
    references: Sequence[QualifiedWetReference],
    state: CalibrationState,
    now: float,
) -> CalibrationDecision | None:
    reason = _state_reason(state, now)
    if reason:
        return _observe(reason)
    if not references:
        return _observe("awaiting_reference")
    if len(samples) > MAX_SAMPLES or len(references) > MAX_REFERENCES:
        return _observe("evidence_limit")
    reason = _current_reason(samples, state, now)
    return _observe(reason) if reason else None


def _forward_calibration(
    windows: Sequence[_Window],
    state: CalibrationState,
    now: float,
    diagnostics: CalibrationDiagnostics,
) -> CalibrationDecision:
    if (
        state.last_applied_at is not None
        and now - state.last_applied_at < COOLDOWN_SECONDS
    ):
        return _observe("cooldown", diagnostics)
    fresh_cycles = [
        w
        for w in windows
        if state.last_applied_at is None or w.reference.start_at > state.last_applied_at
    ]
    reason = _training_reason(fresh_cycles, now)
    if reason:
        return _observe(reason, diagnostics)
    return _candidate(fresh_cycles, state, diagnostics)


def evaluate_calibration(
    samples: Sequence[PhysicalSample],
    references: Sequence[QualifiedWetReference],
    state: CalibrationState,
    now: float,
) -> CalibrationDecision:
    """Evaluate qualified observations without I/O or modifying caller state."""
    preflight = _preflight(samples, references, state, now)
    if preflight:
        return preflight
    windows, diagnostics, conflict = _collect_windows(samples, references, state, now)
    if conflict:
        return _observe("conflicting_evidence")
    if not windows:
        return _observe("poor_reference_quality", diagnostics)
    rollback = _rollback(windows, state, now, diagnostics)
    if rollback:
        return rollback
    return _forward_calibration(windows, state, now, diagnostics)
