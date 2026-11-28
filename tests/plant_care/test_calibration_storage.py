"""Reject restored calibration state that would forget an accepted change."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from typing import Any

from calibration_fixtures import load_component_module

storage = load_component_module("calibration_storage")
SOURCE = "sensor.soil"
RECORD_PATH = f"records[{SOURCE}]"
CREATED = 1_000_000.0
APPLIED = CREATED + 16 * 86400
CONFIGURED = {SOURCE: {"original_dry": 67.0, "original_wet": 221.0}}


def fingerprint(identity: str, epoch: int) -> str:
    """Build fixture identities independently from the module being checked."""
    value = json.dumps([identity, epoch], separators=(",", ":")).encode()
    return hashlib.sha256(value).hexdigest()


def references() -> list[dict]:
    """Return four separate observed reference windows."""
    return [
        {
            "reference_id": f"reference-{index}",
            "cycle_id": f"cycle-{index}",
            "start_at": CREATED + day * 86400,
            "end_at": CREATED + day * 86400 + 3600,
            "source_fingerprint": fingerprint("probe-A", 0),
            "reference_source": "sensor.independent",
            "protocol": "gravimetric-v1",
            "reference_index": 90.0,
        }
        for index, day in enumerate((0, 7, 14, 15))
    ]


def raw_sample(
    *, raw: float = 200.0, at: float = CREATED, trusted: bool = False
) -> dict:
    """Create independent raw and burst observations for a single source epoch."""
    return {
        "sampled_at": at,
        "raw": raw,
        "burst": [raw] * 11,
        "source_fingerprint": fingerprint("probe-A", 0),
        "reliable": trusted,
        "rollback_eligible": trusted,
    }


def without_reference_snapshots(event: dict) -> dict:
    """Keep the native minimal event shape, with no qualified reference metadata."""
    for field in ("reference_ids", "references", "diagnostics"):
        del event[field]
    return event


def initial_record() -> dict:
    """Represent a registered configured original, before any helper change."""
    original = fingerprint("probe-A", 0)
    return {
        "source": SOURCE,
        "baseline": {
            "dry": 67.0,
            "wet": 221.0,
            "source_fingerprint": original,
            "created_at": CREATED,
            "provenance": "configured_original",
        },
        "current": [67.0, 221.0],
        "previous": [67.0, 221.0],
        "source_fingerprint": original,
        "identity_fingerprint": "probe-A",
        "source_epoch": 0,
        "source_fences": [
            {"epoch": 0, "identity": "probe-A", "fingerprint": original, "at": CREATED}
        ],
        "revision": 0,
        "verified_revision": None,
        "last_applied_at": None,
        "effective_at": None,
        "last_evidence_id": None,
        "previous_wet": None,
        "blocked_evidence_ids": [],
        "history": [],
        "samples": [],
        "references": [],
        "pending": None,
        "status": "awaiting_reference",
        "last_saved_at": CREATED,
        "last_evaluated_at": 0,
    }


def envelope(record: dict | None = None) -> dict:
    """Keep the enrollment manifest separate from its mutable record."""
    return {
        "format_version": 1,
        "initialized_sources": [SOURCE],
        "records": {SOURCE: initial_record() if record is None else record},
    }


def qualified_state(event: dict, previous_wet: float | None) -> dict:
    """Keep the state fields of qualified fixtures consistent with their fact."""
    return {
        "current": [event["new_dry"], event["new_wet"]],
        "previous": [event["old_dry"], event["old_wet"]],
        "revision": event["revision"],
        "verified_revision": event["revision"],
        "previous_wet": previous_wet,
        "last_applied_at": event["at"],
        "effective_at": event["at"],
        "barrier_until": event["at"] + 1800,
        "last_evidence_id": event["evidence_id"],
    }


def changed_record() -> dict:
    """Record one actual, qualified, bounded wet change."""
    record = initial_record()
    qualified = references()
    event = {
        "event_id": "actual-change-1",
        "source": SOURCE,
        "at": APPLIED,
        "detected_at": APPLIED,
        "kind": "automatic",
        "old_dry": 67.0,
        "old_wet": 221.0,
        "new_dry": 67.0,
        "new_wet": 222.0,
        "revision": 1,
        "source_fingerprint": fingerprint("probe-A", 0),
        "reason": "qualified_drift",
        "evidence_id": "accepted-evidence",
        "reference_ids": [value["reference_id"] for value in qualified],
        "references": qualified,
        "diagnostics": {},
    }
    record.update(
        **qualified_state(event, 221.0),
        references=copy.deepcopy(qualified),
        applied_without_incident=True,
        status="applied",
        history=[event],
    )
    return record


def rollback_record(*, pending: bool = False) -> dict:
    """Use one protective return fixture as a persisted intent or actual effect."""
    record = changed_record()
    reference = references()[-1]
    reference.update(
        reference_id="rollback-reference",
        cycle_id="rollback-cycle",
        start_at=APPLIED + 100,
        end_at=APPLIED + 3700,
    )
    blocked = ["accepted-evidence", "rollback-evidence"]
    event = copy.deepcopy(record["history"][0])
    event.update(
        event_id="actual-rollback-2",
        kind="rollback",
        at=APPLIED + 86400,
        detected_at=APPLIED + 86400,
        old_wet=222.0,
        new_wet=221.0,
        revision=2,
        reason="contradiction",
        evidence_id="rollback-evidence",
        reference_ids=[reference["reference_id"]],
        references=[reference],
        diagnostics={"block_evidence_ids": list(blocked)},
    )
    if pending:
        intent = pending_record()["pending"]
        intent.update(
            expected=[67.0, 222.0],
            target=[67.0, 221.0],
            expected_revision=1,
            started_at=event["at"],
            **{
                field: copy.deepcopy(event[field])
                for field in intent.keys() & event.keys()
            },
        )
        record["pending"] = intent
        return record
    record["history"].append(event)
    record.update(
        **qualified_state(event, None),
        blocked_evidence_ids=blocked,
    )
    return record


def witness(record: dict) -> dict:
    """Represent the independent care Store's previously persisted observation."""
    fences = [
        attempt["resolved_at"]
        for attempt in record.get("attempts", [])
        if attempt["outcome"] == "interrupted_unknown"
    ]
    if record["last_applied_at"] is not None:
        fences.append(record["last_applied_at"])
    return {
        "baseline_dry": record["baseline"]["dry"],
        "baseline_wet": record["baseline"]["wet"],
        "baseline_fingerprint": record["baseline"]["source_fingerprint"],
        "source_epoch": record["source_epoch"],
        "source_fingerprint": record["source_fingerprint"],
        "revision": record["revision"],
        "last_event_id": record["history"][-1]["event_id"]
        if record["history"]
        else None,
        "last_applied_at": record["last_applied_at"],
        "decision_fence_at": max(fences, default=None),
    }


