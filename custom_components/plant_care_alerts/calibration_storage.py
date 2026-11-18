"""Validate durable calibration evidence without repairing or rebasing it."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Never

FORMAT_VERSION = 1
ADC_MAX = 4095
BARRIER_SECONDS = 1800
COOLDOWN_SECONDS = 7 * 86400
TOTAL_FRACTION = 0.05
STEP_FRACTION = 0.01
EPSILON = 1e-9
PAIR_SIZE = 2
QUALIFIED_REASONS = {"automatic": "qualified_drift", "rollback": "contradiction"}
EXCLUDED_REASONS = {"concurrent_change", "pending_effect_reconciled"}
EVENT_REASONS = {
    "automatic": {"qualified_drift", "concurrent_change"},
    "rollback": {"contradiction", "concurrent_change"},
    "manual": {None},
    "external_discovered": {None, "pending_effect_reconciled"},
}
REFERENCE_FIELDS = {
    "reference_id",
    "cycle_id",
    "start_at",
    "end_at",
    "source_fingerprint",
    "reference_source",
    "protocol",
    "reference_index",
}


class StorageIntegrityError(ValueError):
    """A durable state is missing, malformed, or contradicts its own evidence."""


def _fail(path: str, reason: str) -> Never:
    message = f"{path}: {reason}"
    raise StorageIntegrityError(message)


def _mapping(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        _fail(path, "expected an object")
    return value


def _list(value: Any, path: str) -> list:
    if not isinstance(value, list):
        _fail(path, "expected a JSON array")
    return value


def _required(value: dict, fields: set[str], path: str) -> None:
    missing = fields - value.keys()
    if missing:
        _fail(path, f"missing fields {sorted(missing)}")


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "expected nonempty text")
    return value


def _integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(path, "expected a nonnegative integer")
    return value


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "expected a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        _fail(path, "number cannot be represented as a finite value")
    if not finite:
        _fail(path, "expected a finite number")
    return value


def _timestamp(value: Any, path: str) -> float:
    result = _number(value, path)
    if result <= 0:
        _fail(path, "expected a positive timestamp")
    return result


def _boolean(value: Any, path: str) -> None:
    if not isinstance(value, bool):
        _fail(path, "expected a boolean")


def _pair(value: Any, path: str) -> list:
    pair = _list(value, path)
    if len(pair) != PAIR_SIZE:
        _fail(path, "expected dry and wet values")
    for index, item in enumerate(pair):
        _number(item, f"{path}[{index}]")
    return pair


def _strings(value: Any, path: str) -> list:
    result = _list(value, path)
    for item in result:
        _text(item, path)
    if len(set(result)) != len(result):
        _fail(path, "duplicate identifiers")
    return result


def source_fingerprint(identity: str, epoch: int) -> str:
    """Bind an observed source identity to a persistent, nonreusable epoch."""
    _text(identity, "source identity")
    _integer(epoch, "source epoch")
    value = json.dumps([identity, epoch], separators=(",", ":")).encode()
    return hashlib.sha256(value).hexdigest()


def _originals(value: Any, path: str) -> list:
    config = _mapping(value, path)
    _required(config, {"original_dry", "original_wet"}, path)
    dry = _number(config["original_dry"], f"{path}.original_dry")
    wet = _number(config["original_wet"], f"{path}.original_wet")
    if not 0 <= dry < wet <= ADC_MAX:
        _fail(path, "invalid configured original anchors")
    return [dry, wet]


def _baseline(record: dict, configured: dict | None, path: str) -> list:
    value = _mapping(record["baseline"], f"{path}.baseline")
    _required(
        value,
        {"dry", "wet", "source_fingerprint", "created_at", "provenance"},
        f"{path}.baseline",
    )
    original = _originals(
        {"original_dry": value["dry"], "original_wet": value["wet"]},
        f"{path}.baseline",
    )
    if value["provenance"] != "configured_original":
        _fail(path, "baseline lacks configured-original provenance")
    _timestamp(value["created_at"], f"{path}.baseline.created_at")
    _text(value["source_fingerprint"], f"{path}.baseline.source_fingerprint")
    if configured is not None and original != _originals(configured, path):
        _fail(path, "stored baseline contradicts configured original anchors")
    return original


def _fences(record: dict, path: str) -> dict[str, tuple[float, float]]:
    epoch = _integer(record["source_epoch"], f"{path}.source_epoch")
    fences = _list(record["source_fences"], f"{path}.source_fences")
    if len(fences) != epoch + 1:
        _fail(path, "source epoch and fence history disagree")
    previous_at = 0.0
    for index, item in enumerate(fences):
        fence = _mapping(item, f"{path}.source_fences[{index}]")
        _required(fence, {"epoch", "identity", "fingerprint", "at"}, path)
        if _integer(fence["epoch"], path) != index:
            _fail(path, "source fences are not contiguous")
        identity = _text(fence["identity"], path)
        if fence["fingerprint"] != source_fingerprint(identity, index):
            _fail(path, "source fence fingerprint does not bind its identity and epoch")
        at = _timestamp(fence["at"], path)
        if at < previous_at:
            _fail(path, "source fence times moved backwards")
        previous_at = at
    baseline = record["baseline"]
    if (
        fences[0]["fingerprint"] != baseline["source_fingerprint"]
        or fences[0]["at"] != baseline["created_at"]
        or fences[-1]["fingerprint"] != record["source_fingerprint"]
        or fences[-1]["identity"] != record["identity_fingerprint"]
    ):
        _fail(path, "baseline or current source contradicts the fence history")
    ends = [fence["at"] for fence in fences[1:]] + [math.inf]
    return {
        fence["fingerprint"]: (fence["at"], end)
        for fence, end in zip(fences, ends, strict=True)
    }


def _epoch_time(
    fingerprint: Any, at: float, intervals: dict[str, tuple[float, float]], path: str
) -> None:
    identity = _text(fingerprint, path)
    if identity not in intervals:
        _fail(path, "observation source has no recorded epoch")
    start, end = intervals[identity]
    if not start <= at < end:
        _fail(path, "observation time is outside its source epoch")


def _reference(
    value: Any, fingerprints: dict[str, tuple[float, float]], path: str
) -> dict:
    reference = _mapping(value, path)
    if set(reference) != REFERENCE_FIELDS:
        _fail(path, "reference fields are incomplete or unknown")
    for field in ("reference_id", "cycle_id", "reference_source", "protocol"):
        _text(reference[field], f"{path}.{field}")
    start = _timestamp(reference["start_at"], f"{path}.start_at")
    end = _timestamp(reference["end_at"], f"{path}.end_at")
    if start >= end:
        _fail(path, "reference interval is empty or reversed")
    _number(reference["reference_index"], f"{path}.reference_index")
    fingerprint = reference["source_fingerprint"]
    _epoch_time(fingerprint, start, fingerprints, f"{path}.start_at")
    _epoch_time(fingerprint, end, fingerprints, f"{path}.end_at")
    return reference


def _references(
    value: Any, fingerprints: dict[str, tuple[float, float]], path: str
) -> list:
    references = _list(value, path)
    for index, reference in enumerate(references):
        _reference(reference, fingerprints, f"{path}[{index}]")
    return references


def _evidence(
    value: dict, fingerprints: dict[str, tuple[float, float]], path: str
) -> None:
    _required(
        value, {"evidence_id", "reference_ids", "references", "diagnostics"}, path
    )
    _text(value["evidence_id"], f"{path}.evidence_id")
    ids = _strings(value["reference_ids"], f"{path}.reference_ids")
    references = _references(value["references"], fingerprints, f"{path}.references")
    if not ids or set(ids) != {item["reference_id"] for item in references}:
        _fail(path, "evidence identifiers and reference snapshots disagree")
    if any(
        item["source_fingerprint"] != value["source_fingerprint"] for item in references
    ):
        _fail(path, "evidence mixes source epochs")
    diagnostics = _mapping(value["diagnostics"], f"{path}.diagnostics")
    if "block_evidence_ids" in diagnostics:
        _strings(diagnostics["block_evidence_ids"], f"{path}.block_evidence_ids")


def _rollback_exclusions(value: dict, prior: str | None, path: str) -> set[str]:
    if not prior:
        _fail(path, "rollback has no prior qualified evidence")
    excluded = set(_strings(value["diagnostics"].get("block_evidence_ids"), path))
    if not {prior, value["evidence_id"]} <= excluded:
        _fail(path, "rollback must exclude its prior and contradictory evidence")
    return excluded


def _bounded_change(before: list, after: list, original: list, path: str) -> None:
    dry, wet = original
    span = wet - dry
    if before[0] != dry or after[0] != dry or before == after:
        _fail(path, "automatic effect changes dry or has no wet change")
    if any(
        value <= dry
        or value > ADC_MAX
        or abs(value - wet) > TOTAL_FRACTION * span + EPSILON
        for value in (before[1], after[1])
    ):
        _fail(path, "automatic effect exceeds the original envelope")
    if abs(after[1] - before[1]) > STEP_FRACTION * span + EPSILON:
        _fail(path, "automatic effect exceeds one bounded step")


def _event_time(event: dict) -> float:
    return event["detected_at"] if event["at"] is None else event["at"]


def _event(
    value: Any,
    source: str,
    original: list,
    fingerprints: dict[str, tuple[float, float]],
    path: str,
) -> dict:
    event = _mapping(value, path)
    _required(
        event,
        {
            "event_id",
            "source",
            "at",
            "detected_at",
            "kind",
            "old_dry",
            "old_wet",
            "new_dry",
            "new_wet",
            "revision",
            "source_fingerprint",
        },
        path,
    )
    _text(event["event_id"], f"{path}.event_id")
    _integer(event["revision"], f"{path}.revision")
    if event["source"] != source:
        _fail(path, "journal event belongs to another source")
    _timestamp(event["detected_at"], f"{path}.detected_at")
    kind = _text(event["kind"], f"{path}.kind")
    if kind not in EVENT_REASONS:
        _fail(path, "unknown actual-change kind")
    if kind == "external_discovered":
        if event["at"] is not None:
            _fail(path, "an unobserved effect cannot claim an exact time")
    else:
        _timestamp(event["at"], f"{path}.at")
    for field in ("old_dry", "old_wet", "new_dry", "new_wet"):
        _number(event[field], f"{path}.{field}")
    if _text(event["source_fingerprint"], path) not in fingerprints:
        _fail(path, "journal source has no recorded epoch")
    before = [event["old_dry"], event["old_wet"]]
    after = [event["new_dry"], event["new_wet"]]
    if before == after:
        _fail(path, "journal claims an effect without an actual change")
    _event_evidence(event, original, fingerprints, path)
    return event


def _event_evidence(
    event: dict, original: list, fingerprints: dict[str, tuple[float, float]], path: str
) -> None:
    reason = event.get("reason")
    if reason is not None:
        _text(reason, f"{path}.reason")
    if reason not in EVENT_REASONS[event["kind"]]:
        _fail(f"{path}.reason", "reason does not describe a supported event kind")
    if _qualified(event):
        _completed_evidence(event, original, fingerprints, path)
    elif reason in EXCLUDED_REASONS:
        _text(event.get("evidence_id"), f"{path}.evidence_id")
        if {"reference_ids", "references", "diagnostics"} & event.keys():
            _event_references(event, fingerprints, path)
    elif {"evidence_id", "reference_ids", "references", "diagnostics"} & event.keys():
        _fail(path, "ordinary observed change claims decision evidence")


def _event_references(
    event: dict, fingerprints: dict[str, tuple[float, float]], path: str
) -> None:
    _evidence(event, fingerprints, path)
    if any(
        reference["end_at"] > _event_time(event) for reference in event["references"]
    ):
        _fail(path, "completed effect uses a reference from its future")


def _completed_evidence(
    event: dict, original: list, fingerprints: dict[str, tuple[float, float]], path: str
) -> None:
    _event_references(event, fingerprints, path)
    _bounded_change(
        [event["old_dry"], event["old_wet"]],
        [event["new_dry"], event["new_wet"]],
        original,
        path,
    )
    _epoch_time(
        event["source_fingerprint"], _event_time(event), fingerprints, f"{path}.at"
    )


def _qualified(event: dict) -> bool:
    return (
        event["kind"] in QUALIFIED_REASONS
        and event.get("reason") == QUALIFIED_REASONS[event["kind"]]
    )


def _journal_rollback_target(event: dict, previous: dict | None, path: str) -> None:
    if event["kind"] != "rollback" or not _qualified(event):
        return
    if (
        previous is None
        or previous["kind"] != "automatic"
        or not _qualified(previous)
        or previous["source_fingerprint"] != event["source_fingerprint"]
        or event["new_wet"] != previous["old_wet"]
    ):
        _fail(path, "completed rollback does not restore the preceding verified wet")


def _journal_evidence(
    event: dict,
    last_evidence: str | None,
    evidence_ids: set[str],
    blocked: set[str],
    path: str,
) -> str | None:
    if not _qualified(event):
        return last_evidence
    if event["kind"] == "rollback":
        excluded = _rollback_exclusions(event, last_evidence, path)
        if not excluded <= blocked:
            _fail(path, "rollback evidence lost its persistent exclusion")
    last_evidence = event["evidence_id"]
    if last_evidence in evidence_ids:
        _fail(path, "qualified evidence was applied more than once")
    evidence_ids.add(last_evidence)
    return last_evidence


def _history(
    record: dict,
    original: list,
    fingerprints: dict[str, tuple[float, float]],
    path: str,
) -> None:
    history = _list(record["history"], f"{path}.history")
    revision = _integer(record["revision"], f"{path}.revision")
    if revision != len(history):
        _fail(path, "revision contradicts the complete actual-change journal")
    previous = original
    previous_event = None
    event_ids: set[str] = set()
    evidence_ids: set[str] = set()
    last_evidence = None
    previous_time = record["baseline"]["created_at"]
    blocked = set(record["blocked_evidence_ids"])
    for index, value in enumerate(history, start=1):
        event_path = f"{path}.history[{index - 1}]"
        event = _event(value, record["source"], original, fingerprints, event_path)
        _journal_rollback_target(event, previous_event, event_path)
        if event["revision"] != index or event["event_id"] in event_ids:
            _fail(
                event_path,
                "journal revision or event identifier is duplicated or missing",
            )
        if [event["old_dry"], event["old_wet"]] != previous:
            _fail(
                event_path, "journal old values do not continue the prior actual state"
            )
        if _event_time(event) < previous_time:
            _fail(event_path, "actual-change clocks moved backwards")
        previous_time = _event_time(event)
        previous = [event["new_dry"], event["new_wet"]]
        event_ids.add(event["event_id"])
        last_evidence = _journal_evidence(
            event, last_evidence, evidence_ids, blocked, event_path
        )
        previous_event = event
    if record.get("last_evidence_id") != last_evidence:
        _fail(path, "last qualified evidence contradicts the journal")
    if record["current"] != previous:
        _fail(path, "current anchors contradict the journal")
    expected_previous = (
        [history[-1]["old_dry"], history[-1]["old_wet"]] if history else original
    )
    if record["previous"] != expected_previous:
        _fail(path, "previous anchors contradict the journal")
    _effect_fields(record, history, path)


def _effect_fields(record: dict, history: list, path: str) -> None:
    if not history:
        if any(
            record.get(field) is not None
            for field in (
                "last_applied_at",
                "effective_at",
                "verified_revision",
                "previous_wet",
            )
        ):
            _fail(path, "an initial record claims an unjournaled effect")
        return
    latest = history[-1]
    effective = _event_time(latest)
    for field in ("last_applied_at", "effective_at"):
        if _timestamp(record.get(field), f"{path}.{field}") != effective:
            _fail(
                path, "cooldown or effective clock contradicts the last actual change"
            )
    if _timestamp(record.get("barrier_until"), path) < effective + BARRIER_SECONDS:
        _fail(path, "smoothing barrier does not cover the last actual change")
    _verified_effect(record, latest, path)


def _verified_effect(record: dict, latest: dict, path: str) -> None:
    verified = record.get("verified_revision")
    previous_wet = record.get("previous_wet")
    current_qualified = (
        _qualified(latest)
        and latest["source_fingerprint"] == record["source_fingerprint"]
    )
    if verified is None:
        if current_qualified:
            _fail(path, "latest qualified effect lost its current verification")
        if previous_wet is not None:
            _fail(path, "unverified state claims a protective rollback target")
        return
    if _integer(verified, path) != record["revision"] or not current_qualified:
        _fail(path, "verified revision has no matching current qualified effect")
    expected = latest["old_wet"] if latest["kind"] == "automatic" else None
    if previous_wet is not None:
        _number(previous_wet, path)
    if previous_wet != expected:
        _fail(path, "rollback target is not the preceding verified wet anchor")


def _samples(
    record: dict, fingerprints: dict[str, tuple[float, float]], path: str
) -> None:
    samples = _list(record["samples"], f"{path}.samples")
    fields = {
        "sampled_at",
        "raw",
        "burst",
        "source_fingerprint",
        "reliable",
        "rollback_eligible",
    }
    for index, value in enumerate(samples):
        sample_path = f"{path}.samples[{index}]"
        sample = _mapping(value, sample_path)
        _required(sample, fields, sample_path)
        at = _timestamp(sample["sampled_at"], f"{sample_path}.sampled_at")
        _epoch_time(sample["source_fingerprint"], at, fingerprints, sample_path)
        _boolean(sample["reliable"], f"{sample_path}.reliable")
        _boolean(sample["rollback_eligible"], f"{sample_path}.rollback_eligible")
        _sample_values(sample, sample_path)


def _sample_values(sample: dict, path: str) -> None:
    values = [_number(sample["raw"], f"{path}.raw")]
    burst = _list(sample["burst"], f"{path}.burst")
    values.extend(
        _number(reading, f"{path}.burst[{index}]")
        for index, reading in enumerate(burst)
    )
    if (sample["reliable"] or sample["rollback_eligible"]) and any(
        not 0 <= value <= ADC_MAX for value in values
    ):
        _fail(path, "trusted observation contains a value outside physical ADC bounds")


def _pending_evidence(
    record: dict, pending: dict, after: list, started: float, path: str
) -> None:
    kind = pending["kind"]
    if any(reference["end_at"] > started for reference in pending["references"]):
        _fail(path, "pending intent uses a reference from its future")
    used = {
        event["evidence_id"] for event in record["history"] if _qualified(event)
    } | set(record["blocked_evidence_ids"])
    if pending["evidence_id"] in used:
        _fail(path, "pending intent reuses accepted or blocked evidence")
    if kind == "rollback" and (
        record.get("verified_revision") != record["revision"]
        or record.get("previous_wet") != after[1]
    ):
        _fail(path, "pending rollback has no verified preceding target")
    if kind == "rollback":
        _rollback_exclusions(pending, record.get("last_evidence_id"), path)


def _pending(
    record: dict,
    original: list,
    fingerprints: dict[str, tuple[float, float]],
    path: str,
) -> None:
    value = record["pending"]
    if value is None:
        return
    pending_path = f"{path}.pending"
    pending = _mapping(value, pending_path)
    _required(
        pending,
        {
            "expected",
            "target",
            "expected_revision",
            "source_fingerprint",
            "source_epoch",
            "kind",
            "context_id",
            "started_at",
            "reason",
            "without_incident",
        },
        pending_path,
    )
    before = _pair(pending["expected"], f"{pending_path}.expected")
    after = _pair(pending["target"], f"{pending_path}.target")
    if (
        before != record["current"]
        or _integer(pending["expected_revision"], f"{pending_path}.expected_revision")
        != record["revision"]
        or _integer(pending["source_epoch"], f"{pending_path}.source_epoch")
        != record["source_epoch"]
        or pending["source_fingerprint"] != record["source_fingerprint"]
    ):
        _fail(
            pending_path, "pending intent no longer describes the saved current state"
        )
    kind = _text(pending["kind"], f"{pending_path}.kind")
    if kind not in QUALIFIED_REASONS or pending["reason"] != QUALIFIED_REASONS[kind]:
        _fail(pending_path, "pending intent has no qualified decision")
    _text(pending["context_id"], f"{pending_path}.context_id")
    started = _timestamp(pending["started_at"], f"{pending_path}.started_at")
    if started < (record.get("last_applied_at") or record["baseline"]["created_at"]):
        _fail(pending_path, "pending intent predates the current state")
    _boolean(pending["without_incident"], f"{pending_path}.without_incident")
    _bounded_change(before, after, original, pending_path)
    _evidence(pending, fingerprints, pending_path)
    _pending_evidence(record, pending, after, started, pending_path)


def _attempts(record: dict, original: list, path: str) -> None:
    if "attempts" not in record:
        return
    attempts = _list(record["attempts"], f"{path}.attempts")
    seen: set[str] = set()
    for index, value in enumerate(attempts):
        attempt_path = f"{path}.attempts[{index}]"
        attempt = _mapping(value, attempt_path)
        _required(
            attempt,
            {
                "attempt_id",
                "started_at",
                "resolved_at",
                "outcome",
                "expected",
                "target",
                "observed",
                "evidence_id",
            },
            attempt_path,
        )
        identifier = _text(attempt["attempt_id"], f"{attempt_path}.attempt_id")
        if identifier in seen:
            _fail(attempt_path, "resolved attempt identifier is duplicated")
        seen.add(identifier)
        started = _timestamp(attempt["started_at"], f"{attempt_path}.started_at")
        resolved = _timestamp(attempt["resolved_at"], f"{attempt_path}.resolved_at")
        if started < record["baseline"]["created_at"] or resolved < started:
            _fail(attempt_path, "attempt clocks contradict enrollment or resolution")
        outcome = _text(attempt["outcome"], f"{attempt_path}.outcome")
        if outcome not in {"aborted", "interrupted_unknown"}:
            _fail(attempt_path, "attempt cannot claim a completed actual effect")
        before = _pair(attempt["expected"], f"{attempt_path}.expected")
        after = _pair(attempt["target"], f"{attempt_path}.target")
        _pair(attempt["observed"], f"{attempt_path}.observed")
        _bounded_change(before, after, original, attempt_path)
        evidence_id = _text(attempt["evidence_id"], f"{attempt_path}.evidence_id")
        if (
            outcome == "interrupted_unknown"
            and evidence_id not in record["blocked_evidence_ids"]
        ):
            _fail(
                attempt_path, "uncertain attempt lost its persistent evidence exclusion"
            )
    if record["pending"] and record["pending"]["context_id"] in seen:
        _fail(path, "pending intent was already resolved as an attempt")


def _event_exclusions(event: dict) -> list[str]:
    if _qualified(event) and event["kind"] == "rollback":
        return event["diagnostics"]["block_evidence_ids"]
    if event.get("reason") in EXCLUDED_REASONS:
        return [event["evidence_id"]]
    return []


def _attempt_exclusions(record: dict) -> dict[str, float]:
    blocked_at: dict[str, float] = {}
    for attempt in record.get("attempts", []):
        if attempt["outcome"] == "interrupted_unknown":
            identifier = attempt["evidence_id"]
            blocked_at[identifier] = min(
                blocked_at.get(identifier, math.inf), attempt["resolved_at"]
            )
    return blocked_at


def _decision_cutoff(
    previous: float | None, resolutions: list[float], at: float | None
) -> float | None:
    times = [] if previous is None else [previous]
    times.extend(time for time in resolutions if at is None or time <= at)
    return max(times, default=None)


def _reference_keys(value: dict) -> set[tuple[str, str, str]]:
    return {
        (reference["source_fingerprint"], field, reference[field])
        for reference in value["references"]
        for field in ("reference_id", "cycle_id")
    }


def _decision_identities(value: dict, path: str) -> None:
    seen: set[tuple[str, str]] = set()
    for index, reference in enumerate(value["references"]):
        for field in ("reference_id", "cycle_id"):
            key = (field, reference[field])
            if key in seen:
                _fail(
                    f"{path}.references[{index}].{field}", "duplicate decision identity"
                )
            seen.add(key)


def _decision_constraints(
    value: dict,
    at: float,
    cutoff: float | None,
    consumed: set[tuple[str, str, str]],
    path: str,
) -> None:
    _decision_identities(value, path)
    if cutoff is not None:
        if value["kind"] == "automatic" and at - cutoff < COOLDOWN_SECONDS:
            _fail(path, "ordinary decision does not respect the seven-day cooldown")
        if any(reference["start_at"] <= cutoff for reference in value["references"]):
            _fail(path, "qualified decision reuses a pre-change reference window")
    if _reference_keys(value) & consumed:
        _fail(path, "qualified decision reuses an accepted reference or cycle identity")


def _decision_replay(record: dict, path: str) -> None:
    """Replay known exclusions, consumed cycles and temporal decision fences."""
    resolutions = [
        attempt["resolved_at"]
        for attempt in record.get("attempts", [])
        if attempt["outcome"] == "interrupted_unknown"
    ]
    blocked_at = _attempt_exclusions(record)
    consumed: set[tuple[str, str, str]] = set()
    cutoff = None
    for index, event in enumerate(record["history"]):
        event_path = f"{path}.history[{index}]"
        at = _event_time(event)
        prior = _decision_cutoff(cutoff, resolutions, at)
        if _qualified(event):
            _decision_constraints(event, at, prior, consumed, event_path)
            if blocked_at.get(event["evidence_id"], math.inf) <= at:
                _fail(
                    event_path,
                    "completed effect reuses evidence already excluded at that time",
                )
            consumed.update(_reference_keys(event))
        for identifier in _event_exclusions(event):
            blocked_at[identifier] = min(blocked_at.get(identifier, math.inf), at)
        cutoff = at
    if not set(blocked_at) <= set(record["blocked_evidence_ids"]):
        _fail(path, "journal evidence lost its persistent exclusion")
    if record["pending"] is not None:
        pending = record["pending"]
        _decision_constraints(
            pending,
            pending["started_at"],
            _decision_cutoff(cutoff, resolutions, None),
            consumed,
            f"{path}.pending",
        )


def _record(source: str, value: Any, configured: dict | None) -> None:
    path = f"records[{source}]"
    record = _mapping(value, path)
    _required(
        record,
        {
            "source",
            "baseline",
            "current",
            "previous",
            "source_fingerprint",
            "identity_fingerprint",
            "source_epoch",
            "source_fences",
            "revision",
            "blocked_evidence_ids",
            "history",
            "samples",
            "references",
            "pending",
            "status",
            "last_saved_at",
            "last_evaluated_at",
        },
        path,
    )
    if record["source"] != source:
        _fail(path, "record key and source disagree")
    original = _baseline(record, configured, path)
    fingerprints = _fences(record, path)
    _pair(record["current"], f"{path}.current")
    _pair(record["previous"], f"{path}.previous")
    _strings(record["blocked_evidence_ids"], f"{path}.blocked_evidence_ids")
    _text(record["status"], f"{path}.status")
    _timestamp(record["last_saved_at"], f"{path}.last_saved_at")
    if _number(record["last_evaluated_at"], path) < 0:
        _fail(path, "evaluation clock is negative")
    if "registry_identity" in record and not isinstance(
        record["registry_identity"], str
    ):
        _fail(path, "registry identity is not text")
    if "applied_without_incident" in record:
        _boolean(record["applied_without_incident"], path)
    if "barrier_until" in record:
        _number(record["barrier_until"], f"{path}.barrier_until")
    if record["source_epoch"] and _timestamp(record.get("barrier_until"), path) < (
        record["source_fences"][-1]["at"] + BARRIER_SECONDS
    ):
        _fail(path, "smoothing barrier does not cover the source transition")
    _history(record, original, fingerprints, path)
    _samples(record, fingerprints, path)
    _references(record["references"], fingerprints, f"{path}.references")
    _pending(record, original, fingerprints, path)
    _attempts(record, original, path)
    _decision_replay(record, path)


def _witness(source: str, value: Any, records: dict) -> None:
    path = f"witnesses[{source}]"
    observed = _mapping(value, path)
    _required(
        observed,
        {
            "baseline_dry",
            "baseline_wet",
            "baseline_fingerprint",
            "source_epoch",
            "source_fingerprint",
            "revision",
            "last_event_id",
            "last_applied_at",
            "decision_fence_at",
        },
        path,
    )
    if source not in records:
        _fail(path, "previously witnessed source is missing from the Store")
    record = records[source]
    baseline = record["baseline"]
    if (
        _number(observed["baseline_dry"], path) != baseline["dry"]
        or _number(observed["baseline_wet"], path) != baseline["wet"]
        or observed["baseline_fingerprint"] != baseline["source_fingerprint"]
    ):
        _fail(path, "baseline was changed after independent enrollment")
    epoch = _integer(observed["source_epoch"], path)
    revision = _integer(observed["revision"], path)
    if epoch > record["source_epoch"] or revision > record["revision"]:
        _fail(path, "Store state was rolled back behind the care witness")
    if observed["source_fingerprint"] != record["source_fences"][epoch]["fingerprint"]:
        _fail(path, "witness source epoch is absent from the fence history")
    if revision == 0:
        if (
            observed["last_event_id"] is not None
            or observed["last_applied_at"] is not None
        ):
            _fail(path, "initial witness claims an actual effect")
    else:
        event = record["history"][revision - 1]
        if observed["last_event_id"] != event["event_id"] or _timestamp(
            observed["last_applied_at"], path
        ) != _event_time(event):
            _fail(path, "witness event or cooldown clock contradicts its revision")
    _witness_decision_fence(observed, record, path)


def _witness_decision_fence(observed: dict, record: dict, path: str) -> None:
    fence = observed["decision_fence_at"]
    actual = observed["last_applied_at"]
    if fence is None:
        if actual is not None:
            _fail(path, "witness decision fence omits its actual effect")
        return
    fence = _timestamp(fence, f"{path}.decision_fence_at")
    if actual is not None and fence < actual:
        _fail(path, "witness decision fence predates its actual effect")
    resolutions = [
        attempt["resolved_at"]
        for attempt in record.get("attempts", [])
        if attempt["outcome"] == "interrupted_unknown"
    ]
    primary = _decision_cutoff(record.get("last_applied_at"), resolutions, None)
    if primary is None or primary < fence:
        _fail(path, "Store decision fence was rolled back behind the care witness")


def validate_storage(
    data: Any, configured: dict[str, dict], witnesses: dict[str, dict]
) -> dict:
    """Return intact trusted data, or raise without repairing the caller's evidence."""
    _mapping(configured, "configured")
    _mapping(witnesses, "witnesses")
    for source, options in configured.items():
        _text(source, "configured source")
        _originals(options, f"configured[{source}]")
    if data is None:
        if witnesses:
            _fail("Store", "missing_store: prior enrollment is independently witnessed")
        return {
            "format_version": FORMAT_VERSION,
            "initialized_sources": [],
            "records": {},
        }
    envelope = _mapping(data, "Store")
    if set(envelope) != {"format_version", "initialized_sources", "records"}:
        _fail("Store", "missing or unknown envelope fields")
    if _integer(envelope["format_version"], "format_version") != FORMAT_VERSION:
        _fail("Store", "unsupported format version")
    sources = _strings(envelope["initialized_sources"], "initialized_sources")
    records = _mapping(envelope["records"], "records")
    if set(sources) != set(records):
        _fail("Store", "enrollment manifest and saved records disagree")
    for source, value in records.items():
        _text(source, "record source")
        _record(source, value, configured.get(source))
    for source, value in witnesses.items():
        _text(source, "witness source")
        _witness(source, value, records)
    return envelope
