"""Pure deterministic wet-endpoint calibration; runtime owns all writes."""

import math
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from statistics import median
from typing import Literal, TypedDict

from . import calibration_policy as policy


@dataclass(frozen=True)
class PhysicalSample:
    """One explicitly qualified physical burst, independent of its read count."""

    sampled_at: float
    raw: float
    burst: tuple[float, ...] | list[float]
    source_fingerprint: str
    reliable: bool = False

    def __post_init__(self) -> None:
        """Copy burst data independently of mutable caller lists."""
        object.__setattr__(self, "burst", tuple(self.burst))


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


@dataclass(frozen=True, kw_only=True)
class CalibrationState:
    """
    Validated runtime snapshot; the original baseline never follows helper edits.

    Runtime fences manual/source changes and clears their rollback authority.
    The decision fence includes actual changes and unresolved-attempt cutoffs;
    it does not replace the actual timestamps in the persisted journal.
    """

    original_dry: float
    original_wet: float
    current_wet: float
    revision: int
    decision_fence_at: float | None
    last_evidence_id: str | None
    source_fingerprint: str | None
    baseline_source_fingerprint: str | None
    current_dry: float | None
    previous_wet: float | None = None
    # Block revoked/uncertain attempts and rollback directives, never a successful
    # current forward effect merely because it was applied.
    blocked_evidence_ids: tuple[str, ...] | list[str] = ()
    wet_step: float = 1.0

    def __post_init__(self) -> None:
        """Retain evidence IDs independently of mutable caller lists."""
        object.__setattr__(
            self, "blocked_evidence_ids", tuple(self.blocked_evidence_ids)
        )

    @property
    def original_span(self) -> float:
        """Return the immutable span used by every drift bound."""
        return self.original_wet - self.original_dry


CalibrationReason = Literal[
    "invalid_state",
    "source_changed",
    "manual_override",
    "stale_sample",
    "conflicting_evidence",
    "future_sample",
    "unreliable_sample",
    "evidence_reused",
    "awaiting_cycles",
    "insufficient_span",
    "awaiting_holdout",
    "target_out_of_bounds",
    "deadband",
    "helper_resolution",
    "holdout_failed",
    "awaiting_reference",
    "evidence_limit",
    "cooldown",
    "poor_reference_quality",
    "qualified_drift",
    "contradiction",
]


class CalibrationDiagnostics(TypedDict, total=False):
    """
    Available facts plus a required persistence directive on accepted rollback.

    Runtime must union block_evidence_ids into state.blocked_evidence_ids when
    committing a rollback, and clear previous_wet. Other entries are diagnostics.
    """

    qualified_cycles: int
    fresh_qualified_cycles: int
    rejected_references: dict[str, str]
    block_evidence_ids: tuple[str, ...]
    holdout_error_before: float
    holdout_error_after: float
    target_wet: float


@dataclass(frozen=True, kw_only=True)
class CalibrationDecision:
    """Recommendation only; observe never contains a writable endpoint."""

    status: Literal["observe", "propose", "rollback"]
    reason: CalibrationReason
    proposed_wet: float | None
    evidence_id: str | None
    reference_ids: tuple[str, ...]
    diagnostics: CalibrationDiagnostics


@dataclass(frozen=True)
class _Window:
    reference: QualifiedWetReference
    median_raw: float
    q25_raw: float
    q75_raw: float
    sample_digest: str