def uncertain_record(record: dict, resolved: float = APPLIED + 10) -> dict:
    """Retain an unresolved effect observation without incrementing actual history."""
    record["blocked_evidence_ids"] = ["unknown-evidence"]
    record["attempts"] = [
        {
            "attempt_id": "unknown-context",
            "started_at": resolved - 1,
            "resolved_at": resolved,
            "outcome": "interrupted_unknown",
            "expected": list(record["current"]),
            "target": [record["current"][0], record["current"][1] + 1],
            "observed": list(record["current"]),
            "evidence_id": "unknown-evidence",
        }
    ]
    return record


def pending_record() -> dict:
    """Represent persisted intent with no claimed helper effect."""
    record = initial_record()
    qualified = references()
    record["references"] = copy.deepcopy(qualified)
    record["pending"] = {
        "expected": [67.0, 221.0],
        "target": [67.0, 222.0],
        "expected_revision": 0,
        "source_fingerprint": record["source_fingerprint"],
        "source_epoch": 0,
        "kind": "automatic",
        "context_id": "transaction-context",
        "started_at": APPLIED,
        "reason": "qualified_drift",
        "evidence_id": "new-evidence",
        "reference_ids": [value["reference_id"] for value in qualified],
        "references": qualified,
        "diagnostics": {},
        "without_incident": True,
    }
    return record


def returned_source(record: dict) -> dict:
    """Keep prior evidence while recording two later source transitions."""
    for epoch, identity in ((1, "probe-B"), (2, "probe-A")):
        record["source_fences"].append(
            {
                "epoch": epoch,
                "identity": identity,
                "fingerprint": fingerprint(identity, epoch),
                "at": APPLIED + epoch,
            }
        )
    record.update(
        source_epoch=2,
        identity_fingerprint="probe-A",
        source_fingerprint=fingerprint("probe-A", 2),
        barrier_until=APPLIED + 1802,
        verified_revision=None,
        previous_wet=None,
        status="source_changed",
    )
    return record


def following_record(*, pending: bool, at: float = APPLIED + 8 * 86400) -> dict:
    """Isolate persistence timing from the engine's statistical qualification."""
    record = changed_record()
    fresh = references()
    for index, reference in enumerate(fresh):
        reference.update(
            reference_id=f"fresh-reference-{index}",
            cycle_id=f"fresh-cycle-{index}",
            start_at=APPLIED + 0.1 + index * 0.2,
            end_at=APPLIED + 0.15 + index * 0.2,
        )
    common = {
        "evidence_id": "following-evidence",
        "reference_ids": [value["reference_id"] for value in fresh],
        "references": fresh,
    }
    record["references"] = [*record["references"], *copy.deepcopy(fresh)]
    if pending:
        intent = pending_record()["pending"]
        intent.update(
            expected=[67.0, 222.0],
            target=[67.0, 223.0],
            expected_revision=1,
            started_at=at,
            **common,
        )
        record["pending"] = intent
    else:
        event = copy.deepcopy(record["history"][0])
        event.update(
            event_id="actual-change-2",
            old_wet=222.0,
            new_wet=223.0,
            revision=2,
            at=at,
            detected_at=at,
            **common,
        )
        record["history"].append(event)
        record.update(qualified_state(event, 222.0))
    return record


def decision(record: dict, *, pending: bool) -> dict:
    """Return the mutable decision within this independently created fixture."""
    return record["pending"] if pending else record["history"][-1]


