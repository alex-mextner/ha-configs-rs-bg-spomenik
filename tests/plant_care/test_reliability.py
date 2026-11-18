"""Bounded regression tests for real timestamps, frozen values and saturation."""

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "_plant_reliability_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "custom_components/plant_care_alerts")]
sys.modules[PACKAGE] = package


def load(name) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        PACKAGE + "." + name,
        ROOT / "custom_components/plant_care_alerts" / (name + ".py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


state_machine = load("state_machine")
analytics = load("analytics")
START = 1794538800.0


def sample(at, value=100, raw=350) -> dict:
    return {
        "source_identity": "sensor.soil",
        "moisture": value,
        "raw_source_value": raw,
        "raw_source_identity": "sensor.raw",
        "sample_at": at,
        "dry_threshold": 25,
        "has_sensor": True,
    }


def plateau() -> dict:
    record = {}
    for offset in range(0, 12 * 3600 + 1, 300):
        record = state_machine.evaluate(record, sample(START + offset), START + offset)
    return record


class ReliabilityTests(unittest.TestCase):
    """Exercise persistent numeric clocks and valid-evidence recovery."""

    def test_twelve_hours_is_boundary_and_fresh_repeats_do_not_reset(self) -> None:
        record = plateau()
        self.assertNotEqual(record["status"], "sensor_error")
        now = START + 12 * 3600 + 1
        record = state_machine.evaluate(record, sample(now), now)
        self.assertEqual(record["status"], "sensor_error")
        self.assertEqual(record["last_value_change_at"], START)
        self.assertEqual(record["reliability_reason"], "saturated")
        self.assertIsNone(record["moisture"])

    def test_constant_midrange_is_not_accepted_as_fresh_healthy(self) -> None:
        record = {}
        for offset in range(0, 12 * 3600 + 301, 300):
            now = START + offset
            record = state_machine.evaluate(record, sample(now, 50, 150), now)
        self.assertEqual(record["status"], "sensor_error")
        self.assertEqual(record["reliability_reason"], "unchanged")

    def test_restart_and_unavailable_do_not_reset_numeric_change_clock(self) -> None:
        record = plateau()
        record = json.loads(json.dumps(record))
        now = START + 12 * 3600 + 301
        record = state_machine.evaluate(record, sample(now, None, None), now)
        record = state_machine.evaluate(record, sample(now + 300), now + 300)
        self.assertEqual(record["status"], "sensor_error")
        self.assertEqual(record["last_value_change_at"], START)

    def test_one_glitch_does_not_clear_incident(self) -> None:
        now = START + 12 * 3600 + 300
        record = state_machine.evaluate(plateau(), sample(now), now)
        incident = record.get("reliability_incident_started_at")
        record = state_machine.evaluate(record, sample(now + 300, 80, 190), now + 300)
        self.assertEqual(record["status"], "sensor_error")
        record = state_machine.evaluate(record, sample(now + 600), now + 600)
        self.assertEqual(record["status"], "sensor_error")
        self.assertEqual(record["reliability_incident_started_at"], incident)

    def test_three_physical_changed_samples_over_ten_minutes_restore(self) -> None:
        now = START + 12 * 3600 + 300
        record = state_machine.evaluate(plateau(), sample(now), now)
        for offset, value, raw in [(300, 80, 190), (600, 79, 188)]:
            record = state_machine.evaluate(
                record, sample(now + offset, value, raw), now + offset
            )
            self.assertEqual(record["status"], "sensor_error")
        record = state_machine.evaluate(record, sample(now + 900, 78, 187), now + 900)
        self.assertNotEqual(record["status"], "sensor_error")
        self.assertTrue(record["reliability_valid"])

    def test_duplicate_timestamp_cannot_count_as_recovery(self) -> None:
        now = START + 12 * 3600 + 300
        record = state_machine.evaluate(plateau(), sample(now), now)
        for offset in [300, 600, 900]:
            record = state_machine.evaluate(
                record, sample(now + 300, 80, 190), now + offset
            )
        self.assertEqual(record["status"], "sensor_error")

    def test_future_and_nan_are_errors(self) -> None:
        for reading in [sample(START + 600), sample(START, float("nan"))]:
            record = state_machine.evaluate({}, reading, START)
            self.assertEqual(record["status"], "sensor_error")
            self.assertFalse(record.get("reliability_valid", True))

    def test_saturated_raw_noise_does_not_infer_watering(self) -> None:
        points = [
            [START + i * 300, v]
            for i, v in enumerate([385, 370, 376, 458, 438, 440, 378])
        ]
        result = analytics.analyze({}, points, points[-1][0], calibration=[67, 221])
        self.assertIsNone(result.get("last_watering_at"))

    def test_real_dry_to_wet_rise_can_infer_watering(self) -> None:
        points = [
            [START + i * 300, v] for i, v in enumerate([70, 72, 71, 160, 165, 170])
        ]
        result = analytics.analyze({}, points, points[-1][0], calibration=[67, 221])
        self.assertEqual(result.get("last_watering_at"), points[3][0])

    def test_recorder_bootstrap_ignores_outages_and_restored_equal_values(self) -> None:
        now = START + 5 * 86400
        previous = {
            "value_history": [
                [START, 100],
                [START + 86400, None],
                [START + 86400 + 300, 100],
                [now - 300, 100],
            ]
        }
        result = state_machine.evaluate(previous, sample(now), now)
        self.assertEqual(result["status"], "sensor_error")
        self.assertEqual(result["last_value_change_at"], START)

    def test_stale_history_cannot_create_a_watering_event(self) -> None:
        points = [
            [START + i * 300, v] for i, v in enumerate([70, 72, 71, 160, 165, 170])
        ]
        result = analytics.analyze(
            {}, points, points[-1][0] + 86400, calibration=[67, 221]
        )
        self.assertIsNone(result.get("last_watering_at"))

    def test_previous_auto_estimate_from_saturated_noise_is_retracted(self) -> None:
        points = [
            [START + i * 300, v]
            for i, v in enumerate([385, 370, 376, 458, 438, 440, 378])
        ]
        previous = {"last_watering_at": points[3][0], "last_watering_source": "auto"}
        result = analytics.analyze(
            previous, points, points[-1][0], calibration=[67, 221]
        )
        self.assertIsNone(result.get("last_watering_at"))
        self.assertEqual(result.get("untrusted_last_watering_at"), points[3][0])

    def test_manual_watering_record_survives_invalid_source(self) -> None:
        points = [
            [START + i * 300, v]
            for i, v in enumerate([385, 370, 376, 458, 438, 440, 378])
        ]
        previous = {
            "last_watering_at": "2026-11-03",
            "last_watering_source": "user_report",
        }
        result = analytics.analyze(
            previous, points, points[-1][0], calibration=[67, 221]
        )
        self.assertEqual(result["last_watering_at"], "2026-11-03")

    def test_failed_history_bootstrap_does_not_grant_twelve_new_hours(self) -> None:
        reading = sample(START)
        reading["history_ready"] = False
        result = state_machine.evaluate({}, reading, START)
        self.assertEqual(result["status"], "sensor_error")
        self.assertEqual(result["reliability_reason"], "history_unavailable")
        self.assertIsNone(result.get("last_value_change_at"))

    def test_startup_stale_cause_upgrades_to_frozen_without_new_incident(self) -> None:
        now = START + 12 * 3600 + 1800
        record = state_machine.evaluate(plateau(), sample(now - 1800), now)
        incident = record.get("reliability_incident_started_at")
        record = state_machine.evaluate(record, sample(now + 300), now + 300)
        record = state_machine.evaluate(record, sample(now + 600, 99, 210), now + 600)
        for offset in [900, 1200, 1500]:
            record = state_machine.evaluate(record, sample(now + offset), now + offset)
        self.assertEqual(record["status"], "sensor_error")
        self.assertEqual(record["reliability_incident_started_at"], incident)
        self.assertEqual(record["reliability_incident_reason"], "saturated")

    def test_replacement_source_does_not_inherit_old_incident_on_next_read(
        self,
    ) -> None:
        now = START + 12 * 3600 + 300
        record = state_machine.evaluate(plateau(), sample(now), now)
        first = dict(sample(now + 300, 60, 170), source_identity="sensor.new")
        record.update(
            value_history_source="sensor.new",
            value_history=[[now + 100, 59], [now + 300, 60]],
        )
        record = state_machine.evaluate(record, first, now + 300)
        record = state_machine.evaluate(
            record, dict(first, sample_at=now + 600), now + 600
        )
        self.assertTrue(record["reliability_valid"])
        self.assertIsNone(record.get("reliability_incident_started_at"))

    def test_watering_from_below_dry_calibration_is_not_rejected(self) -> None:
        points = [
            [START + i * 300, v] for i, v in enumerate([67, 66, 65, 160, 165, 170])
        ]
        result = analytics.analyze({}, points, points[-1][0], calibration=[67, 221])
        self.assertEqual(result.get("last_watering_at"), points[3][0])

    def test_owner_twelve_hour_rule_also_flags_lower_clipping(self) -> None:
        record = {}
        for offset in range(0, 12 * 3600 + 301, 300):
            now = START + offset
            record = state_machine.evaluate(
                record, sample(now, 0, 60 + (offset % 600) / 100), now
            )
        self.assertEqual(record["status"], "sensor_error")
        self.assertEqual(record.get("reliability_reason"), "lower_saturation")
        self.assertIsNotNone(record["due_since"])

    def test_live_changes_can_establish_recovery_without_recorder(self) -> None:
        record = {}
        for offset, value, raw in [
            (0, 50, 150),
            (300, 49, 149),
            (600, 48, 148),
            (900, 47, 147),
        ]:
            reading = dict(sample(START + offset, value, raw), history_ready=False)
            record = state_machine.evaluate(record, reading, START + offset)
        self.assertTrue(record["reliability_valid"])
        self.assertNotEqual(record["status"], "sensor_error")

    def test_replacement_bootstraps_its_own_old_constant_history(self) -> None:
        now = START + 5 * 86400
        previous = {
            "source_identity": "sensor.old",
            "value_history_source": "sensor.new",
            "value_history": [[START, 100], [now - 300, 100]],
        }
        reading = dict(sample(now), source_identity="sensor.new")
        record = state_machine.evaluate(previous, reading, now)
        self.assertEqual(record["status"], "sensor_error")
        self.assertEqual(record["last_value_change_at"], START)

    def test_rejected_auto_evidence_does_not_revive_after_recovery(self) -> None:
        points = [
            [START + i * 300, v] for i, v in enumerate([70, 72, 71, 160, 160, 160])
        ]
        previous = analytics.analyze({}, points, points[-1][0], calibration=[67, 221])
        previous = analytics.analyze(
            previous,
            points,
            points[-1][0],
            calibration=[67, 221],
            reliable=False,
            unreliable_since=points[3][0],
        )
        recovered = analytics.analyze(
            previous, points, points[-1][0], calibration=[67, 221]
        )
        self.assertIsNone(recovered.get("last_watering_at"))
        self.assertIn(points[3][0], recovered.get("rejected_watering_events", []))

    def test_legacy_short_samples_cannot_bootstrap_unknown_history(self) -> None:
        previous = {
            "source_identity": "sensor.soil",
            "samples": [[START - 600, 100], [START - 300, 100]],
        }
        result = state_machine.evaluate(
            previous, dict(sample(START), history_ready=False), START
        )
        self.assertFalse(result["reliability_valid"])
        self.assertEqual(result["reliability_reason"], "history_unavailable")
        self.assertIsNone(result["last_value_change_at"])

    def test_replacement_cannot_inherit_old_history_readiness(self) -> None:
        old = plateau()
        old.update(
            value_history_source="sensor.soil",
            reliability_history_ready=True,
            value_history=[[START, 60]],
        )
        now = START + 12 * 3600 + 300
        result = state_machine.evaluate(
            old,
            dict(
                sample(now, 60, 170), source_identity="sensor.new", history_ready=True
            ),
            now,
        )
        self.assertFalse(result["reliability_valid"])
        self.assertEqual(result["reliability_reason"], "history_unavailable")
        result = state_machine.evaluate(
            result,
            dict(
                sample(now + 300, 60, 170),
                source_identity="sensor.new",
                history_ready=False,
            ),
            now + 300,
        )
        self.assertFalse(result["reliability_valid"])
        self.assertIsNone(result["last_value_change_at"])

    def test_first_inference_after_recovery_excludes_fault_period(self) -> None:
        record = {}
        points = []
        for offset in range(0, 12 * 3600 + 301, 300):
            now = START + offset
            points.append([now, 65])
            record = state_machine.evaluate(record, sample(now, 0, 65), now)
        for offset, value, raw in [(300, 60, 160), (600, 62, 165), (900, 64, 170)]:
            stamp = now + offset
            points.append([stamp, raw])
            record = state_machine.evaluate(record, sample(stamp, value, raw), stamp)
            record.update(
                analytics.analyze(
                    record,
                    points,
                    stamp,
                    calibration=[67, 221],
                    reliable=record["reliability_valid"],
                )
            )
        self.assertTrue(record["reliability_valid"])
        self.assertIsNone(record.get("last_watering_at"))
        record = json.loads(json.dumps(record))
        result = analytics.analyze(record, points, points[-1][0], calibration=[67, 221])
        self.assertIsNone(result.get("last_watering_at"))

    def test_wet_recovery_does_not_reissue_historical_dry_warning(self) -> None:
        record = {}
        for offset in range(0, 12 * 3600 + 301, 300):
            now = START + offset
            record = state_machine.evaluate(record, sample(now, 0, 65), now)
        for offset, value, raw in [(300, 60, 160), (600, 62, 165), (900, 64, 170)]:
            stamp = now + offset
            record = state_machine.evaluate(record, sample(stamp, value, raw), stamp)
        self.assertTrue(record["reliability_valid"])
        self.assertNotIn(record["status"], ["due", "overdue"])
        for offset in [1200, 1500, 1800]:
            record = state_machine.evaluate(
                record, sample(now + offset, 64, 170), now + offset
            )
        self.assertIsNone(record["due_since"])

    def test_lower_to_upper_clip_does_not_claim_current_lower_clip(self) -> None:
        previous = {"source_identity": "sensor.soil", "value_history": [[START, 0]]}
        now = START + 13 * 3600
        record = state_machine.evaluate(previous, sample(now, 0, 65), now)
        record = state_machine.evaluate(record, sample(now + 300, 100, 300), now + 300)
        self.assertFalse(record["reliability_valid"])
        self.assertEqual(record["reliability_reason"], "recovering")
        self.assertEqual(record["reliability_incident_reason"], "lower_saturation")

    def test_forecast_cannot_reuse_quarantined_history_after_recovery(self) -> None:
        record = {}
        points = []
        for index in range(157):
            now = START + index * 300
            raw = 200 - index * 0.25
            points.append([now, raw])
            record = state_machine.evaluate(record, sample(now, 50, raw), now)
        for index, value in enumerate([60, 59, 58], start=157):
            now = START + index * 300
            raw = 200 - index * 0.25
            points.append([now, raw])
            record = state_machine.evaluate(record, sample(now, value, raw), now)
        self.assertTrue(record["reliability_valid"])
        result = analytics.analyze(record, points, now, calibration=[67, 221])
        self.assertNotEqual(result["forecast_status"], "available")
        self.assertIsNone(result["forecast_due_at"])

    def test_loaded_trustworthy_history_recovers_without_value_change(self) -> None:
        record = state_machine.evaluate(
            {}, dict(sample(START, 50, 150), history_ready=False), START
        )
        record.update(
            value_history_source="sensor.soil",
            value_history=[[START - 7200, 49], [START - 3600, 50]],
        )
        for offset in [300, 600, 900]:
            reading = dict(
                sample(START + offset, 50, 150), history_ready=True, recorder_ready=True
            )
            record = state_machine.evaluate(record, reading, START + offset)
        self.assertTrue(record["reliability_valid"])

    def test_old_recovery_does_not_downgrade_later_sparse_dry_window(self) -> None:
        now = START + 7200
        previous = {
            "source_identity": "sensor.soil",
            "due_since": START,
            "notified_status": "due",
            "reliability_recovered_at": START - 86400,
            "samples": [[now - 600, 20], [now - 300, 21]],
            "last_sample_at": now - 300,
            "last_value_change_at": now - 300,
            "last_observed_value": 21,
        }
        record = state_machine.evaluate(previous, sample(now, 22, 100), now)
        self.assertEqual(record["status"], "due")
        self.assertEqual(record["notified_status"], "due")

    def test_startup_absence_does_not_create_a_new_latched_incident(self) -> None:
        previous = {
            "source_identity": "sensor.soil",
            "due_since": START - 3600,
            "notified_status": "due",
            "last_value_change_at": START - 300,
            "last_observed_value": 20,
            "last_sample_at": START - 300,
            "samples": [[START - 900, 19], [START - 600, 21], [START - 300, 20]],
        }
        record = state_machine.evaluate(
            previous,
            dict(sample(START, None, None), sample_at=None, starting=True),
            START,
        )
        self.assertIsNone(record.get("reliability_incident_started_at"))
        record = state_machine.evaluate(record, sample(START + 60, 21, 100), START + 60)
        self.assertEqual(record["status"], "due")
        self.assertEqual(record["notified_status"], "due")

    def test_startup_does_not_hide_an_already_old_plateau(self) -> None:
        now = START + 13 * 3600
        previous = plateau()
        record = state_machine.evaluate(
            previous, dict(sample(now, None, None), sample_at=None, starting=True), now
        )
        self.assertEqual(record["status"], "sensor_error")
        self.assertIsNotNone(record.get("reliability_incident_started_at"))

    def test_owner_freeze_threshold_cannot_be_overridden(self) -> None:
        previous = {
            "source_identity": "sensor.soil",
            "last_observed_value": 50,
            "last_value_change_at": START,
            "last_source_value": 150,
            "last_source_change_at": START,
        }
        for configured in [3600, 86400]:
            with self.subTest(configured=configured):
                early = START + 2 * 3600
                record = state_machine.evaluate(
                    previous,
                    sample(early, 50, 150),
                    early,
                    {"unchanged_seconds": configured},
                )
                self.assertTrue(record["reliability_valid"])
                late = START + 13 * 3600
                record = state_machine.evaluate(
                    previous,
                    sample(late, 50, 150),
                    late,
                    {"unchanged_seconds": configured},
                )
                self.assertFalse(record["reliability_valid"])

    def test_short_missing_poll_preserves_confirmed_drying_history(self) -> None:
        points = [[START + i * 300, 200 - i * 0.25] for i in range(157)]
        now, raw = points[-1]
        record = state_machine.evaluate(
            {}, sample(now, (raw - 67) * 100 / 154, raw), now
        )
        before = analytics.analyze(record, points, now, calibration=[67, 221])
        self.assertEqual(before["forecast_status"], "available")
        record = state_machine.evaluate(
            record,
            dict(sample(now + 300, None, None), measurement_missing=True),
            now + 300,
        )
        self.assertFalse(record["reliability_valid"])
        for index in range(158, 161):
            stamp, raw = START + index * 300, 200 - index * 0.25
            points.append([stamp, raw])
            record = state_machine.evaluate(
                record, sample(stamp, (raw - 67) * 100 / 154, raw), stamp
            )
        self.assertTrue(record["reliability_valid"])
        result = analytics.analyze(record, points, stamp, calibration=[67, 221])
        self.assertEqual(result["forecast_status"], "available")

    def test_transport_gaps_preserve_only_continuous_wet_history(self) -> None:
        original = [[START + i * 300, 210 + i % 2] for i in range(865)]
        now = original[-1][0]
        for gap, expected in [(600, "prolonged"), (3600, "checking")]:
            with self.subTest(gap=gap):
                points = list(original)
                record = state_machine.evaluate({}, sample(now, 94, 211), now)
                record = state_machine.evaluate(
                    record,
                    dict(sample(now + 300, None, None), measurement_missing=True),
                    now + 300,
                )
                for offset in [gap, gap + 300, gap + 600]:
                    stamp = now + offset
                    raw = 210 + (offset // 300) % 2
                    points.append([stamp, raw])
                    record = state_machine.evaluate(
                        record, sample(stamp, (raw - 67) * 100 / 154, raw), stamp
                    )
                self.assertTrue(record["reliability_valid"])
                result = analytics.analyze(record, points, stamp, calibration=[67, 221])
                self.assertEqual(result["wet_risk"], expected)
                if gap == 600:
                    self.assertEqual(result["wet_since"], START)

    def test_transport_absence_cannot_release_a_frozen_quarantine(self) -> None:
        now = START + 13 * 3600
        record = state_machine.evaluate(plateau(), sample(now), now)
        record = state_machine.evaluate(
            record,
            dict(sample(now + 300, None, None), measurement_missing=True),
            now + 300,
        )
        record = state_machine.evaluate(record, sample(now + 600), now + 600)
        self.assertFalse(record["reliability_valid"])
        self.assertEqual(record["reliability_invalid_intervals"], [[START, None]])

    def test_freeze_crossing_during_absence_quarantines_the_plateau(self) -> None:
        record = {}
        points = []
        for index in range(145):
            now, raw = START + index * 300, 200 - index * 0.25
            points.append([now, raw])
            record = state_machine.evaluate(record, sample(now, 50, raw), now)
        self.assertTrue(record["reliability_valid"])
        record = state_machine.evaluate(
            record,
            dict(sample(now + 300, None, None), measurement_missing=True),
            now + 300,
        )
        self.assertEqual(record["reliability_incident_reason"], "unchanged")
        self.assertEqual(record["reliability_incident_value"], 50)
        self.assertEqual(record["reliability_invalid_intervals"], [[START, None]])
        for index, value in enumerate([49, 48, 47], start=146):
            stamp, raw = START + index * 300, 200 - index * 0.25
            points.append([stamp, raw])
            record = state_machine.evaluate(record, sample(stamp, value, raw), stamp)
        self.assertTrue(record["reliability_valid"])
        result = analytics.analyze(record, points, stamp, calibration=[67, 221])
        self.assertIsNone(result["forecast_due_at"])
        self.assertNotEqual(result["forecast_status"], "available")

    def test_absent_freeze_keeps_the_previous_value_as_recovery_baseline(self) -> None:
        now = START + 13 * 3600
        for baseline in [0, 50, 100]:
            with self.subTest(baseline=baseline):
                previous = {
                    "source_identity": "sensor.soil",
                    "last_observed_value": baseline,
                    "last_value_change_at": START,
                    "last_source_value": 150,
                    "last_source_change_at": now - 300,
                }
                record = state_machine.evaluate(
                    previous,
                    dict(sample(now, None, None), measurement_missing=True),
                    now,
                )
                for offset, value, raw in [
                    (300, 49, 149),
                    (600, baseline, 151),
                    (900, baseline, 152),
                ]:
                    stamp = now + offset
                    record = state_machine.evaluate(
                        record, sample(stamp, value, raw), stamp
                    )
                self.assertFalse(record["reliability_valid"])
                self.assertEqual(record["reliability_incident_value"], baseline)

    def test_absence_does_not_hide_an_independent_raw_freeze(self) -> None:
        now = START + 13 * 3600
        previous = {
            "source_identity": "sensor.soil",
            "raw_source_identity": "sensor.raw",
            "last_observed_value": 50,
            "last_value_change_at": now - 300,
            "last_source_value": 150,
            "last_source_change_at": START,
        }
        record = state_machine.evaluate(
            previous,
            dict(sample(now, None, None), measurement_missing=True),
            now,
        )
        self.assertEqual(record["reliability_incident_reason"], "raw_unchanged")
        self.assertEqual(record["reliability_invalid_intervals"], [[START, None]])
        self.assertEqual(record["reliability_incident_raw"], 150)

    def test_unmapped_index_does_not_inherit_an_independent_raw_clock(self) -> None:
        now = START + 13 * 3600
        previous = {
            "source_identity": "sensor.soil",
            "last_observed_value": 50,
            "last_value_change_at": now - 300,
            "adc_identity": "sensor.soil",
            "adc_samples": [[START, 50]],
            "last_source_value": 50,
            "last_source_change_at": START,
        }
        record = state_machine.evaluate(
            previous,
            dict(sample(now, 51, None), raw_source_identity=None),
            now,
        )
        self.assertTrue(record["reliability_valid"])
        self.assertIsNone(record["last_source_change_at"])

    def test_replaced_raw_channel_cannot_inherit_an_old_clock(self) -> None:
        now = START + 13 * 3600
        previous = {
            "source_identity": "sensor.soil",
            "last_observed_value": 50,
            "last_value_change_at": now - 300,
            "raw_source_identity": "sensor.old",
            "adc_identity": "sensor.old",
            "adc_samples": [[START, 150]],
            "last_source_value": 150,
            "last_source_change_at": START,
        }
        record = state_machine.evaluate(previous, sample(now, 51, 150), now)
        self.assertTrue(record["reliability_valid"])
        self.assertEqual(record["last_source_change_at"], now)

    def test_removed_raw_mapping_preserves_incident_until_stable_recovery(self) -> None:
        now = START + 13 * 3600
        previous = {
            "source_identity": "sensor.soil",
            "last_observed_value": 50,
            "last_value_change_at": now - 300,
            "raw_source_identity": "sensor.raw",
            "last_source_value": 150,
            "last_source_change_at": START,
        }
        record = state_machine.evaluate(previous, sample(now, 50, 150), now)
        incident = record["reliability_incident_started_at"]
        self.assertEqual(record["reliability_incident_reason"], "raw_unchanged")
        for offset, value in [(300, 51), (600, 52), (900, 53)]:
            stamp = now + offset
            record = state_machine.evaluate(
                record,
                dict(sample(stamp, value, None), raw_source_identity=None),
                stamp,
            )
            if offset < 900:
                self.assertFalse(record["reliability_valid"])
                self.assertEqual(record["reliability_incident_started_at"], incident)
        self.assertTrue(record["reliability_valid"])
        self.assertEqual(record["reliability_invalid_intervals"], [[START, now + 900]])

    def test_simultaneous_frozen_clocks_quarantine_from_the_earliest(self) -> None:
        now = START + 14 * 3600
        previous = {
            "source_identity": "sensor.soil",
            "last_observed_value": 100,
            "last_value_change_at": START + 3600,
            "raw_source_identity": "sensor.raw",
            "last_source_value": 350,
            "last_source_change_at": START,
        }
        record = state_machine.evaluate(previous, sample(now, 100, 350), now)
        self.assertEqual(record["reliability_incident_reason"], "saturated")
        self.assertEqual(record["reliability_invalid_intervals"], [[START, None]])
        self.assertEqual(record["reliability_incident_value_changed_at"], START)

    def test_legacy_raw_clock_uses_current_history_after_loader_rewrites_identity(
        self,
    ) -> None:
        now = START + 13 * 3600
        previous = {
            "source_identity": "sensor.soil",
            "last_observed_value": 50,
            "last_value_change_at": now - 300,
            "adc_identity": "sensor.raw",
            "adc_samples": [[now - 300, 150]],
            "last_source_value": 150,
            "last_source_change_at": START,
        }
        record = state_machine.evaluate(previous, sample(now, 51, 150), now)
        self.assertTrue(record["reliability_valid"])
        self.assertEqual(record["last_source_change_at"], now - 300)

    def test_new_raw_mapping_bootstraps_matching_known_frozen_history(self) -> None:
        now = START + 13 * 3600
        previous = {
            "source_identity": "sensor.soil",
            "last_observed_value": 50,
            "last_value_change_at": now - 300,
            "raw_source_identity": "sensor.old",
            "adc_identity": "sensor.raw",
            "adc_samples": [[START, 150]],
            "last_source_value": 150,
            "last_source_change_at": now - 300,
        }
        record = state_machine.evaluate(previous, sample(now, 51, 150), now)
        self.assertFalse(record["reliability_valid"])
        self.assertEqual(record["reliability_incident_reason"], "raw_unchanged")
        self.assertEqual(record["reliability_invalid_intervals"], [[START, None]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