def _finite(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _quantile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _observe(
    reason: CalibrationReason, diagnostics: CalibrationDiagnostics | None = None
) -> CalibrationDecision:
    return CalibrationDecision(
        status="observe",
        reason=reason,
        proposed_wet=None,
        evidence_id=None,
        reference_ids=(),
        diagnostics=diagnostics or {},
    )


def _within_envelope(value: float, state: CalibrationState) -> bool:
    return policy.within_envelope(state.original_dry, state.original_wet, value)


def _valid_decision_fence(state: CalibrationState, now: float) -> bool:
    if state.decision_fence_at is None:
        return (
            state.revision == 0
            and state.previous_wet is None
            and state.last_evidence_id is None
        )
    return _finite(state.decision_fence_at) and 0 <= state.decision_fence_at <= now


def _numeric_state_valid(state: CalibrationState, now: float) -> bool:
    numbers = (
        state.original_dry,
        state.original_wet,
        state.current_wet,
        state.current_dry,
        state.wet_step,
        now,
    )
    return (
        all(_finite(value) for value in numbers)
        and 0 <= state.original_dry < state.original_wet <= policy.ADC_MAX
        and state.wet_step > 0
        and isinstance(state.revision, int)
        and state.revision >= 0
        and _valid_decision_fence(state, now)
    )


def _state_reason(state: CalibrationState, now: float) -> CalibrationReason | None:
    if not _numeric_state_valid(state, now):
        return "invalid_state"
    if (
        not state.source_fingerprint
        or state.source_fingerprint != state.baseline_source_fingerprint
    ):
        return "source_changed"
    if state.current_dry != state.original_dry or not _within_envelope(
        state.current_wet, state
    ):
        return "manual_override"
    span = state.original_span
    if state.previous_wet is not None and (
        not _finite(state.previous_wet)
        or not _within_envelope(state.previous_wet, state)
        or abs(state.previous_wet - state.current_wet)
        > policy.MAX_STEP_FRACTION * span + policy.EPSILON
    ):
        return "invalid_state"
    return None


def _sample_good(sample: PhysicalSample, state: CalibrationState) -> bool:
    if (
        sample.reliable is not True
        or sample.source_fingerprint != state.source_fingerprint
    ):
        return False
    # Keep out-of-envelope readings until the complete robust target is evaluated.
    if not _finite(sample.raw) or not 0 <= sample.raw <= policy.ADC_MAX:
        return False
    burst = sample.burst
    if not policy.MIN_BURST_READS <= len(burst) <= policy.MAX_BURST_READS or not all(
        _finite(value) and 0 <= value <= policy.ADC_MAX for value in burst
    ):
        return False
    return abs(
        sample.raw - median(burst)
    ) <= policy.RAW_MEDIAN_TOLERANCE and _robust_spread_ok(
        burst, state.original_span, policy.BURST_SPREAD_FRACTION
    )


def _sample_unclipped(sample: PhysicalSample) -> bool:
    ordered = sorted(sample.burst)
    lower_middle = ordered[(len(ordered) - 1) // 2]
    upper_middle = ordered[len(ordered) // 2]
    return (
        0 < sample.raw < policy.ADC_MAX
        and lower_middle > 0
        and upper_middle < policy.ADC_MAX
    )


def _current_sample_good(sample: PhysicalSample, state: CalibrationState) -> bool:
    span = state.original_span
    return (
        _sample_good(sample, state)
        and _sample_unclipped(sample)
        and state.original_dry
        < sample.raw
        <= state.original_wet + policy.MAX_TOTAL_DRIFT_FRACTION * span
    )


def _same_sample(left: PhysicalSample, right: PhysicalSample) -> bool:
    return (
        left.sampled_at == right.sampled_at
        and left.raw == right.raw
        and left.burst == right.burst
        and left.source_fingerprint == right.source_fingerprint
        and left.reliable is right.reliable
    )


def _current_reason(
    samples: Sequence[PhysicalSample], state: CalibrationState, now: float
) -> CalibrationReason | None:
    dated = [s for s in samples if _finite(s.sampled_at)]
    if not dated:
        return "stale_sample"
    latest = max(dated, key=lambda s: s.sampled_at)
    if any(
        not _same_sample(s, latest)
        for s in dated
        if s is not latest and s.sampled_at == latest.sampled_at
    ):
        return "conflicting_evidence"
    if latest.sampled_at > now:
        return "future_sample"
    if now - latest.sampled_at > policy.CURRENT_MAX_AGE:
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
        if not _finite(stamp) or not now - policy.HISTORY_SECONDS <= stamp <= now:
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
        or not 0 < reference.reference_index <= policy.INDEX_MAX
    ):
        return "invalid_reference"
    if reference.start_at < now - policy.HISTORY_SECONDS or reference.end_at > now:
        return "reference_time"
    if (
        not policy.MIN_WINDOW_SECONDS
        <= reference.end_at - reference.start_at
        <= policy.MAX_WINDOW_SECONDS
    ):
        return "reference_window"
    return None


def _coverage_good(
    samples: Sequence[PhysicalSample], reference: QualifiedWetReference
) -> bool:
    if not samples:
        return False
    first, last = samples[0].sampled_at, samples[-1].sampled_at
    return (
        last - first >= policy.MIN_WINDOW_SECONDS
        and first - reference.start_at <= policy.NOMINAL_SAMPLE_SECONDS
        and reference.end_at - last <= policy.NOMINAL_SAMPLE_SECONDS
        and all(
            b.sampled_at - a.sampled_at <= policy.MAX_GAP_SECONDS
            for a, b in pairwise(samples)
        )
    )


def _robust_spread_ok(
    values: Sequence[float], span: float, spread_fraction: float
) -> bool:
    center = median(values)
    mad = median(abs(value - center) for value in values)
    return mad <= max(
        policy.MIN_MAD_COUNTS, policy.MAD_SPAN_FRACTION * span
    ) and _quantile(values, 0.9) - _quantile(values, 0.1) <= max(
        policy.MIN_SPREAD_COUNTS, spread_fraction * span
    )


def _stationary(values: Sequence[float], span: float) -> bool:
    third = max(1, len(values) // 3)
    return _robust_spread_ok(values, span, policy.WINDOW_SPREAD_FRACTION) and abs(
        median(values[:third]) - median(values[-third:])
    ) <= max(policy.MIN_TREND_COUNTS, policy.TREND_SPAN_FRACTION * span)


def _temporal_votes(samples: Sequence[PhysicalSample]) -> list[PhysicalSample]:
    selected: list[PhysicalSample] = []
    for sample in samples:
        if (
            not selected
            or sample.sampled_at - selected[-1].sampled_at >= policy.MIN_VOTE_SPACING
        ):
            selected.append(sample)
    return selected


def _window(
    samples: Sequence[PhysicalSample],
    reference: QualifiedWetReference,
    state: CalibrationState,
) -> _Window | Literal["coverage_or_stationarity", "clipped"]:
    # Votes establish independence; every good observation informs statistics.
    votes = _temporal_votes(samples)
    if len(votes) < policy.MIN_VOTES or not _coverage_good(samples, reference):
        return "coverage_or_stationarity"
    if not all(_sample_unclipped(sample) for sample in samples):
        return "clipped"
    values = [sample.raw for sample in samples]
    if not _stationary(values, state.original_span):
        return "coverage_or_stationarity"
    digest = policy.canonical_digest(
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


def _references_by_id(
    references: Sequence[QualifiedWetReference],
) -> dict[str, QualifiedWetReference] | None:
    by_id: dict[str, QualifiedWetReference] = {}
    for reference in references:
        if (
            reference.reference_id in by_id
            and reference != by_id[reference.reference_id]
        ):
            return None
        by_id[reference.reference_id] = reference
    return by_id


def _collect_windows(
    samples: Sequence[PhysicalSample],
    references: dict[str, QualifiedWetReference],
    state: CalibrationState,
    now: float,
) -> tuple[list[_Window], CalibrationDiagnostics]:
    valid: list[QualifiedWetReference] = []
    rejected: dict[str, str] = {}
    for reference in references.values():
        reason = _reference_reason(reference, state, now)
        if reason is not None:
            rejected[reference.reference_id] = reason
        else:
            valid.append(reference)
    physical = _unique_samples(samples, state, now)
    stamps = [sample.sampled_at for sample in physical]
    windows: list[_Window] = []
    cycles: set[str] = set()
    for reference in sorted(
        valid, key=lambda r: (r.start_at, r.end_at, r.reference_id)
    ):
        subset = physical[
            bisect_left(stamps, reference.start_at) : bisect_right(
                stamps, reference.end_at
            )
        ]
        window = _window(subset, reference, state)
        if isinstance(window, str):
            rejected[reference.reference_id] = window
        elif reference.cycle_id in cycles or (
            windows and reference.start_at <= windows[-1].reference.end_at
        ):
            rejected[reference.reference_id] = "dependent_cycle"
        else:
            cycles.add(reference.cycle_id)
            windows.append(window)
    return windows, {"qualified_cycles": len(windows), "rejected_references": rejected}


def _implied_wet(window: _Window, dry: float) -> float:
    return (
        dry
        + (window.median_raw - dry)
        * policy.INDEX_MAX
        / window.reference.reference_index
    )


def _error(raw: float, reference_index: float, dry: float, wet: float) -> float:
    # Deliberately unclipped: a capped 100 must not conceal calibration error.
    return abs(policy.INDEX_MAX * (raw - dry) / (wet - dry) - reference_index)


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
    return policy.canonical_digest(
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


def _after_decision_fence(
    windows: Sequence[_Window], state: CalibrationState
) -> list[_Window]:
    return [
        w
        for w in windows
        if state.decision_fence_at is None
        or w.reference.start_at > state.decision_fence_at
    ]


def _rollback(
    windows: Sequence[_Window],
    state: CalibrationState,
    now: float,
    diagnostics: CalibrationDiagnostics,
) -> CalibrationDecision | None:
    # A blocked current effect has no rollback authority; accepted forward
    # effects are never blocked merely because they were successfully applied.
    if (
        state.previous_wet is None
        or state.decision_fence_at is None
        or state.last_evidence_id in state.blocked_evidence_ids
    ):
        return None
    later = [
        w
        for w in _after_decision_fence(windows, state)
        if now - w.reference.end_at <= policy.HOLDOUT_MAX_AGE
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
        + policy.EPSILON
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
        status="rollback",
        reason="contradiction",
        proposed_wet=state.previous_wet,
        evidence_id=evidence,
        reference_ids=(window.reference.reference_id,),
        diagnostics=details,
    )


def _training_reason(
    windows: Sequence[_Window], now: float
) -> CalibrationReason | None:
    if len(windows) < policy.MIN_TRAINING_CYCLES:
        return "awaiting_cycles"
    training = windows[:-1] if len(windows) >= policy.MIN_TOTAL_CYCLES else windows
    if (
        training[-1].reference.start_at - training[0].reference.start_at
        < policy.TRAINING_SPAN_SECONDS
    ):
        return "insufficient_span"
    if (
        len(windows) < policy.MIN_TOTAL_CYCLES
        or now - windows[-1].reference.end_at > policy.HOLDOUT_MAX_AGE
    ):
        return "awaiting_holdout"
    return None


def _candidate(
    windows: Sequence[_Window],
    state: CalibrationState,
    diagnostics: CalibrationDiagnostics,
) -> CalibrationDecision:
    training, holdout = windows[:-1], windows[-1]
    span = state.original_span
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
    if abs(difference) <= max(
        policy.MIN_DEADBAND_COUNTS, policy.DEADBAND_SPAN_FRACTION * span
    ):
        return _observe("deadband", details)
    # Quantize the bounded move using the chosen write increment without EWMA.
    steps = math.floor(
        (min(abs(difference), policy.MAX_STEP_FRACTION * span) + policy.EPSILON)
        / state.wet_step
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
    if (
        after + policy.EPSILON >= before
        or training_after + policy.EPSILON >= training_before
    ):
        return _observe("holdout_failed", details)
    ids = tuple(window.reference.reference_id for window in windows)
    return CalibrationDecision(
        status="propose",
        reason="qualified_drift",
        proposed_wet=proposed,
        evidence_id=evidence,
        reference_ids=ids,
        diagnostics=details,
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
    if len(samples) > policy.MAX_SAMPLES or len(references) > policy.MAX_REFERENCES:
        return _observe("evidence_limit")
    reason = _current_reason(samples, state, now)
    return _observe(reason) if reason else None


def _forward_calibration(
    windows: Sequence[_Window],
    state: CalibrationState,
    now: float,
    diagnostics: CalibrationDiagnostics,
) -> CalibrationDecision:
    fresh_cycles = _after_decision_fence(windows, state)
    diagnostics = {**diagnostics, "fresh_qualified_cycles": len(fresh_cycles)}
    if (
        state.decision_fence_at is not None
        and now - state.decision_fence_at < policy.COOLDOWN_SECONDS
    ):
        return _observe("cooldown", diagnostics)
    reason = _training_reason(fresh_cycles, now)
    if reason is not None:
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
    if preflight is not None:
        return preflight
    references_by_id = _references_by_id(references)
    if references_by_id is None:
        return _observe("conflicting_evidence")
    windows, diagnostics = _collect_windows(samples, references_by_id, state, now)
    if not windows:
        return _observe("poor_reference_quality", diagnostics)
    rollback = _rollback(windows, state, now, diagnostics)
    if rollback is not None:
        return rollback
    return _forward_calibration(windows, state, now, diagnostics)
