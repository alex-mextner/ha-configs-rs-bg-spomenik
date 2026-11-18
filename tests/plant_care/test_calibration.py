"""Bounded deterministic calibration tests, independent of Home Assistant."""

import importlib.util
import json
import sys
import types
import unittest
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "_plant_calibration_engine_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "custom_components/plant_care_alerts")]
sys.modules[PACKAGE] = package
MODULE_PATH = ROOT / "custom_components/plant_care_alerts/calibration.py"
SPEC = importlib.util.spec_from_file_location(PACKAGE + ".calibration", MODULE_PATH)
calibration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = calibration
SPEC.loader.exec_module(calibration)

START = 1_800_000_000.0
DAY = 86400.0
SOURCE = "adc3:pin22:revision1"


def baseline(**changes: object) -> calibration.CalibrationState:
    values = {
        "original_dry": 67.0,
        "original_wet": 221.0,
        "current_wet": 221.0,
        "revision": 0,
        "decision_fence_at": None,
        "last_evidence_id": None,
        "source_fingerprint": SOURCE,
        "baseline_source_fingerprint": SOURCE,
        "current_dry": 67.0,
    }
    return calibration.CalibrationState(**(values | changes))


def reading(at, raw, **changes: object) -> calibration.PhysicalSample:
    values = {
        "sampled_at": at,
        "raw": raw,
        "burst": (raw - 1,) * 5 + (raw,) + (raw + 1,) * 5,
        "source_fingerprint": SOURCE,
        "reliable": True,
    }
    return calibration.PhysicalSample(**(values | changes))


def reference_window(
    day, raw=209.2, index=90.0, count=13
) -> tuple[list[calibration.PhysicalSample], calibration.QualifiedWetReference]:
    """209.2 at a trusted 90% implies wet225, independently hand calculated."""
    start = START + day * DAY
    samples = [reading(start + i * 300, raw) for i in range(count)]
    reference = calibration.QualifiedWetReference(
        reference_id=f"reference-{day}",
        cycle_id=f"cycle-{day}",
        start_at=start,
        end_at=start + 3600,
        source_fingerprint=SOURCE,
        reference_source="sensor.independent_reference",
        protocol="controlled-wet-reference-v1",
        reference_index=index,
    )
    return samples, reference


def qualified_history(
    days=(0, 7, 14, 15), raw=209.2, index=90.0
) -> tuple[
    list[calibration.PhysicalSample], list[calibration.QualifiedWetReference], float
]:
    samples, references = [], []
    for day in days:
        window, reference = reference_window(day, raw, index)
        samples.extend(window)
        references.append(reference)
    return samples, references, samples[-1].sampled_at


def full_scale_state(wet: float = 4000.0) -> calibration.CalibrationState:
    return baseline(
        original_dry=0.0, original_wet=wet, current_dry=0.0, current_wet=wet
    )


def rollback_state(fence_at: float, **changes: object) -> calibration.CalibrationState:
    values = {
        "current_wet": 222.0,
        "previous_wet": 221.0,
        "revision": 1,
        "decision_fence_at": fence_at,
        "last_evidence_id": "accepted-evidence",
    }
    return baseline(**(values | changes))


def custom_history(
    offsets: Sequence[float],
    raws: Sequence[float] | None = None,
    *,
    index: float = 90.0,
    window_bounds: tuple[float, float] = (0.0, 3600.0),
) -> tuple[
    list[calibration.PhysicalSample], list[calibration.QualifiedWetReference], float
]:
    values = [209.2] * len(offsets) if raws is None else raws
    samples, refs = [], []
    for day in (0, 7, 14, 15):
        _, ref = reference_window(day, index=index, count=0)
        start = ref.start_at
        refs.append(
            replace(
                ref, start_at=start + window_bounds[0], end_at=start + window_bounds[1]
            )
        )
        samples.extend(
            reading(start + offset, raw)
            for offset, raw in zip(offsets, values, strict=True)
        )
    return samples, refs, refs[-1].end_at