class StorageIntegrityTests(unittest.TestCase):
    """Exercise the pure persistence boundary without a Home Assistant runtime."""

    def setUp(self) -> None:
        self.validate = storage.validate_storage
        self.error = storage.StorageIntegrityError
        self.fingerprint = storage.source_fingerprint
        self.decision_fence = storage.decision_fence

    def accept(self, record: dict) -> dict:
        data = envelope(record)
        self.assertIs(self.validate(data, CONFIGURED, {}), data)
        return data

    def reject(
        self,
        data: Any,
        witnesses: dict | None = None,
        *,
        path: str | None = None,
        reason: str | None = None,
    ) -> None:
        before = copy.deepcopy(data)
        with self.assertRaises(self.error) as caught:
            self.validate(data, CONFIGURED, witnesses or {})
        self.assertEqual(data, before, "Rejected evidence must remain intact")
        if path is not None:
            self.assertTrue(
                str(caught.exception).startswith(f"{path}:"), caught.exception
            )
        if reason is not None:
            self.assertEqual(str(caught.exception), f"{path}: {reason}")

    def test_only_absent_store_without_witness_allows_initial_enrollment(self) -> None:
        self.assertEqual(
            self.validate(None, CONFIGURED, {}),
            {"format_version": 1, "initialized_sources": [], "records": {}},
        )

    def test_empty_mapping_is_not_initial_store(self) -> None:
        self.reject({})

    def test_missing_store_with_existing_witness_is_rejected(self) -> None:
        self.reject(None, {SOURCE: witness(initial_record())})

    def test_manifest_and_records_must_preserve_enrollment(self) -> None:
        for field in ("records", "initialized_sources"):
            with self.subTest(missing=field):
                data = envelope()
                data[field].clear()
                self.reject(data)

    def test_witness_detects_removal_of_both_manifest_and_record(self) -> None:
        self.reject(
            {"format_version": 1, "initialized_sources": [], "records": {}},
            {SOURCE: witness(initial_record())},
        )

    def test_new_configured_source_does_not_rewrite_existing_enrollment(self) -> None:
        data = envelope()
        configured = CONFIGURED | {
            "sensor.new": {"original_dry": 10.0, "original_wet": 200.0}
        }
        self.assertIs(self.validate(data, configured, {}), data)
        self.assertEqual(data["initialized_sources"], [SOURCE])

    def test_valid_state_retains_raw_observations_outside_calibration_range(
        self,
    ) -> None:
        data = envelope()
        data["records"][SOURCE]["samples"] = [raw_sample(raw=1400.0)]
        self.assertIs(self.validate(data, CONFIGURED, {}), data)

    def test_configured_originals_cannot_be_rebased_to_current_helpers(self) -> None:
        data = envelope()
        data["records"][SOURCE]["baseline"]["wet"] = 222.0
        data["records"][SOURCE]["current"][1] = 222.0
        data["records"][SOURCE]["previous"][1] = 222.0
        self.reject(data)

    def test_partial_restore_cannot_reset_revision_and_cooldown(self) -> None:
        record = changed_record()
        record.update(
            revision=0,
            last_applied_at=None,
            last_evidence_id=None,
            previous_wet=None,
            verified_revision=None,
        )
        self.reject(envelope(record))

    def test_last_change_clock_must_match_the_journal(self) -> None:
        data = envelope(changed_record())
        data["records"][SOURCE]["last_applied_at"] = CREATED
        self.reject(data)

    def test_last_used_evidence_cannot_be_forgotten(self) -> None:
        data = envelope(changed_record())
        data["records"][SOURCE]["last_evidence_id"] = None
        self.reject(data)

    def test_current_and_previous_must_match_actual_journal_values(self) -> None:
        for field in ("current", "previous"):
            with self.subTest(field=field):
                data = envelope(changed_record())
                data["records"][SOURCE][field][1] += 1
                self.reject(data)

    def test_journal_requires_contiguous_revision_and_original_start(self) -> None:
        cases = (
            (
                "revision",
                2,
                "journal revision or event identifier is duplicated or missing",
            ),
            (
                "old_wet",
                221.5,
                "journal old values do not continue the prior actual state",
            ),
        )
        for field, value, reason in cases:
            with self.subTest(field=field):
                data = envelope(changed_record())
                self.assertIs(self.validate(data, CONFIGURED, {}), data)
                data["records"][SOURCE]["history"][0][field] = value
                self.reject(data, path=f"{RECORD_PATH}.history[0]", reason=reason)

    def test_initial_record_cannot_claim_an_unjournaled_change(self) -> None:
        data = envelope()
        data["records"][SOURCE]["current"][1] = 222.0
        self.reject(data)

    def test_valid_pending_intent_does_not_claim_a_completed_effect(self) -> None:
        data = envelope(pending_record())
        self.assertIs(self.validate(data, CONFIGURED, {}), data)
        self.assertEqual(data["records"][SOURCE]["history"], [])

    def test_pending_must_match_current_revision_identity_and_source_epoch(
        self,
    ) -> None:
        for field, value in (
            ("expected", [67.0, 220.0]),
            ("expected_revision", 1),
            ("source_fingerprint", "wrong-source"),
            ("source_epoch", 1),
        ):
            with self.subTest(field=field):
                data = envelope(pending_record())
                data["records"][SOURCE]["pending"][field] = value
                self.reject(data)

    def test_pending_cannot_change_dry_or_exceed_step_and_total_bounds(self) -> None:
        for target in ([68.0, 222.0], [67.0, 223.0], [67.0, 1400.0], [67.0, 221.0]):
            with self.subTest(target=target):
                data = envelope(pending_record())
                data["records"][SOURCE]["pending"]["target"] = target
                self.reject(data)

    def test_valid_history_can_be_ahead_of_the_care_witness(self) -> None:
        data = envelope(changed_record())
        self.assertIs(
            self.validate(data, CONFIGURED, {SOURCE: witness(initial_record())}), data
        )

    def test_care_witness_prevents_history_rollback(self) -> None:
        self.reject(envelope(), {SOURCE: witness(changed_record())})

    def test_care_witness_event_and_clock_must_match_its_revision(self) -> None:
        for field, value in (
            ("last_event_id", "another-event"),
            ("last_applied_at", CREATED),
        ):
            with self.subTest(field=field):
                observed = witness(changed_record())
                observed[field] = value
                self.reject(envelope(changed_record()), {SOURCE: observed})

    def test_epoch_fence_preserves_an_a_b_a_transition(self) -> None:
        record = returned_source(initial_record())
        data = self.accept(record)
        self.assertNotEqual(
            record["source_fingerprint"], record["baseline"]["source_fingerprint"]
        )
        record["source_fingerprint"] = record["baseline"]["source_fingerprint"]
        self.reject(data)

    def test_care_witness_prevents_source_epoch_rollback(self) -> None:
        observed = witness(initial_record())
        observed.update(source_epoch=1, source_fingerprint=fingerprint("probe-B", 1))
        self.reject(envelope(), {SOURCE: observed})

    def test_structural_sample_and_reference_corruption_is_rejected(self) -> None:
        data = envelope()
        data["records"][SOURCE]["samples"] = [{"sampled_at": CREATED}]
        self.reject(data)
        data = envelope()
        data["records"][SOURCE]["references"] = references()
        data["records"][SOURCE]["references"][0]["start_at"] = float("inf")
        self.reject(data)

    def test_boolean_revision_and_nonfinite_anchors_are_not_numbers(self) -> None:
        data = envelope()
        data["records"][SOURCE]["revision"] = False
        self.reject(data)
        data = envelope()
        data["records"][SOURCE]["baseline"]["wet"] = float("inf")
        self.reject(data)

    def test_unknown_format_and_duplicate_enrollment_are_rejected(self) -> None:
        data = envelope()
        data["format_version"] = True
        self.reject(data)
        data = envelope()
        data["initialized_sources"].append(SOURCE)
        self.reject(data)

    def test_missing_required_fields_are_not_repaired(self) -> None:
        data = envelope()
        del data["records"][SOURCE]["blocked_evidence_ids"]
        self.reject(data)

    def test_manual_change_keeps_prior_evidence_without_claiming_verification(
        self,
    ) -> None:
        record = changed_record()
        record["history"].append(
            {
                "event_id": "manual-change-2",
                "source": SOURCE,
                "at": APPLIED + 100,
                "detected_at": APPLIED + 100,
                "kind": "manual",
                "old_dry": 67.0,
                "old_wet": 222.0,
                "new_dry": 67.0,
                "new_wet": 230.0,
                "revision": 2,
                "source_fingerprint": record["source_fingerprint"],
            }
        )
        record.update(
            current=[67.0, 230.0],
            previous=[67.0, 222.0],
            revision=2,
            verified_revision=None,
            previous_wet=None,
            last_applied_at=APPLIED + 100,
            effective_at=APPLIED + 100,
            barrier_until=APPLIED + 1900,
        )
        self.accept(record)

    def test_unverified_native_effect_cannot_claim_qualified_evidence(self) -> None:
        record = changed_record()
        record["history"][0]["reason"] = "concurrent_change"
        record.update(
            last_evidence_id=None,
            previous_wet=None,
            verified_revision=None,
            blocked_evidence_ids=["accepted-evidence"],
        )
        data = self.accept(record)
        record["verified_revision"] = 1
        self.reject(data)

    def test_malformed_field_types_fail_through_integrity_error(self) -> None:
        record = changed_record()
        record["history"][0]["kind"] = []
        self.reject(envelope(record), path=f"{RECORD_PATH}.history[0].kind")
        record = changed_record()
        record["references"][0]["source_fingerprint"] = {}
        self.reject(
            envelope(record), path=f"{RECORD_PATH}.references[0].source_fingerprint"
        )

    def test_unrepresentable_timestamp_fails_through_integrity_error(self) -> None:
        data = envelope()
        data["records"][SOURCE]["last_saved_at"] = 10**400
        self.reject(data, path=f"{RECORD_PATH}.last_saved_at")

    def test_rollback_cannot_lose_both_persistent_exclusions(self) -> None:
        for missing in ("accepted-evidence", "rollback-evidence"):
            with self.subTest(missing=missing):
                record = rollback_record()
                data = self.accept(record)
                record["blocked_evidence_ids"].remove(missing)
                self.assertEqual(
                    record["history"][-1]["diagnostics"]["block_evidence_ids"],
                    ["accepted-evidence", "rollback-evidence"],
                )
                self.reject(data)

    def test_rollback_diagnostics_require_each_excluded_id(self) -> None:
        for pending in (False, True):
            for missing in ("accepted-evidence", "rollback-evidence"):
                with self.subTest(pending=pending, missing=missing):
                    record = rollback_record(pending=pending)
                    data = self.accept(record)
                    decision(record, pending=pending)["diagnostics"][
                        "block_evidence_ids"
                    ].remove(missing)
                    suffix = "pending" if pending else "history[1]"
                    self.reject(
                        data,
                        path=f"{RECORD_PATH}.{suffix}.diagnostics.block_evidence_ids",
                        reason="rollback must exclude its prior and contradictory evidence",
                    )

    def test_uncertain_attempt_is_preserved_without_inventing_a_change(self) -> None:
        record = uncertain_record(initial_record(), APPLIED + 1)
        data = self.accept(record)
        self.assertEqual(record["revision"], 0)
        self.assertEqual(record["history"], [])
        record["blocked_evidence_ids"] = []
        self.reject(data)
        record["attempts"][0]["outcome"] = "aborted"
        record["attempts"][0]["observed"] = [67.0, 1400.0]
        self.assertIs(self.validate(data, CONFIGURED, {}), data)
        record["attempts"][0]["outcome"] = "applied"
        self.reject(data)

    def test_history_cannot_reuse_earlier_uncertain_evidence(self) -> None:
        uncertain = uncertain_record(initial_record(), APPLIED + 1)
        record = changed_record()
        effect = APPLIED + 16 * 86400
        event = record["history"][0]
        for reference in event["references"]:
            reference["start_at"] += APPLIED + 2 - CREATED
            reference["end_at"] += APPLIED + 2 - CREATED
        event.update(at=effect, detected_at=effect)
        record.update(
            last_applied_at=effect,
            effective_at=effect,
            barrier_until=effect + 1800,
            blocked_evidence_ids=uncertain["blocked_evidence_ids"],
            attempts=uncertain["attempts"],
        )
        data = self.accept(record)
        event["evidence_id"] = record["last_evidence_id"] = "unknown-evidence"
        self.reject(
            data,
            path=f"{RECORD_PATH}.history[0]",
            reason="completed effect reuses evidence already excluded at that time",
        )

    def test_reference_relabelling_cannot_cross_source_epoch(self) -> None:
        record = returned_source(pending_record())
        pending = record["pending"]
        pending.update(
            source_epoch=2,
            source_fingerprint=record["source_fingerprint"],
            started_at=APPLIED + 2000,
        )
        for reference in pending["references"]:
            reference["source_fingerprint"] = record["source_fingerprint"]
        self.reject(envelope(record))

    def test_stored_reference_window_starts_within_its_own_epoch(self) -> None:
        record = returned_source(initial_record())
        reference = references()[-1]
        reference["source_fingerprint"] = record["source_fingerprint"]
        record["references"] = [reference]
        data = envelope(record)
        self.reject(data)
        reference.update(start_at=APPLIED + 2, end_at=APPLIED + 3602)
        self.assertIs(self.validate(data, CONFIGURED, {}), data)

    def test_prior_epoch_history_and_references_remain_valid_after_source_return(
        self,
    ) -> None:
        data = envelope(returned_source(changed_record()))
        self.assertIs(self.validate(data, CONFIGURED, {}), data)

    def test_completed_rollback_restores_the_preceding_verified_wet(self) -> None:
        record = rollback_record()
        data = self.accept(record)
        record["history"][-1]["new_wet"] = 223.0
        record["current"][1] = 223.0
        self.reject(data)

    def test_completed_effect_cannot_use_a_future_reference(self) -> None:
        record = changed_record()
        event = record["history"][0]
        event["references"][-1].update(start_at=APPLIED - 3600, end_at=APPLIED)
        data = self.accept(record)
        event["references"][-1]["end_at"] = APPLIED + 1
        self.reject(data)

    def test_concurrent_exclusion_persists_before_pending_or_completion(self) -> None:
        for stage in ("saved", "pending", "completed"):
            with self.subTest(stage=stage):
                record = (
                    changed_record()
                    if stage == "saved"
                    else following_record(pending=stage == "pending")
                )
                record["history"][0]["reason"] = "concurrent_change"
                if stage != "completed":
                    record.update(
                        last_evidence_id=None, previous_wet=None, verified_revision=None
                    )
                if stage != "saved":
                    decision(record, pending=stage == "pending")["evidence_id"] = (
                        "accepted-evidence"
                    )
                    if stage == "completed":
                        record["last_evidence_id"] = "accepted-evidence"
                data = envelope(record)
                self.reject(data, {SOURCE: witness(record)})
                record["blocked_evidence_ids"] = ["accepted-evidence"]
                if stage == "saved":
                    self.assertIs(self.validate(data, CONFIGURED, {}), data)
                else:
                    self.reject(data)

    def test_qualified_decisions_require_fresh_reference_windows(self) -> None:
        for pending in (False, True):
            for rename in (False, True):
                with self.subTest(pending=pending, rename=rename):
                    record = following_record(pending=pending)
                    value = decision(record, pending=pending)
                    snapshots = copy.deepcopy(record["history"][0]["references"])
                    if rename:
                        for reference in snapshots:
                            reference["reference_id"] += "-renamed"
                            reference["cycle_id"] += "-renamed"
                    value.update(
                        references=snapshots,
                        reference_ids=[r["reference_id"] for r in snapshots],
                    )
                    self.reject(envelope(record), {SOURCE: witness(changed_record())})

    def test_accepted_reference_and_cycle_identity_cannot_be_reused(self) -> None:
        for pending in (False, True):
            for field in ("reference_id", "cycle_id"):
                with self.subTest(pending=pending, field=field):
                    record = following_record(pending=pending)
                    data = self.accept(record)
                    value = decision(record, pending=pending)
                    value["references"][0][field] = record["history"][0]["references"][
                        0
                    ][field]
                    value["reference_ids"] = [
                        reference["reference_id"] for reference in value["references"]
                    ]
                    self.reject(data)

    def test_ordinary_decisions_respect_the_seven_day_boundary(self) -> None:
        for pending in (False, True):
            with self.subTest(pending=pending):
                record = following_record(pending=pending, at=APPLIED + 7 * 86400)
                data = self.accept(record)
                value = decision(record, pending=pending)
                if pending:
                    value["started_at"] -= 1
                else:
                    value["at"] -= 1
                    value["detected_at"] -= 1
                    record["last_applied_at"] -= 1
                    record["effective_at"] -= 1
                self.reject(data)

    def test_unknown_resolution_fences_subsequent_qualified_references(self) -> None:
        for pending in (False, True):
            with self.subTest(pending=pending):
                record = following_record(pending=pending)
                unknown = uncertain_record(changed_record(), APPLIED + 2)
                record.update(
                    attempts=unknown["attempts"],
                    blocked_evidence_ids=unknown["blocked_evidence_ids"],
                )
                data = envelope(record)
                suffix = "pending" if pending else "history[1]"
                self.reject(
                    data,
                    path=f"{RECORD_PATH}.{suffix}",
                    reason="qualified decision reuses a pre-change reference window",
                )
                for reference in decision(record, pending=pending)["references"]:
                    reference["start_at"] += 2
                    reference["end_at"] += 2
                self.assertIs(self.validate(data, CONFIGURED, {}), data)

    def test_verified_automatic_effect_requires_its_rollback_target(self) -> None:
        for missing in (False, True):
            with self.subTest(missing=missing):
                record = changed_record()
                if missing:
                    del record["previous_wet"]
                else:
                    record["previous_wet"] = None
                self.reject(envelope(record))

    def test_unverified_concurrent_effect_preserves_actual_unbounded_values(
        self,
    ) -> None:
        for kind in ("automatic", "rollback"):
            for actual in ([67.0, 223.0], [68.0, 1400.0]):
                with self.subTest(kind=kind, actual=actual):
                    record = changed_record()
                    record["history"][0].update(
                        kind=kind,
                        reason="concurrent_change",
                        new_dry=actual[0],
                        new_wet=actual[1],
                    )
                    record.update(
                        current=list(actual),
                        last_evidence_id=None,
                        previous_wet=None,
                        verified_revision=None,
                        blocked_evidence_ids=["accepted-evidence"],
                    )
                    self.accept(record)

    def test_samples_belong_to_their_complete_epoch_interval(self) -> None:
        record = returned_source(initial_record())
        sample = raw_sample(at=APPLIED, trusted=True)
        record["samples"] = [sample]
        data = self.accept(record)
        sample["sampled_at"] = APPLIED + 1
        self.reject(data)
        sample.update(
            sampled_at=CREATED, source_fingerprint=record["source_fingerprint"]
        )
        self.reject(data)
        sample["sampled_at"] = APPLIED + 2
        self.assertIs(self.validate(data, CONFIGURED, {}), data)

    def test_archived_reference_must_end_before_the_next_epoch(self) -> None:
        record = returned_source(initial_record())
        reference = references()[-1]
        record["references"] = [reference]
        reference.update(start_at=APPLIED - 3600, end_at=APPLIED)
        data = self.accept(record)
        reference["end_at"] = APPLIED + 1
        self.reject(data)
        reference.update(start_at=APPLIED + 3, end_at=APPLIED + 3603)
        self.reject(data)

    def test_impossible_adc_observations_are_retained_only_without_trust(self) -> None:
        for value in (-1.0, 4096.0, 1e300):
            for field in ("raw", "burst"):
                for trusted in ("reliable", "rollback_eligible"):
                    with self.subTest(value=value, field=field, trusted=trusted):
                        record = initial_record()
                        sample = raw_sample()
                        sample[field] = value if field == "raw" else [value] * 11
                        record["samples"] = [sample]
                        data = self.accept(record)
                        sample[trusted] = True
                        self.reject(data)

    def test_witness_prevents_losing_or_rolling_back_an_unknown_fence(self) -> None:
        for factory in (initial_record, changed_record):
            for restore in ("missing", "earlier"):
                with self.subTest(factory=factory.__name__, restore=restore):
                    known = uncertain_record(factory())
                    observed = {SOURCE: witness(known)}
                    latest = envelope(known)
                    self.assertIs(self.validate(latest, CONFIGURED, observed), latest)
                    record = factory()
                    if restore == "earlier":
                        uncertain_record(record, APPLIED + 5)
                    self.reject(envelope(record), observed)

    def test_lagging_witness_allows_newer_primary_decision_fence(self) -> None:
        primary = envelope(uncertain_record(initial_record()))
        for record in (
            initial_record(),
            uncertain_record(initial_record(), APPLIED + 5),
        ):
            with self.subTest(fence=witness(record)["decision_fence_at"]):
                self.assertIs(
                    self.validate(primary, CONFIGURED, {SOURCE: witness(record)}),
                    primary,
                )
        restored = primary["records"][SOURCE]
        self.assertEqual(restored["revision"], 0)
        self.assertEqual(restored["history"], [])
        self.assertIsNone(restored["last_applied_at"])
        self.assertIsNone(restored["effective_at"])

    def test_witness_requires_an_explicit_decision_fence_field(self) -> None:
        for record in (initial_record(), uncertain_record(initial_record())):
            with self.subTest(fence=witness(record)["decision_fence_at"]):
                observed = witness(record)
                del observed["decision_fence_at"]
                self.reject(envelope(record), {SOURCE: observed})

    def test_witness_decision_fence_requires_a_finite_positive_clock(self) -> None:
        record = uncertain_record(initial_record())
        for invalid in (False, 0, -1, float("inf"), float("nan"), "later"):
            with self.subTest(fence=invalid):
                observed = witness(record)
                observed["decision_fence_at"] = invalid
                self.reject(envelope(record), {SOURCE: observed})

    def test_witness_decision_fence_cannot_precede_its_actual_effect(self) -> None:
        record = changed_record()
        for invalid in (None, APPLIED - 1):
            with self.subTest(fence=invalid):
                observed = witness(record)
                observed["decision_fence_at"] = invalid
                self.reject(envelope(record), {SOURCE: observed})

    def test_native_concurrent_effect_without_snapshots_is_preserved(self) -> None:
        for kind in ("automatic", "rollback"):
            with self.subTest(kind=kind):
                record = changed_record()
                event = record["history"][0]
                without_reference_snapshots(event)
                manual = dict(event)
                manual.update(
                    event_id="manual-change-1",
                    kind="manual",
                    new_wet=230.0,
                    at=APPLIED - 1,
                    detected_at=APPLIED - 1,
                )
                del manual["reason"], manual["evidence_id"]
                event.update(
                    kind=kind, reason="concurrent_change", old_wet=230.0, revision=2
                )
                record["history"].insert(0, manual)
                record.update(
                    previous=[67.0, 230.0],
                    revision=2,
                    verified_revision=None,
                    previous_wet=None,
                    last_evidence_id=None,
                    blocked_evidence_ids=["accepted-evidence"],
                )
                data = envelope(record)
                self.assertIs(
                    self.validate(data, CONFIGURED, {SOURCE: witness(record)}), data
                )

    def test_qualified_current_tail_cannot_lose_both_verification_fields(self) -> None:
        for factory in (changed_record, rollback_record):
            for remove in (False, True):
                with self.subTest(factory=factory.__name__, remove=remove):
                    record = factory()
                    observed = {SOURCE: witness(record)}
                    for field in ("verified_revision", "previous_wet"):
                        if remove:
                            del record[field]
                        else:
                            record[field] = None
                    self.reject(envelope(record), observed)

    def test_event_reason_must_belong_to_its_declared_kind(self) -> None:
        for reason in ("unknown_effect", "contradiction"):
            with self.subTest(reason=reason):
                record = changed_record()
                record["history"][0]["reason"] = reason
                self.reject(
                    envelope(record),
                    path=f"{RECORD_PATH}.history[0].reason",
                    reason="reason does not describe a supported event kind",
                )

    def test_reconciled_external_effect_keeps_its_exclusion_without_attempts(
        self,
    ) -> None:
        for pending in (False, True):
            with self.subTest(pending=pending):
                record = following_record(pending=True) if pending else changed_record()
                event = record["history"][0]
                event.update(
                    kind="external_discovered",
                    at=None,
                    reason="pending_effect_reconciled",
                )
                without_reference_snapshots(event)
                record.update(
                    last_evidence_id=None, verified_revision=None, previous_wet=None
                )
                if pending:
                    record["pending"]["evidence_id"] = "accepted-evidence"
                data = envelope(record)
                self.reject(data)
                record["blocked_evidence_ids"] = ["accepted-evidence"]
                if pending:
                    self.reject(data)
                else:
                    self.assertIs(self.validate(data, CONFIGURED, {}), data)

    def test_qualified_effect_time_belongs_to_its_claimed_epoch(self) -> None:
        for effect in (APPLIED + 1, APPLIED + 2000):
            with self.subTest(effect=effect):
                record = returned_source(changed_record())
                record["history"][0].update(at=effect, detected_at=effect)
                record.update(
                    last_applied_at=effect,
                    effective_at=effect,
                    barrier_until=max(effect + 1800, APPLIED + 1802),
                )
                self.reject(envelope(record), {SOURCE: witness(initial_record())})

    def test_nested_errors_identify_the_rejected_collection_entry(self) -> None:
        for index in (0, 1):
            with self.subTest(history=index):
                record = following_record(pending=False)
                record["history"][index]["new_wet"] = "invalid"
                self.reject(
                    envelope(record), path=f"{RECORD_PATH}.history[{index}].new_wet"
                )
        for field, suffix in (("raw", "raw"), ("burst", "burst[2]")):
            with self.subTest(sample=field):
                record = initial_record()
                sample = raw_sample()
                record["samples"] = [copy.deepcopy(sample), sample]
                if field == "raw":
                    sample["raw"] = "invalid"
                else:
                    sample["burst"][2] = "invalid"
                self.reject(envelope(record), path=f"{RECORD_PATH}.samples[1].{suffix}")
        record = uncertain_record(initial_record())
        attempt = copy.deepcopy(record["attempts"][0])
        attempt["attempt_id"] = "second-attempt"
        attempt["target"][1] = "invalid"
        record["attempts"].append(attempt)
        self.reject(envelope(record), path=f"{RECORD_PATH}.attempts[1].target[1]")
        for field in ("expected", "target"):
            with self.subTest(pending=field):
                record = pending_record()
                record["pending"][field][1] = "invalid"
                self.reject(envelope(record), path=f"{RECORD_PATH}.pending.{field}[1]")

    def test_qualified_reference_and_cycle_ids_must_be_unique(self) -> None:
        for factory in (pending_record, changed_record):
            for field in ("reference_id", "cycle_id"):
                with self.subTest(factory=factory.__name__, field=field):
                    record = factory()
                    value = record["pending"] or record["history"][-1]
                    if field == "reference_id":
                        duplicate = copy.deepcopy(value["references"][0])
                        duplicate["cycle_id"] = "different-cycle"
                        value["references"].append(duplicate)
                    else:
                        for reference in value["references"]:
                            reference["cycle_id"] = "same-cycle"
                    self.reject(envelope(record), {SOURCE: witness(record)})

    def test_initial_record_rejects_malformed_present_barrier(self) -> None:
        for factory in (initial_record, pending_record):
            for invalid in (
                float("nan"),
                float("inf"),
                {},
                "broken-clock",
                None,
                False,
                0,
                -1,
            ):
                with self.subTest(factory=factory.__name__, barrier=invalid):
                    record = factory()
                    record["barrier_until"] = invalid
                    self.reject(envelope(record), {SOURCE: witness(record)})

    def test_decision_fence_filters_only_uncertainty_resolutions(self) -> None:
        self.assertIsNone(self.decision_fence(initial_record()))
        record = uncertain_record(changed_record(), APPLIED + 10)
        aborted = copy.deepcopy(record["attempts"][0])
        aborted.update(
            attempt_id="aborted-context",
            outcome="aborted",
            resolved_at=APPLIED + 20,
        )
        record["attempts"].append(aborted)
        self.validate(envelope(record), CONFIGURED, {})
        self.assertEqual(self.decision_fence(record), APPLIED + 10)
        fence_at = storage._decision_fence
        attempts = record["attempts"]
        for cutoff, expected in ((APPLIED + 9, APPLIED), (APPLIED + 10, APPLIED + 10)):
            with self.subTest(inclusive_cutoff=cutoff):
                self.assertEqual(
                    fence_at(APPLIED, attempts, attempts_resolved_through=cutoff),
                    expected,
                )
        self.assertIsNone(
            fence_at(None, attempts, attempts_resolved_through=APPLIED + 9)
        )
        self.assertEqual(
            fence_at(APPLIED, attempts, attempts_resolved_through=CREATED), APPLIED
        )

    def test_exported_outcomes_and_reasons_keep_the_wire_values(self) -> None:
        for actual, expected in (
            (storage.UNKNOWN_OUTCOME, "interrupted_unknown"),
            (storage.ABORTED_OUTCOME, "aborted"),
            (storage.PRE_ENROLLMENT_REASON, "pre_enrollment"),
            (storage.AMBIGUOUS_ORDER_REASON, "ambiguous_order"),
            (storage.INVALID_TIME_REASON, "invalid_time"),
        ):
            self.assertEqual(actual, expected)

    def test_nested_identifier_and_fence_errors_name_the_exact_field(self) -> None:
        record = pending_record()
        record["pending"]["diagnostics"]["block_evidence_ids"] = ["valid", {}]
        self.reject(
            envelope(record),
            path=f"{RECORD_PATH}.pending.diagnostics.block_evidence_ids[1]",
        )
        record = pending_record()
        record["pending"]["reference_ids"][1] = {}
        self.reject(envelope(record), path=f"{RECORD_PATH}.pending.reference_ids[1]")
        record = returned_source(initial_record())
        record["source_fences"][1]["at"] = "invalid"
        self.reject(envelope(record), path=f"{RECORD_PATH}.source_fences[1].at")

    def test_unbound_helper_observations_are_structural_facts_only(self) -> None:
        record = initial_record()
        observation = {
            "endpoint": "wet",
            "before": 221.0,
            "after": 223.0,
            "at": CREATED - 10,
            "context_id": None,
            "event_id": "early-helper",
            "detected_at": CREATED,
            "source_fingerprint": None,
            "reason": "pre_enrollment",
        }
        record["helper_observations"] = [observation]
        for reason in ("pre_enrollment", "ambiguous_order", "invalid_time"):
            observation.update(
                reason=reason, at=APPLIED if reason == "invalid_time" else CREATED - 10
            )
            data = self.accept(record)
        self.assertEqual(record["history"], [])
        self.assertIsNone(self.decision_fence(record))
        for field, invalid in (
            ("endpoint", "raw"),
            ("before", "221"),
            ("at", 0),
            ("context_id", 1),
            ("source_fingerprint", fingerprint("probe-A", 0)),
            ("reason", "qualified_drift"),
        ):
            with self.subTest(field=field):
                corrupted = copy.deepcopy(data)
                corrupted["records"][SOURCE]["helper_observations"][0][field] = invalid
                self.reject(
                    corrupted, path=f"{RECORD_PATH}.helper_observations[0].{field}"
                )

    def test_fingerprint_includes_the_persisted_epoch(self) -> None:
        initial = self.fingerprint("probe-A", 0)
        self.assertEqual(initial, fingerprint("probe-A", 0))
        self.assertNotEqual(initial, self.fingerprint("probe-A", 2))


if __name__ == "__main__":
    unittest.main()
