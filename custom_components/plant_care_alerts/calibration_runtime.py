"""Persist raw evidence and audit calibration separately from plant-care estimates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from .calibration import (
    CalibrationState,
    PhysicalSample,
    QualifiedWetReference,
    evaluate_calibration,
)
from .calibration_storage import (
    source_fingerprint as epoch_fingerprint,
)
from .calibration_storage import (
    validate_storage,
)
from .reliability import number

_LOGGER = logging.getLogger(__name__)
BARRIER_SECONDS = 1800
HISTORY_SECONDS = 90 * 86400
SAVE_SECONDS = 1800
EVALUATE_SECONDS = 3600
ADC_MAX = 4095
MAX_FUTURE_SECONDS = 60
MAX_REFERENCE_IDS = 256
INDEX_MAX = 100
UNCHANGED_SECONDS = 43200
CURRENT_MAX_AGE = 1200
HOLDOUT_MAX_AGE = 86400


@dataclass(frozen=True)
class RuntimeHooks:
    """Supply HA effects and current preflight observations at the boundary."""

    context_factory: Callable[[], Any] | None = None
    log_event: Callable[[dict], None] | None = None
    clock: Callable[[], float] = time.time
    quality_provider: Callable[[str, float], dict] | None = None
    identity_provider: Callable[[str], str] | None = None
    write_ready: Callable[[], bool] | None = None


def sample_time(value: Any) -> float | None:
    """Accept explicit UTC timestamps without guessing a timezone."""
    parsed = number(value)
    if parsed is not None:
        return parsed if parsed > 0 else None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.timestamp()
    return None


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _within_invalid_interval(stamp: float, quality: dict, now: float) -> bool:
    return any(
        start <= stamp <= (end if end is not None else now)
        for start, end in quality.get("reliability_invalid_intervals") or []
    )


def _valid_decision_fence(value: Any) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and number(value) is not None
        and value > 0
    )


def _merge_witness(prior: dict, witness: dict) -> dict:
    """Retain actual progress and the independently durable decision cutoff."""
    strongest = max(
        (prior, witness),
        key=lambda item: (item.get("source_epoch", -1), item.get("revision", -1)),
    )
    merged = dict(strongest)
    field = "decision_fence_at"
    if field not in prior or field not in witness:
        merged.pop(field, None)
        return merged
    values = (prior[field], witness[field])
    invalid = [value for value in values if not _valid_decision_fence(value)]
    merged[field] = (
        invalid[0]
        if invalid
        else max((value for value in values if value is not None), default=None)
    )
    return merged


def collect_witnesses(records: dict) -> dict:
    """Merge care entries without losing unknown-attempt fences or repairing damage."""
    result = {}
    for record in records.values():
        for source, witness in record.get("calibration_witnesses", {}).items():
            prior = result.get(source)
            result[source] = (
                _merge_witness(prior, witness) if prior is not None else dict(witness)
            )
    return result


def _next_causal_event(events: list[dict], current: list) -> dict | None:
    at = min(event["at"] for event in events)
    matches = [
        event
        for event in events
        if event["at"] == at
        and current[0 if event["endpoint"] == "dry" else 1] == event["before"]
    ]
    return matches[0] if len(matches) == 1 else None


def _causal_events(record: dict, events: list[dict]) -> list[dict] | None:
    """Order known transitions without guessing between indistinguishable histories."""
    remaining, ordered = list(events), []
    current = list(record["current"])
    earliest = max(
        record.get("last_applied_at") or record["baseline"]["created_at"],
        record["source_fences"][-1]["at"],
    )
    while remaining:
        event = _next_causal_event(remaining, current)
        if event is None or event["at"] < earliest:
            return None
        current[0 if event["endpoint"] == "dry" else 1] = event["after"]
        ordered.append(event)
        remaining.remove(event)
    return ordered


class CalibrationRuntime:
    """Own helper transactions and an append-only journal; never request a probe."""

    def __init__(
        self,
        hass: Any,
        store: Any,
        config: dict,
        *,
        hooks: RuntimeHooks | None = None,
    ) -> None:
        """Use only HA's state, service and Store boundaries."""
        self.hass = hass
        self.store = store
        self.config = config
        self.records: dict[str, dict] = {}
        self._envelope = {
            "format_version": 1,
            "initialized_sources": [],
            "records": self.records,
        }
        self._durable_witnesses: dict[str, dict] = {}
        self.bindings: dict[str, str] = {}
        self._events: dict[str, list] = {}
        self._generations: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._storage_ready = True
        callbacks = hooks or RuntimeHooks()
        self._context_factory = callbacks.context_factory
        self._log_event = callbacks.log_event
        self._clock = callbacks.clock
        self._quality_provider = callbacks.quality_provider
        self._identity_provider = callbacks.identity_provider
        self._write_ready = callbacks.write_ready
        self._stopping = False
        self._quality: dict[str, dict] = {}
        self._write_checks: dict[str, tuple] = {}
        self._disabled_reason: str | None = None

    async def async_load(self, witnesses: dict | None = None) -> None:
        """Reject missing or inconsistent history instead of recreating its baseline."""
        try:
            data = await self.store.async_load()
            self._envelope = validate_storage(
                data, self._configured_originals(), witnesses or {}
            )
            self.records = self._envelope["records"]
            self._durable_witnesses = self._witness_snapshot()
        except Exception:
            self._storage_ready = False
            _LOGGER.exception(
                "Calibration history unavailable; automatic writes disabled"
            )

    def _configured_originals(self) -> dict:
        return {
            source: {
                "original_dry": options["original_dry"],
                "original_wet": options["original_wet"],
            }
            for source, options in self.config.get("automatic_calibration", {}).items()
            if options.get("enabled", False)
        }

    def _witness_snapshot(self) -> dict:
        return {
            source: {
                "baseline_dry": record["baseline"]["dry"],
                "baseline_wet": record["baseline"]["wet"],
                "baseline_fingerprint": record["baseline"]["source_fingerprint"],
                "source_epoch": record["source_epoch"],
                "source_fingerprint": record["source_fingerprint"],
                "revision": record["revision"],
                "last_event_id": record["history"][-1]["event_id"]
                if record["history"]
                else None,
                "last_applied_at": record.get("last_applied_at"),
                "decision_fence_at": self._decision_time_fence(record),
            }
            for source, record in self.records.items()
        }

    def witnesses(self) -> dict:
        """Return only evidence whose calibration Store write completed."""
        return {
            source: dict(value) for source, value in self._durable_witnesses.items()
        }

    async def async_save(self) -> bool:
        """Commit a consistent envelope before any helper effect is authorized."""
        if not self._storage_ready:
            return False
        try:
            validate_storage(
                self._envelope, self._configured_originals(), self._durable_witnesses
            )
            await self.store.async_save(self._envelope)
            self._durable_witnesses = self._witness_snapshot()
        except Exception:
            self._storage_ready = False
            _LOGGER.exception("Unable to persist calibration history; writes disabled")
            return False
        return True

    def _options(self, source: str) -> dict:
        return self.config.get("automatic_calibration", {}).get(source, {})

    def _record(self, entry_id: str) -> dict | None:
        return self.records.get(self.bindings.get(entry_id, ""))

    @property
    def watched_entities(self) -> set[str]:
        """Subscribe to configured helpers and references without discovery guesses."""
        entities = set()
        for source in self.config.get("automatic_calibration", {}):
            entities.update(
                self.config.get("calibration_sources", {}).get(source, {}).values()
            )
            reference = self._options(source).get("reference_entity")
            if reference:
                entities.add(reference)
        return entities

    def _endpoints(self, source: str) -> tuple[float, float] | None:
        ids = self.config.get("calibration_sources", {}).get(source, {})
        states = [self.hass.states.get(ids.get(key)) for key in ("dry", "wet")]
        values = [
            number(state.state) if state is not None else None for state in states
        ]
        return tuple(values) if all(value is not None for value in values) else None

    def _raw_state(self, source: str) -> Any:
        raw_id = self.config.get("freshness_sources", {}).get(source)
        return self.hass.states.get(raw_id) if raw_id else None

    def _fingerprint(self, source: str, registry_identity: str) -> str | None:
        raw = self._raw_state(source)
        if raw is None or raw.attributes.get("physical_pin") is None:
            return None
        return _digest(
            {
                "source": source,
                "raw": self.config.get("freshness_sources", {}).get(source),
                "registry": self._identity_provider(source)
                if self._identity_provider
                else registry_identity,
                "physical_pin": raw.attributes["physical_pin"],
                "source_revision": self._options(source).get("source_revision"),
            }
        )

    def _source_matches(self, record: dict) -> bool:
        """Do not grant old evidence the identity observed after an awaited refresh."""
        return record["identity_fingerprint"] == self._fingerprint(
            record["source"], record.get("registry_identity", "")
        )

    @staticmethod
    def _new_record(
        source: str, endpoints: tuple[float, float], identity: str, now: float
    ) -> dict:
        fingerprint = epoch_fingerprint(identity, 0)
        return {
            "source": source,
            "identity_fingerprint": identity,
            "source_epoch": 0,
            "source_fences": [
                {
                    "epoch": 0,
                    "identity": identity,
                    "fingerprint": fingerprint,
                    "at": now,
                }
            ],
            "baseline": {
                "dry": endpoints[0],
                "wet": endpoints[1],
                "source_fingerprint": fingerprint,
                "created_at": now,
                "provenance": "configured_original",
            },
            "current": list(endpoints),
            "previous": list(endpoints),
            "source_fingerprint": fingerprint,
            "revision": 0,
            "verified_revision": None,
            "last_applied_at": None,
            "last_evidence_id": None,
            "previous_wet": None,
            "blocked_evidence_ids": [],
            "history": [],
            "attempts": [],
            "samples": [],
            "references": [],
            "pending": None,
            "status": "awaiting_reference",
            "last_saved_at": now,
            "last_evaluated_at": 0,
        }

    @staticmethod
    def _helper_change(event: Any, endpoint: str) -> dict | None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is None or new is None:
            return None
        before, after = number(old.state), number(new.state)
        at = sample_time(new.last_updated)
        if at is None or before is None or after is None or before == after:
            return None
        return {
            "endpoint": endpoint,
            "before": before,
            "after": after,
            "at": at,
            "context_id": getattr(event.context, "id", None),
            "event_id": _digest([event.data.get("entity_id"), before, after, at]),
        }

    def note_helper_event(self, event: Any) -> None:
        """Fence every helper report and retain every actual old/new value."""
        entity_id = event.data.get("entity_id")
        for source in self.config.get("automatic_calibration", {}):
            ids = self.config.get("calibration_sources", {}).get(source, {})
            endpoint = next(
                (key for key, entity in ids.items() if entity == entity_id), None
            )
            if endpoint is None:
                continue
            self._generations[source] = self._generations.get(source, 0) + 1
            change = self._helper_change(event, endpoint)
            if change is not None:
                self._events.setdefault(source, []).append(change)

    @staticmethod
    def _change(
        record: dict, endpoints: list[float], at: float, kind: str, **evidence: Any
    ) -> dict | None:
        if record["current"] == endpoints:
            return None
        previous = list(record["current"])
        event_id = evidence.pop(
            "event_id", _digest([record["source"], previous, endpoints, at, kind])
        )
        if any(event["event_id"] == event_id for event in record["history"]):
            return None
        record["revision"] += 1
        event = {
            "event_id": event_id,
            "source": record["source"],
            "at": None if kind == "external_discovered" else at,
            "detected_at": evidence.pop("detected_at", at),
            "kind": kind,
            "old_dry": previous[0],
            "old_wet": previous[1],
            "new_dry": endpoints[0],
            "new_wet": endpoints[1],
            "revision": record["revision"],
            "source_fingerprint": record["source_fingerprint"],
            **evidence,
        }
        record["history"].append(event)
        record.update(
            current=endpoints,
            previous=previous,
            last_applied_at=at,
            effective_at=at,
            barrier_until=at + BARRIER_SECONDS,
            verified_revision=None,
            previous_wet=None,
        )
        return event

    def _manual_events(self, record: dict, now: float) -> list[dict]:
        changes = []
        pending = record.get("pending") or {}
        for event in self._events.pop(record["source"], []):
            if (
                pending.get("context_id")
                and event["context_id"] == pending["context_id"]
            ):
                continue
            if event["at"] > now + MAX_FUTURE_SECONDS:
                continue
            endpoints = list(record["current"])
            endpoints[0 if event["endpoint"] == "dry" else 1] = event["after"]
            change = self._change(
                record,
                endpoints,
                event["at"],
                "manual",
                event_id=event["event_id"],
                observed_old_value=event["before"],
                changed_endpoint=event["endpoint"],
                detected_at=now,
            )
            if change:
                changes.append(change)
        return changes

    def _base_status(self, record: dict) -> str:
        baseline = record["baseline"]
        if record["source_fingerprint"] != baseline["source_fingerprint"]:
            return "source_changed"
        if record["current"][0] != baseline["dry"]:
            return "dry_changed"
        span = baseline["wet"] - baseline["dry"]
        if abs(record["current"][1] - baseline["wet"]) > span * 0.05:
            return "outside_baseline_envelope"
        return "awaiting_reference"

    async def async_prepare(
        self, entry_id: str, source: str, now: float, registry_identity: str = ""
    ) -> None:
        """Reconcile helper history before evaluating a derived moisture reading."""
        options = self._options(source)
        if not options.get("enabled", False):
            return
        self.bindings[entry_id] = source
        async with self._lock:
            endpoints = self._endpoints(source)
            fingerprint = self._fingerprint(source, registry_identity)
            if endpoints is None or fingerprint is None or not self._storage_ready:
                return
            record = self.records.get(source)
            changed = record is None
            if record is None:
                original = self._configured_originals()[source]
                baseline = (original["original_dry"], original["original_wet"])
                record = self._new_record(source, baseline, fingerprint, now)
                self.records[source] = record
                self._envelope["initialized_sources"].append(source)
            if record["identity_fingerprint"] != fingerprint:
                self._advance_source(record, fingerprint, now)
                changed = True
            record["registry_identity"] = registry_identity
            changed = changed or bool(record.get("pending"))
            changes = self._reconcile_pending(record, endpoints, now)
            changes.extend(self._manual_events(record, now))
            discovered = self._change(
                record, list(endpoints), now, "external_discovered"
            )
            if discovered:
                changes.append(discovered)
            base_status = self._base_status(record)
            if base_status != "awaiting_reference" or not options.get(
                "reference_entity"
            ):
                record["status"] = base_status
            if (changed or changes) and await self.async_save() and self._log_event:
                for event in changes:
                    self._log_event(event)

    def _advance_source(self, record: dict, identity: str, now: float) -> None:
        """Fence each identity transition, including a return to an earlier source."""
        record["source_epoch"] += 1
        fingerprint = epoch_fingerprint(identity, record["source_epoch"])
        record["identity_fingerprint"] = identity
        record["source_fingerprint"] = fingerprint
        record["source_fences"].append(
            {
                "epoch": record["source_epoch"],
                "identity": identity,
                "fingerprint": fingerprint,
                "at": now,
            }
        )
        record["barrier_until"] = now + BARRIER_SECONDS
        record["verified_revision"] = None
        record["previous_wet"] = None
        record["last_evaluated_at"] = 0

    def _physical_values(
        self, record: dict, state: Any, now: float
    ) -> tuple[float, float] | None:
        current = self._raw_state(record["source"])
        if not self._source_matches(record):
            record["status"] = "source_changed"
            return None
        if state is None or current is None:
            return None
        stamp = sample_time(state.attributes.get("sampled_at"))
        raw = number(state.state)
        if (
            stamp is None
            or raw is None
            or not record["source_fences"][-1]["at"] <= stamp <= now
            or state.attributes.get("physical_pin")
            != current.attributes.get("physical_pin")
        ):
            return None
        return stamp, raw

    @staticmethod
    def _quarantine_samples(
        record: dict, quality: dict, now: float, *, rollback: bool
    ) -> None:
        for retained in record["samples"]:
            if _within_invalid_interval(retained["sampled_at"], quality, now):
                retained["reliable"] = False
                if not rollback:
                    retained["rollback_eligible"] = False

    @staticmethod
    def _adc_in_range(raw: float, burst: list) -> bool:
        """Keep ADC rail policy in the engine; bound storage trust inclusively."""
        return 0 <= raw <= ADC_MAX and all(
            (value := number(item)) is not None and 0 <= value <= ADC_MAX
            for item in burst
        )

    @staticmethod
    def _matching_sample(points: list, stamp: float, fingerprint: str) -> dict | None:
        return next(
            (
                point
                for point in reversed(points)
                if point["sampled_at"] == stamp
                and point["source_fingerprint"] == fingerprint
            ),
            None,
        )

    def _append_sample(
        self,
        record: dict,
        state: Any,
        quality: dict,
        now: float,
        *,
        historical: bool = False,
    ) -> bool:
        physical = self._physical_values(record, state, now)
        if physical is None:
            return False
        stamp, raw = physical
        points = record["samples"]
        fingerprint = record["source_fingerprint"]
        burst = state.attributes.get("samples", [])
        burst = list(burst) if isinstance(burst, (list, tuple)) else []
        in_range = self._adc_in_range(raw, burst)
        duplicate = self._matching_sample(points, stamp, fingerprint)
        if duplicate is not None:
            duplicate["reliable"] = (
                duplicate["reliable"] and duplicate["raw"] == raw and in_range
            )
            duplicate["rollback_eligible"] = duplicate["rollback_eligible"] and in_range
            return False
        rollback = self._rollback_quality(record, quality, now)
        point = {
            "sampled_at": stamp,
            "raw": raw,
            "burst": burst,
            "source_fingerprint": fingerprint,
            "reliable": in_range
            and (historical or quality.get("reliability_valid") is True)
            and not _within_invalid_interval(stamp, quality, now),
            "rollback_eligible": in_range
            and rollback
            and stamp > (record.get("effective_at") or now),
        }
        record["samples"] = [
            p
            for p in points
            if now - HISTORY_SECONDS <= p["sampled_at"] <= now + MAX_FUTURE_SECONDS
        ] + [point]
        self._quarantine_samples(record, quality, now, rollback=rollback)
        return True

    def seed_history(
        self, entry_id: str, states: list, quality: dict, now: float
    ) -> None:
        """Accumulate actual Recorder bursts without rewriting their timestamps."""
        record = self._record(entry_id)
        if record is None:
            return
        for state in states:
            self._append_sample(record, state, quality, now, historical=True)

    async def async_observe(
        self, entry_id: str, quality: dict, now: float, *, allow_write: bool = False
    ) -> None:
        """Observe scheduled measurements only; no fresh endpoint or model call."""
        async with self._lock:
            record = self._record(entry_id)
            if record is None:
                return
            self._quality[entry_id] = quality
            self._append_sample(record, self._raw_state(record["source"]), quality, now)
            if self._storage_ready and not self._disabled_reason and allow_write:
                await self._evaluate(entry_id, record, quality, now)
            if now - record.get("last_saved_at", 0) >= SAVE_SECONDS:
                record["last_saved_at"] = now
                await self.async_save()

    def reading_fields(self, entry_id: str) -> dict:
        """Describe a scale transition without asserting that a sensor recovered."""
        record = self._record(entry_id)
        if record is None:
            return {}
        return {
            "calibration_revision": record["revision"],
            "calibration_current": list(record["current"]),
            "calibration_previous": list(record["previous"]),
            "calibration_effective_at": record.get("effective_at"),
            "calibration_barrier_until": record.get("barrier_until"),
            "calibration_verified_revision": record.get("verified_revision"),
        }

    def attributes(self, entry_id: str) -> dict:
        """Expose a compact complete change history, never the raw sample buffer."""
        record = self._record(entry_id)
        if record is None:
            source = self.bindings.get(entry_id)
            if source and self._options(source).get("enabled", False):
                return {
                    "calibration_enabled": True,
                    "calibration_history_available": self._storage_ready,
                    "calibration_status": self._disabled_reason
                    or (
                        "awaiting_source"
                        if self._storage_ready
                        else "storage_unavailable"
                    ),
                    "calibration_history": [],
                }
            return {}
        return {
            "calibration_enabled": True,
            "calibration_history_available": self._storage_ready,
            "calibration_status": (self._disabled_reason or record["status"])
            if self._storage_ready
            else "storage_unavailable",
            "calibration_revision": record["revision"],
            "calibration_baseline_dry": record["baseline"]["dry"],
            "calibration_baseline_wet": record["baseline"]["wet"],
            "calibration_dry_adc": record["current"][0],
            "calibration_wet_adc": record["current"][1],
            "calibration_source_fingerprint": record["source_fingerprint"],
            "calibration_last_change_at": record.get("effective_at"),
            "calibration_observations": len(record["samples"]),
            "calibration_references": len(record["references"]),
            "calibration_history": [
                {
                    key: event.get(key)
                    for key in (
                        "event_id",
                        "at",
                        "detected_at",
                        "kind",
                        "old_dry",
                        "old_wet",
                        "new_dry",
                        "new_wet",
                        "reason",
                        "revision",
                    )
                }
                for event in record["history"]
            ],
        }

    def _reference_dependencies(self, entity_id: str) -> list[str]:
        state = self.hass.states.get(entity_id)
        if state is None:
            return []
        dependencies = []
        for key in (
            "source_entity",
            "source_sensor",
            "source",
            "entity_id",
            "entities",
            "source_entities",
            "freshness_source",
        ):
            value = state.attributes.get(key)
            values = value if isinstance(value, (list, tuple)) else [value]
            dependencies.extend(v for v in values if isinstance(v, str) and "." in v)
        return dependencies

    def _independent_reference(self, source: str, entity_id: str) -> bool:
        """Reject known self-derived aliases as well as direct feedback."""
        forbidden = {
            source,
            self.config.get("freshness_sources", {}).get(source),
            *self.config.get("calibration_sources", {}).get(source, {}).values(),
        }
        pending, seen = [entity_id], set()
        while pending:
            current = pending.pop()
            if current in forbidden:
                return False
            if current in seen:
                continue
            seen.add(current)
            pending.extend(self._reference_dependencies(current))
        return True

    def _reference(self, record: dict, now: float) -> QualifiedWetReference | None:
        if not self._source_matches(record):
            return None
        options = self._options(record["source"])
        entity_id, protocol = (
            options.get("reference_entity"),
            options.get("reference_protocol"),
        )
        if (
            not entity_id
            or not protocol
            or not self._independent_reference(record["source"], entity_id)
        ):
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        attrs = state.attributes
        start, end = (
            sample_time(attrs.get("start_at")),
            sample_time(attrs.get("end_at")),
        )
        index = number(state.state)
        names = [attrs.get("reference_id"), attrs.get("cycle_id")]
        if (
            start is None
            or end is None
            or not max(now - HISTORY_SECONDS, record["source_fences"][-1]["at"])
            <= start
            < end
            <= now
            or index is None
            or not 0 < index <= INDEX_MAX
            or not all(
                isinstance(value, str) and 0 < len(value.strip()) <= MAX_REFERENCE_IDS
                for value in names
            )
            or attrs.get("protocol") != protocol
            or attrs.get("source_fingerprint") != record["source_fingerprint"]
        ):
            return None
        return QualifiedWetReference(
            names[0],
            names[1],
            start,
            end,
            record["source_fingerprint"],
            entity_id,
            protocol,
            index,
        )

    def _ingest_reference(self, record: dict, now: float) -> bool:
        reference = self._reference(record, now)
        if reference is None:
            return False
        value = asdict(reference)
        references = [
            r for r in record["references"] if r["end_at"] >= now - HISTORY_SECONDS
        ]
        if value in references:
            return False
        if len(references) >= MAX_REFERENCE_IDS:
            record["reference_limit"] = True
            record["status"] = "evidence_limit"
            return False
        record["references"] = [*references, value]
        # Conflicting IDs remain visible to the pure engine and cannot be replaced.
        record["last_evaluated_at"] = 0
        return True

    async def async_record_reference(self, entry_id: str, now: float) -> bool:
        """Read only the configured qualified entity; callers supply no evidence."""
        async with self._lock:
            record = self._record(entry_id)
            if record is None or not self._storage_ready:
                return False
            accepted = self._ingest_reference(record, now)
            if accepted:
                return await self.async_save()
            return False

    @staticmethod
    def _rollback_quality(record: dict, quality: dict, now: float) -> bool:
        """Allow a new derived saturation to justify undoing our verified step."""
        applied = number(record.get("effective_at"))
        if (
            applied is None
            or record.get("previous_wet") is None
            or record.get("verified_revision") != record["revision"]
            or not record.get("last_evidence_id")
            or not record.get("applied_without_incident")
        ):
            return False
        if quality.get("reliability_valid") is True:
            return True
        cause = quality.get("reliability_reason")
        incident = number(quality.get("reliability_incident_started_at"))
        if cause == "calibrating" and incident is None:
            return True
        raw_changed = number(quality.get("last_source_change_at"))
        return (
            cause == "saturated"
            and quality.get("reliability_incident_reason") == "saturated"
            and incident is not None
            and applied < incident <= now
            and raw_changed is not None
            and 0 <= now - raw_changed < UNCHANGED_SECONDS
        )

    def _current_quality(self, entry_id: str, now: float) -> dict:
        if self._quality_provider:
            return self._quality_provider(entry_id, now)
        return self._quality.get(entry_id, {})

    @staticmethod
    def _decision_time_fence(record: dict) -> float | None:
        """Treat an unknown effect conservatively without inventing an actual change."""
        confirmed = number(record.get("last_applied_at"))
        fences = [confirmed] if confirmed is not None else []
        fences.extend(
            attempt["resolved_at"]
            for attempt in record.get("attempts", [])
            if attempt["outcome"] == "interrupted_unknown"
        )
        return max(fences, default=None)

    def _engine_state(self, record: dict) -> CalibrationState:
        source = record["source"]
        wet_id = self.config["calibration_sources"][source]["wet"]
        wet_state = self.hass.states.get(wet_id)
        baseline = record["baseline"]
        return CalibrationState(
            original_dry=baseline["dry"],
            original_wet=baseline["wet"],
            current_wet=record["current"][1],
            revision=record["revision"],
            last_applied_at=self._decision_time_fence(record),
            last_evidence_id=record.get("last_evidence_id"),
            previous_wet=record.get("previous_wet"),
            source_fingerprint=record["source_fingerprint"],
            baseline_source_fingerprint=baseline["source_fingerprint"],
            blocked_evidence_ids=tuple(record.get("blocked_evidence_ids", [])),
            current_dry=record["current"][0],
            wet_step=number(wet_state.attributes.get("step")) or 1.0,
        )

    @staticmethod
    def _engine_samples(
        record: dict, *, rollback: bool = False
    ) -> tuple[PhysicalSample, ...]:
        return tuple(
            PhysicalSample(
                p["sampled_at"],
                p["raw"],
                tuple(p["burst"]),
                p["source_fingerprint"],
                p["reliable"] or (rollback and p.get("rollback_eligible") is True),
            )
            for p in record["samples"]
        )

    def _raw_token(self, source: str) -> tuple | None:
        state = self._raw_state(source)
        if state is None:
            return None
        burst = state.attributes.get("samples")
        return (
            sample_time(state.attributes.get("sampled_at")),
            number(state.state),
            tuple(burst) if isinstance(burst, (list, tuple)) else (),
            state.attributes.get("physical_pin"),
        )

    def _cas_token(self, record: dict, now: float) -> tuple:
        return (
            record["source_epoch"],
            record["source_fingerprint"],
            record["identity_fingerprint"],
            record["revision"],
            tuple(record["current"]),
            self._endpoints(record["source"]),
            self._fingerprint(record["source"], record.get("registry_identity", "")),
            self._raw_token(record["source"]),
            self._reference(record, now),
            self._generations.get(record["source"], 0),
        )

    def _write_state_stable(self, record: dict, token: tuple, now: float) -> bool:
        if self._stopping or (
            self._write_ready is not None and not self._write_ready()
        ):
            return False
        return (
            self._source_matches(record)
            and token == self._cas_token(record, now)
            and not record.get("reference_limit")
            and self._endpoints(record["source"]) == tuple(record["current"])
            and not self._events.get(record["source"])
        )

    def _decision_quality(
        self, entry_id: str, record: dict, decision: Any, now: float
    ) -> bool:
        fresh = self._current_quality(entry_id, now)
        if decision.status == "propose":
            return fresh.get("reliability_valid") is True
        return decision.status == "rollback" and (
            fresh.get("reliability_valid") is True
            or self._rollback_quality(record, fresh, now)
        )

    def _write_allowed(
        self, entry_id: str, record: dict, decision: Any, token: tuple, now: float
    ) -> bool:
        if not self._write_state_stable(record, token, now):
            return False
        raw = self._raw_token(record["source"])
        if raw is None or raw[0] is None or not 0 <= now - raw[0] <= CURRENT_MAX_AGE:
            return False
        if not self._decision_quality(entry_id, record, decision, now):
            return False
        referenced = [
            r
            for r in record["references"]
            if r["reference_id"] in decision.reference_ids
        ]
        return (
            bool(referenced)
            and now - max(r["end_at"] for r in referenced) <= HOLDOUT_MAX_AGE
        )

    async def _prepare_evaluation(self, record: dict, now: float) -> bool:
        if not self._source_matches(record):
            record["status"] = "source_changed"
            return False
        options = self._options(record["source"])
        if not options.get("reference_entity") or not options.get("reference_protocol"):
            record["status"] = self._base_status(record)
            return False
        if self._base_status(record) != "awaiting_reference" or record.get("pending"):
            return False
        if self._events.get(record["source"]) or self._endpoints(
            record["source"]
        ) != tuple(record["current"]):
            record["status"] = "concurrent_change"
            return False
        if self._reference(record, now) is None:
            record["status"] = "invalid_reference"
            return False
        saved = not self._ingest_reference(record, now) or await self.async_save()
        return saved and now - record.get("last_evaluated_at", 0) >= EVALUATE_SECONDS

    async def _evaluate(
        self, entry_id: str, record: dict, quality: dict, now: float
    ) -> None:
        if not await self._prepare_evaluation(record, now):
            return
        if not self._source_matches(record):
            record["status"] = "source_changed"
            return
        options = self._options(record["source"])
        references = tuple(
            QualifiedWetReference(**r)
            for r in record["references"]
            if r["reference_source"] == options["reference_entity"]
            and r["protocol"] == options["reference_protocol"]
        )
        state = self._engine_state(record)
        samples = self._engine_samples(record)
        rollback = (
            self._engine_samples(record, rollback=True)
            if self._rollback_quality(record, quality, now)
            else None
        )
        token = self._cas_token(record, now)
        decision = await self.hass.async_add_executor_job(
            _evaluate_snapshots, samples, rollback, references, state, now
        )
        checked_at = self._clock()
        if token != self._cas_token(record, checked_at):
            record["status"] = "concurrent_change"
            return
        record["last_evaluated_at"] = checked_at
        record["status"] = decision.reason
        if decision.status in ("propose", "rollback") and self._write_allowed(
            entry_id, record, decision, token, checked_at
        ):
            await self._apply(entry_id, record, decision, token, checked_at)

    def stop_writes(self) -> None:
        """Disarm pending effects before shutdown persistence begins."""
        self._stopping = True
        self._write_checks.clear()

    def inhibit_writes(self, reason: str) -> None:
        """Keep observation active when the owned native write path is unavailable."""
        self._disabled_reason = reason

    async def async_guarded_apply(self, entity: Any, call: Any) -> dict:
        """Check expectations after permissions and immediately before native effect."""
        transaction = call.data.get("transaction_id")
        check = self._write_checks.get(transaction)
        if check is None or call.context.id != transaction or self._disabled_reason:
            return {"applied": False}
        entry_id, decision, token = check
        record = self._record(entry_id)
        pending = record.get("pending") if record else None
        if (
            not pending
            or pending["context_id"] != transaction
            or self.config["calibration_sources"][record["source"]]["wet"]
            != entity.entity_id
            or record["revision"] != pending["expected_revision"]
            or record["source_epoch"] != pending["source_epoch"]
            or number(entity.native_value) != pending["expected"][1]
            or not self._write_allowed(entry_id, record, decision, token, self._clock())
        ):
            return {"applied": False}
        # Native InputNumber.async_set_native_value in the supported HA version
        # validates and writes without yielding. Native CI protects this boundary.
        await entity.async_set_native_value(pending["target"][1])
        return {"applied": True}

    async def _apply(
        self, entry_id: str, record: dict, decision: Any, token: tuple, now: float
    ) -> None:
        if self._context_factory is None:
            record["status"] = "context_unavailable"
            return
        context = self._context_factory()
        quality = self._current_quality(entry_id, now)
        record["pending"] = {
            "expected": list(record["current"]),
            "target": [record["current"][0], decision.proposed_wet],
            "expected_revision": record["revision"],
            "source_epoch": record["source_epoch"],
            "source_fingerprint": record["source_fingerprint"],
            "kind": "rollback" if decision.status == "rollback" else "automatic",
            "context_id": context.id,
            "started_at": now,
            "reason": decision.reason,
            "evidence_id": decision.evidence_id,
            "reference_ids": list(decision.reference_ids),
            "references": [
                r
                for r in record["references"]
                if r["reference_id"] in decision.reference_ids
            ],
            "diagnostics": json.loads(json.dumps(decision.diagnostics)),
            "without_incident": quality.get("reliability_valid") is True
            and quality.get("reliability_incident_started_at") is None,
        }
        record["status"] = "applying"
        if not await self.async_save():
            return
        if not self._write_allowed(entry_id, record, decision, token, self._clock()):
            record["pending"] = None
            record["status"] = "concurrent_change"
            await self.async_save()
            return
        confirmed = await self._call_guarded_service(
            entry_id, record, decision, token, context
        )
        await self._finish_apply(record, confirmed=confirmed)

    async def _call_guarded_service(
        self, entry_id: str, record: dict, decision: Any, token: tuple, context: Any
    ) -> bool | None:
        confirmed = None
        wet_id = self.config["calibration_sources"][record["source"]]["wet"]
        self._write_checks[context.id] = (entry_id, decision, token)
        try:
            response = await self.hass.services.async_call(
                "plant_care_alerts",
                "apply_calibration",
                {"entity_id": wet_id, "transaction_id": context.id},
                blocking=True,
                context=context,
                return_response=True,
            )
            outcome = response.get(wet_id, {}).get("applied") if response else None
            confirmed = outcome if isinstance(outcome, bool) else None
        except Exception:
            _LOGGER.exception("Calibration helper call did not confirm completion")
        finally:
            self._write_checks.pop(context.id, None)
        return confirmed

    async def _finish_apply(self, record: dict, *, confirmed: bool | None) -> None:
        endpoints = self._endpoints(record["source"])
        if endpoints is None:
            return
        finished = self._clock()
        changes = self._reconcile_pending(
            record, endpoints, finished, confirmed=confirmed
        )
        changes.extend(self._manual_events(record, finished))
        discovered = self._change(
            record, list(endpoints), finished, "external_discovered"
        )
        if discovered:
            changes.append(discovered)
        if await self.async_save() and self._log_event:
            for event in changes:
                self._log_event(event)

    def _complete_pending(
        self, record: dict, pending: dict, at: float, now: float
    ) -> dict | None:
        event = self._change(
            record,
            pending["target"],
            at,
            pending["kind"],
            detected_at=now,
            reason=pending["reason"],
            evidence_id=pending["evidence_id"],
            reference_ids=pending["reference_ids"],
            references=pending["references"],
            diagnostics=pending["diagnostics"],
        )
        if event is None:
            return None
        record["verified_revision"] = record["revision"]
        record["last_evidence_id"] = pending["evidence_id"]
        record["applied_without_incident"] = pending["without_incident"]
        if pending["kind"] == "rollback":
            blocked = pending["diagnostics"].get("block_evidence_ids", [])
            record["blocked_evidence_ids"] = list(
                dict.fromkeys([*record["blocked_evidence_ids"], *blocked])
            )
            record["status"] = "rolled_back"
        else:
            record["previous_wet"] = pending["expected"][1]
            record["status"] = "applied"
        return event

    def _block_evidence(self, record: dict, evidence_id: str | None) -> None:
        if evidence_id:
            record["blocked_evidence_ids"] = list(
                dict.fromkeys([*record["blocked_evidence_ids"], evidence_id])
            )

    def _pending_events(
        self, record: dict, pending: dict, now: float
    ) -> tuple[list, bool]:
        changes, own_seen = [], False
        observed = [
            event
            for event in self._events.pop(record["source"], [])
            if event["at"] <= now + MAX_FUTURE_SECONDS
        ]
        ordered = _causal_events(record, observed)
        if ordered is None:
            pending["observed_events"] = observed
            return changes, own_seen
        for event in ordered:
            own = (
                event["context_id"] == pending["context_id"]
                and event["endpoint"] == "wet"
                and event["after"] == pending["target"][1]
            )
            if own:
                own_seen = True
                change = self._observed_effect(record, pending, event, now)
            else:
                endpoints = list(record["current"])
                endpoints[0 if event["endpoint"] == "dry" else 1] = event["after"]
                change = self._change(
                    record,
                    endpoints,
                    event["at"],
                    "manual",
                    detected_at=now,
                    event_id=event["event_id"],
                    observed_old_value=event["before"],
                    changed_endpoint=event["endpoint"],
                )
            if change:
                changes.append(change)
        return changes, own_seen

    def _observed_effect(
        self, record: dict, pending: dict, event: dict, now: float
    ) -> dict | None:
        if (
            record["current"] == pending["expected"]
            and record["revision"] == pending["expected_revision"]
            and event["before"] == pending["expected"][1]
            and record["source_epoch"] == pending["source_epoch"]
            and record["source_fingerprint"] == pending["source_fingerprint"]
        ):
            return self._complete_pending(record, pending, event["at"], now)
        # Do not call a delayed or replaced dispatcher's outcome verified.
        self.inhibit_writes("unsupported_runtime")
        _LOGGER.error(
            "Guarded helper effect crossed an external change; writes disabled"
        )
        self._block_evidence(record, pending["evidence_id"])
        return self._change(
            record,
            [record["current"][0], event["after"]],
            event["at"],
            pending["kind"],
            detected_at=now,
            reason="concurrent_change",
            evidence_id=pending["evidence_id"],
        )

    def _record_attempt(
        self, record: dict, pending: dict, endpoints: tuple, now: float, outcome: str
    ) -> None:
        attempt = {
            "attempt_id": pending["context_id"],
            "started_at": pending["started_at"],
            "resolved_at": now,
            "outcome": outcome,
            "expected": pending["expected"],
            "target": pending["target"],
            "observed": list(endpoints),
            "evidence_id": pending["evidence_id"],
        }
        if pending.get("observed_events"):
            attempt["observed_events"] = [
                dict(event) for event in pending["observed_events"]
            ]
        attempts = record.setdefault("attempts", [])
        if not any(item["attempt_id"] == attempt["attempt_id"] for item in attempts):
            attempts.append(attempt)

    def _reconcile_pending(
        self,
        record: dict,
        endpoints: tuple,
        now: float,
        *,
        confirmed: bool | None = None,
    ) -> list[dict]:
        pending = record.get("pending")
        if not pending:
            return []
        record["pending"] = None
        changes, own_seen = self._pending_events(record, pending, now)
        if own_seen:
            return changes
        if (
            confirmed
            and not changes
            and not pending.get("observed_events")
            and list(endpoints) == pending["target"]
        ):
            event = self._complete_pending(record, pending, now, now)
            return [event] if event else []
        if confirmed is False and not pending.get("observed_events"):
            self._record_attempt(record, pending, endpoints, now, "aborted")
            record["status"] = "concurrent_change"
            return changes
        # After a crash, matching expected values cannot disprove a brief effect.
        self._record_attempt(record, pending, endpoints, now, "interrupted_unknown")
        record["status"] = "concurrent_change" if changes else "effect_unconfirmed"
        self._block_evidence(record, pending.get("evidence_id"))
        event = self._change(
            record,
            list(endpoints),
            now,
            "external_discovered",
            reason="pending_effect_reconciled",
            evidence_id=pending.get("evidence_id"),
        )
        if event:
            changes.append(event)
        return changes


def _evaluate_snapshots(
    samples: tuple,
    rollback: tuple | None,
    references: tuple,
    state: CalibrationState,
    now: float,
) -> Any:
    """Alternate acquisition evidence can only recommend undoing our last step."""
    decision = evaluate_calibration(samples, references, state, now)
    if decision.status == "observe" and rollback is not None:
        alternate = evaluate_calibration(rollback, references, state, now)
        if alternate.status == "rollback":
            return alternate
    return decision