class CalibrationTests(unittest.TestCase):
    """Exercise bounded updates and refusal of insufficient physical evidence."""

    def test_even_burst_central_adc_rails_do_not_qualify_an_anchor(self) -> None:
        for raw, burst in (
            (4094.0, (4094.0,) * 3 + (4095.0,) * 3),
            (1.0, (0.0,) * 3 + (1.0,) * 3),
        ):
            with self.subTest(raw=raw):
                samples, refs, now = qualified_history(raw=raw, index=100.0)
                samples = [replace(s, burst=burst) for s in samples]
                now += 300
                samples.append(reading(now, 4000.0))
                state = full_scale_state()
                result = calibration.evaluate_calibration(samples, refs, state, now)
                self.assertEqual(result.reason, "poor_reference_quality")
                self.assertEqual(
                    set(result.diagnostics["rejected_references"].values()), {"clipped"}
                )
                self.assertIsNone(result.proposed_wet)

    def test_oversized_json_integers_fail_closed_at_numeric_boundaries(self) -> None:
        samples, refs, now = qualified_history()
        huge = 10**400
        cases = (
            ("state", samples, refs, baseline(original_wet=huge), now, "invalid_state"),
            ("clock", samples, refs, baseline(), huge, "invalid_state"),
            (
                "raw",
                [*samples[:-1], reading(now, huge)],
                refs,
                baseline(),
                now,
                "unreliable_sample",
            ),
            (
                "reference",
                samples,
                [replace(r, reference_index=huge) for r in refs],
                baseline(),
                now,
                "poor_reference_quality",
            ),
        )
        for label, points, references, state, at, reason in cases:
            with self.subTest(boundary=label):
                result = calibration.evaluate_calibration(points, references, state, at)
                self.assertEqual(result.reason, reason)
                self.assertIsNone(result.proposed_wet)

    def test_omitted_reliability_does_not_trust_a_physical_sample(self) -> None:
        samples, refs, now = qualified_history()
        latest = calibration.PhysicalSample(
            sampled_at=now,
            raw=209.2,
            burst=samples[-1].burst,
            source_fingerprint=SOURCE,
        )
        result = calibration.evaluate_calibration(
            [*samples[:-1], latest], refs, baseline(), now
        )
        self.assertEqual(result.reason, "unreliable_sample")
        self.assertIsNone(result.proposed_wet)

    def test_frozen_inputs_do_not_retain_mutable_json_list_aliases(self) -> None:
        original = reading(START, 209.2)
        burst = list(original.burst)
        blocked = ["older-evidence"]
        sample = replace(original, burst=burst)
        state = baseline(blocked_evidence_ids=blocked)
        burst[0] = 4095.0
        blocked.append("later-mutation")
        self.assertEqual(sample.burst, original.burst)
        self.assertEqual(state.blocked_evidence_ids, ("older-evidence",))

    def test_single_nan_reading_is_unreliable_not_conflicting_evidence(self) -> None:
        samples, refs, now = qualified_history()
        result = calibration.evaluate_calibration(
            [*samples[:-1], reading(now, float("nan"))], refs, baseline(), now
        )
        self.assertEqual(result.reason, "unreliable_sample")
        self.assertIsNone(result.proposed_wet)

    def test_current_raw_1500_without_reference_never_changes_baseline(self) -> None:
        result = calibration.evaluate_calibration(
            [reading(START, 1500.0, reliable=False)], [], baseline(), START
        )
        self.assertEqual(result.status, "observe")
        self.assertEqual(result.reason, "awaiting_reference")
        self.assertIsNone(result.proposed_wet)

    def test_three_cycles_and_future_holdout_propose_only_one_adc_count(self) -> None:
        samples, references, now = qualified_history()
        result = calibration.evaluate_calibration(samples, references, baseline(), now)
        self.assertEqual(result.status, "propose")
        self.assertEqual(result.reason, "qualified_drift")
        self.assertEqual(result.proposed_wet, 222.0)
        self.assertEqual(
            result.reference_ids, tuple(r.reference_id for r in references)
        )
        self.assertAlmostEqual(result.diagnostics["target_wet"], 225.0)
        self.assertLess(
            result.diagnostics["holdout_error_after"],
            result.diagnostics["holdout_error_before"],
        )

    def test_three_cycles_without_future_holdout_wait(self) -> None:
        samples, refs, now = qualified_history(days=(0, 7, 14))
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(
            (result.status, result.reason), ("observe", "awaiting_holdout")
        )

    def test_training_must_span_fourteen_days_before_holdout(self) -> None:
        samples, refs, now = qualified_history(days=(0, 6, 13, 14))
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.reason, "insufficient_span")
        self.assertIsNone(result.proposed_wet)

    def test_twelve_polls_in_fifty_five_minutes_are_insufficient(self) -> None:
        samples, refs, now = custom_history([i * 300 for i in range(12)])
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.reason, "poor_reference_quality")
        self.assertIsNone(result.proposed_wet)

    def test_replaying_one_burst_does_not_create_independent_observations(self) -> None:
        samples, refs, now = qualified_history()
        samples = [s for s in samples if (s.sampled_at - START) % DAY == 0] * 13
        samples.append(reading(now, 209.2))
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.reason, "poor_reference_quality")

    def test_internal_gap_over_ten_minutes_fails_coverage_despite_enough_votes(
        self,
    ) -> None:
        samples, refs, now = qualified_history()
        samples = [
            replace(s, sampled_at=s.sampled_at + 1200)
            if (s.sampled_at - START) % DAY >= 1800
            else s
            for s in samples
        ]
        refs = [replace(r, end_at=r.end_at + 1200) for r in refs]
        result = calibration.evaluate_calibration(samples, refs, baseline(), now + 1200)
        self.assertEqual(result.reason, "poor_reference_quality")

    def test_same_cycle_cannot_supply_three_training_references(self) -> None:
        samples, refs, now = qualified_history()
        refs = [replace(r, cycle_id="one-cycle") for r in refs]
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.reason, "awaiting_cycles")

    def test_conflicting_reused_reference_identifier_is_rejected(self) -> None:
        samples, refs, now = qualified_history()
        refs.append(replace(refs[0], reference_index=80))
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.reason, "conflicting_evidence")
        self.assertIsNone(result.proposed_wet)

    def test_unreliable_current_sample_blocks_even_good_old_references(self) -> None:
        samples, refs, now = qualified_history()
        samples[-1] = replace(samples[-1], reliable=False)
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.reason, "unreliable_sample")
        self.assertIsNone(result.proposed_wet)

    def test_stale_current_sample_cannot_authorize_calibration(self) -> None:
        samples, refs, now = qualified_history()
        result = calibration.evaluate_calibration(samples, refs, baseline(), now + 1201)
        self.assertEqual(result.reason, "stale_sample")

    def test_future_physical_sample_cannot_authorize_calibration(self) -> None:
        samples, refs, now = qualified_history()
        result = calibration.evaluate_calibration(samples, refs, baseline(), now - 1)
        self.assertEqual(result.reason, "future_sample")

    def test_source_change_invalidates_original_baseline(self) -> None:
        samples, refs, now = qualified_history()
        state = baseline(baseline_source_fingerprint="replaced-probe")
        result = calibration.evaluate_calibration(samples, refs, state, now)
        self.assertEqual(result.reason, "source_changed")

    def test_manual_dry_override_is_preserved(self) -> None:
        samples, refs, now = qualified_history()
        result = calibration.evaluate_calibration(
            samples, refs, baseline(current_dry=68), now
        )
        self.assertEqual(result.reason, "manual_override")
        self.assertIsNone(result.proposed_wet)

    def test_manual_wet_override_outside_original_envelope_is_preserved(self) -> None:
        samples, refs, now = qualified_history()
        result = calibration.evaluate_calibration(
            samples, refs, baseline(current_wet=230), now
        )
        self.assertEqual(result.reason, "manual_override")

    def test_far_target_is_rejected_instead_of_clipped_into_small_steps(self) -> None:
        samples, refs, now = qualified_history(raw=222.7)
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.reason, "target_out_of_bounds")
        self.assertIsNone(result.proposed_wet)
        self.assertAlmostEqual(result.diagnostics["target_wet"], 240.0)

    def test_raw_1500_is_rejected_even_with_spurious_reference_labels(self) -> None:
        samples, refs, now = qualified_history(raw=1500.0)
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.reason, "unreliable_sample")
        self.assertIsNone(result.proposed_wet)

    def test_target_inside_deadband_does_not_churn_helper(self) -> None:
        samples, refs, now = qualified_history(raw=206.5)
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.reason, "deadband")
        self.assertIsNone(result.proposed_wet)

    def test_helper_resolution_cannot_exceed_original_span_step(self) -> None:
        samples, refs, now = qualified_history()
        result = calibration.evaluate_calibration(
            samples, refs, baseline(wet_step=2.0), now
        )
        self.assertEqual(result.reason, "helper_resolution")
        self.assertIsNone(result.proposed_wet)

    def test_fractional_helper_step_rounds_towards_current_endpoint(self) -> None:
        samples, refs, now = qualified_history()
        result = calibration.evaluate_calibration(
            samples, refs, baseline(wet_step=0.1), now
        )
        self.assertEqual(result.proposed_wet, 222.5)

    def test_cooldown_survives_state_reconstruction(self) -> None:
        samples, refs, now = qualified_history()
        state = baseline(revision=1, decision_fence_at=now - 6 * DAY)
        result = calibration.evaluate_calibration(samples, refs, state, now)
        self.assertEqual(result.reason, "cooldown")
        self.assertIsNone(result.proposed_wet)

    def test_holdout_must_improve_actual_quantized_proposal(self) -> None:
        samples, refs, now = qualified_history()
        holdout = refs[-1]
        samples = [
            reading(s.sampled_at, 205.6)
            if holdout.start_at <= s.sampled_at <= holdout.end_at
            else s
            for s in samples
        ]
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.reason, "holdout_failed")
        self.assertIsNone(result.proposed_wet)

    def test_one_extreme_burst_read_does_not_shift_a_robust_anchor(self) -> None:
        samples, refs, now = qualified_history()
        samples = [replace(s, burst=(*s.burst[:-1], 4000.0)) for s in samples]
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.proposed_wet, 222.0)

    def test_watering_step_inside_window_is_not_filtered_into_stationarity(
        self,
    ) -> None:
        samples, refs, now = qualified_history()
        samples = [
            reading(
                s.sampled_at, 205.0 if (s.sampled_at - START) % DAY < 1800 else 215.0
            )
            for s in samples
        ]
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.reason, "poor_reference_quality")
        self.assertIsNone(result.proposed_wet)

    def test_full_window_target_is_not_hidden_by_sample_envelope_filter(self) -> None:
        samples, refs, now = custom_history(
            [i * 100 for i in range(37)],
            [228.0 + i % 3 for i in range(37)],
            index=100.0,
        )
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(
            (result.status, result.reason), ("observe", "target_out_of_bounds")
        )
        self.assertEqual(result.diagnostics["target_wet"], 229.0)
        self.assertIsNone(result.proposed_wet)

    def test_stationarity_uses_observations_between_independent_votes(self) -> None:
        for phase in (0, 1):
            with self.subTest(phase=phase):
                samples, refs, now = custom_history(
                    [i * 150 for i in range(25)],
                    [209.2 + 10 * ((i + phase) % 2) for i in range(25)],
                )
                result = calibration.evaluate_calibration(
                    samples, refs, baseline(), now
                )
                self.assertEqual(result.reason, "poor_reference_quality")
                self.assertIsNone(result.proposed_wet)

    def test_adc_rail_reference_is_not_an_anchor_after_current_sample_recovers(
        self,
    ) -> None:
        samples, refs, now = qualified_history(raw=4095.0, index=100.0)
        samples = [replace(s, burst=(4095.0,) * 11) for s in samples]
        now += 300
        samples.append(reading(now, 4000.0))
        state = full_scale_state()
        result = calibration.evaluate_calibration(samples, refs, state, now)
        self.assertEqual(result.reason, "poor_reference_quality")
        self.assertIsNone(result.proposed_wet)

    def test_reported_raw_cannot_hide_burst_median_at_adc_rail(self) -> None:
        for raw, rail in ((4094.0, 4095.0), (1.0, 0.0)):
            with self.subTest(raw=raw, burst_median=rail):
                samples, refs, now = qualified_history(raw=raw, index=100.0)
                samples = [replace(s, burst=(rail,) * 11) for s in samples]
                now += 300
                samples.append(reading(now, 4000.0))
                state = full_scale_state()
                result = calibration.evaluate_calibration(samples, refs, state, now)
                self.assertEqual(result.reason, "poor_reference_quality")
                self.assertEqual(result.diagnostics["qualified_cycles"], 0)
                self.assertEqual(
                    set(result.diagnostics["rejected_references"].values()), {"clipped"}
                )
                self.assertIsNone(result.proposed_wet)

    def test_true_independent_hundred_reference_is_not_inferred_from_clipping(
        self,
    ) -> None:
        samples, refs, now = qualified_history(raw=225.0, index=100.0)
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.proposed_wet, 222.0)
        self.assertGreater(result.diagnostics["holdout_error_before"], 2.0)

    def test_qualified_later_contradiction_rolls_back_once(self) -> None:
        samples, refs, applied_at = qualified_history()
        window, ref = reference_window(16, raw=205.6)
        state = rollback_state(applied_at)
        result = calibration.evaluate_calibration(
            samples + window, [*refs, ref], state, ref.end_at
        )
        self.assertEqual((result.status, result.reason), ("rollback", "contradiction"))
        self.assertEqual(result.proposed_wet, 221.0)
        self.assertIn("accepted-evidence", result.diagnostics["block_evidence_ids"])
        self.assertIn(result.evidence_id, result.diagnostics["block_evidence_ids"])

    def test_blocked_rollback_evidence_does_not_repeat(self) -> None:
        samples, refs, applied_at = qualified_history()
        window, ref = reference_window(16, raw=205.6)
        state = rollback_state(applied_at)
        first = calibration.evaluate_calibration(
            samples + window, [*refs, ref], state, ref.end_at
        )
        state = replace(state, blocked_evidence_ids=(first.evidence_id,))
        repeated = calibration.evaluate_calibration(
            samples + window, [*refs, ref], state, ref.end_at
        )
        self.assertEqual(repeated.reason, "evidence_reused")
        self.assertIsNone(repeated.proposed_wet)

    def test_blocked_rollback_origin_cannot_reapply_previous_endpoint(self) -> None:
        samples, refs, rolled_back_at = qualified_history()
        window, ref = reference_window(16)
        # Runtime clears previous_wet after rollback. Guard even a caller that
        # incorrectly retains the reversed endpoint alongside the rollback marker.
        state = baseline(
            current_wet=221.0,
            previous_wet=222.0,
            revision=2,
            decision_fence_at=rolled_back_at,
            last_evidence_id="rollback-evidence",
            blocked_evidence_ids=("applied-evidence", "rollback-evidence"),
        )
        result = calibration.evaluate_calibration(
            samples + window, [*refs, ref], state, ref.end_at
        )
        self.assertEqual((result.status, result.reason), ("observe", "cooldown"))
        self.assertIsNone(result.proposed_wet)

    def test_new_proposal_requires_three_cycles_after_prior_apply(self) -> None:
        samples, refs, applied_at = qualified_history()
        new_now = applied_at + 7 * DAY
        samples.append(reading(new_now, 209.2))
        state = baseline(
            current_wet=222.0,
            revision=1,
            decision_fence_at=applied_at,
            last_evidence_id="accepted-evidence",
        )
        result = calibration.evaluate_calibration(samples, refs, state, new_now)
        self.assertEqual(result.reason, "awaiting_cycles")
        self.assertEqual(result.diagnostics["qualified_cycles"], 4)
        self.assertEqual(result.diagnostics["fresh_qualified_cycles"], 0)
        self.assertIsNone(result.proposed_wet)

    def test_repeated_accepted_evidence_is_not_applied_again(self) -> None:
        samples, refs, now = qualified_history()
        first = calibration.evaluate_calibration(samples, refs, baseline(), now)
        state = baseline(blocked_evidence_ids=(first.evidence_id,))
        repeated = calibration.evaluate_calibration(samples, refs, state, now)
        self.assertEqual(repeated.reason, "evidence_reused")
        self.assertIsNone(repeated.proposed_wet)

    def test_adc_ceiling_applies_to_target_before_any_step(self) -> None:
        samples, refs, now = qualified_history(raw=3735.0)
        state = full_scale_state(4094.0)
        result = calibration.evaluate_calibration(samples, refs, state, now)
        self.assertEqual(result.reason, "target_out_of_bounds")
        self.assertIsNone(result.proposed_wet)

    def test_far_holdout_target_cannot_validate_small_training_update(self) -> None:
        samples, refs, now = qualified_history()
        holdout = refs[-1]
        samples = [
            reading(s.sampled_at, 222.7)
            if holdout.start_at <= s.sampled_at <= holdout.end_at
            else s
            for s in samples
        ]
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.reason, "target_out_of_bounds")
        self.assertIsNone(result.proposed_wet)

    def test_numeric_json_representation_does_not_change_evidence_identity(
        self,
    ) -> None:
        samples, refs, now = qualified_history(raw=225.0, index=100.0)
        first = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(first.status, "propose")
        self.assertIsNotNone(first.evidence_id)
        integral_samples = [
            replace(
                s,
                sampled_at=int(s.sampled_at),
                raw=int(s.raw),
                burst=tuple(int(v) for v in s.burst),
            )
            for s in samples
        ]
        integral_refs = [
            replace(
                r, start_at=int(r.start_at), end_at=int(r.end_at), reference_index=100
            )
            for r in refs
        ]
        state = baseline(
            original_dry=67, original_wet=221, current_wet=221, current_dry=67
        )
        reloaded = calibration.evaluate_calibration(
            integral_samples, integral_refs, state, int(now)
        )
        self.assertEqual(first.evidence_id, reloaded.evidence_id)

    def test_conflicting_latest_replay_cannot_authorize_old_good_evidence(self) -> None:
        samples, refs, now = qualified_history()
        samples.extend(
            [reading(now + 60, 209.2), reading(now + 60, 1500, reliable=False)]
        )
        result = calibration.evaluate_calibration(samples, refs, baseline(), now + 60)
        self.assertEqual(result.reason, "conflicting_evidence")
        self.assertIsNone(result.proposed_wet)

    def test_added_real_sample_cannot_shorten_physical_window_coverage(self) -> None:
        samples, refs, now = qualified_history()
        samples = [replace(s, sampled_at=s.sampled_at + 150) for s in samples]
        refs = [
            replace(r, start_at=r.start_at + 150, end_at=r.end_at + 150) for r in refs
        ]
        before = calibration.evaluate_calibration(samples, refs, baseline(), now + 150)
        self.assertEqual(before.status, "propose")
        samples.extend(reading(r.start_at + 3460, 209.2) for r in refs)
        enriched = calibration.evaluate_calibration(
            samples, refs, baseline(), now + 150
        )
        self.assertEqual(enriched.status, "propose")
        self.assertEqual(enriched.proposed_wet, 222.0)

    def test_invalid_reference_copy_conflicts_in_both_input_orders(self) -> None:
        samples, refs, now = qualified_history()
        for changes in (
            {"reference_index": 0},
            {"source_fingerprint": "different-source"},
            {"protocol": ""},
            {"end_at": now + 1},
        ):
            bad = replace(refs[0], **changes)
            for ordered in ([bad, *refs], [*refs, bad]):
                with self.subTest(changes=changes, bad_first=ordered[0] is bad):
                    result = calibration.evaluate_calibration(
                        samples, ordered, baseline(), now
                    )
                    self.assertEqual(result.reason, "conflicting_evidence")
                    self.assertIsNone(result.proposed_wet)

    def test_applied_state_cannot_lose_its_persisted_cooldown_clock(self) -> None:
        samples, refs, now = qualified_history()
        for state in (
            baseline(revision=1, current_wet=222, last_evidence_id="prior-apply"),
            baseline(previous_wet=220, last_evidence_id="prior-apply"),
        ):
            with self.subTest(state=state):
                result = calibration.evaluate_calibration(samples, refs, state, now)
                self.assertEqual(result.reason, "invalid_state")
                self.assertIsNone(result.proposed_wet)

    def test_missing_reference_does_not_hide_changed_or_invalid_baseline(self) -> None:
        for state, reason in (
            (baseline(source_fingerprint="new-probe"), "source_changed"),
            (baseline(current_dry=68), "manual_override"),
            (baseline(current_dry=None), "invalid_state"),
        ):
            with self.subTest(reason=reason):
                result = calibration.evaluate_calibration(
                    [reading(START, 209.2)], [], state, START
                )
                self.assertEqual(result.reason, reason)
                self.assertIsNone(result.proposed_wet)

    def test_boundary_pairs_supply_only_seven_independent_votes(self) -> None:
        offsets = [offset for b in range(1, 14, 2) for offset in (300 * b - 1, 300 * b)]
        samples, refs, now = custom_history(
            offsets, window_bounds=(offsets[0], offsets[-1])
        )
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.reason, "poor_reference_quality")
        self.assertIsNone(result.proposed_wet)

    def test_json_list_bursts_and_live_tuple_copies_are_the_same_evidence(self) -> None:
        samples, refs, now = qualified_history()
        first = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(first.status, "propose")
        self.assertIsNotNone(first.evidence_id)
        restored_samples = [
            calibration.PhysicalSample(**json.loads(json.dumps(asdict(s))))
            for s in samples
        ]
        restored_state = calibration.CalibrationState(
            **json.loads(json.dumps(asdict(baseline())))
        )
        restored_refs = [
            calibration.QualifiedWetReference(**json.loads(json.dumps(asdict(r))))
            for r in refs
        ]
        restored = calibration.evaluate_calibration(
            [*restored_samples, *samples], restored_refs, restored_state, now
        )
        self.assertEqual(restored.status, "propose")
        self.assertEqual(restored.evidence_id, first.evidence_id)

    def test_forward_downward_drift_keeps_positive_sensor_polarity(self) -> None:
        samples, refs, now = qualified_history(raw=202.0)
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.status, "propose")
        self.assertEqual(result.proposed_wet, 220.0)
        self.assertAlmostEqual(result.diagnostics["target_wet"], 217.0)
        self.assertLess(
            result.diagnostics["holdout_error_after"],
            result.diagnostics["holdout_error_before"],
        )

    def test_one_window_outlier_is_robust_but_a_step_cluster_is_not(self) -> None:
        samples, refs, now = qualified_history()
        isolated = [
            reading(s.sampled_at, 215.2) if (s.sampled_at - START) % DAY == 1800 else s
            for s in samples
        ]
        accepted = calibration.evaluate_calibration(isolated, refs, baseline(), now)
        self.assertEqual(accepted.proposed_wet, 222.0)
        clustered = [
            reading(s.sampled_at, 215.2) if (s.sampled_at - START) % DAY >= 2400 else s
            for s in samples
        ]
        rejected = calibration.evaluate_calibration(clustered, refs, baseline(), now)
        self.assertEqual(rejected.reason, "poor_reference_quality")
        self.assertIsNone(rejected.proposed_wet)

    def test_stale_holdout_cannot_authorize_forward_or_rollback(self) -> None:
        samples, refs, applied_at = qualified_history()
        later = applied_at + DAY + 1
        result = calibration.evaluate_calibration(
            [*samples, reading(later, 209.2)], refs, baseline(), later
        )
        self.assertEqual(result.reason, "awaiting_holdout")
        window, ref = reference_window(16, raw=205.6)
        later = ref.end_at + DAY + 1
        state = rollback_state(applied_at, last_evidence_id="prior-apply")
        result = calibration.evaluate_calibration(
            [*samples, *window, reading(later, 205.6)], [*refs, ref], state, later
        )
        self.assertEqual(result.status, "observe")
        self.assertIsNone(result.proposed_wet)

    def test_inverted_scale_is_outside_supported_scope(self) -> None:
        samples, refs, now = qualified_history()
        state = baseline(
            original_dry=221, original_wet=67, current_wet=67, current_dry=221
        )
        result = calibration.evaluate_calibration(samples, refs, state, now)
        self.assertEqual(result.reason, "invalid_state")

    def test_foreign_reference_fingerprint_cannot_calibrate_current_probe(self) -> None:
        samples, refs, now = qualified_history()
        refs = [replace(r, source_fingerprint="foreign-probe") for r in refs]
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.reason, "poor_reference_quality")
        self.assertIsNone(result.proposed_wet)
        self.assertEqual(
            set(result.diagnostics["rejected_references"].values()), {"source_changed"}
        )

    def test_nominal_poll_jitter_does_not_merge_adjacent_temporal_votes(self) -> None:
        offsets = [i * 300 + (0.1 if i % 2 == 0 else -0.1) for i in range(13)]
        samples, refs, now = custom_history(
            offsets, window_bounds=(offsets[0], offsets[-1])
        )
        result = calibration.evaluate_calibration(samples, refs, baseline(), now)
        self.assertEqual(result.status, "propose")
        self.assertEqual(result.proposed_wet, 222.0)


if __name__ == "__main__":
    unittest.main()
