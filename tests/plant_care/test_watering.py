"""Verify sustained raw watering evidence without human confirmation or rescaling."""

import json
import unittest

from calibration_fixtures import load_component_module

analytics = load_component_module("analytics")
START = 1794902400.0
CALIBRATION = [67, 1298]


def measured_rise(baseline: float = 300, rise: float = 240) -> list:
    points = [[START + i * 300, baseline + (i % 3 - 1) * 3] for i in range(12)]
    points.extend(
        [START + i * 300, baseline + rise + (i % 3 - 1) * 3] for i in range(12, 19)
    )
    return points


class WateringTests(unittest.TestCase):
    """Exercise actual public analytics with bounded deterministic raw windows."""

    def test_stable_wet_baseline_allows_automatic_top_up(self) -> None:
        points = measured_rise(950)
        result = analytics.analyze({}, points, points[-1][0], calibration=CALIBRATION)
        self.assertEqual(result.get("last_watering_source"), "auto")
        self.assertEqual(result["last_watering_precision"], "interval")
        self.assertEqual(result["last_watering_window_start"], START + 3300)
        self.assertEqual(result["last_watering_window_end"], START + 3600)
        self.assertEqual(result["last_watering_detected_at"], START + 5400)
        self.assertFalse(result["last_watering_retroactive"])

    def test_rise_waits_for_thirty_minutes_of_retained_evidence(self) -> None:
        points = measured_rise()
        for count in (13, 15, 18):
            with self.subTest(count=count):
                result = analytics.analyze(
                    {}, points[:count], points[count - 1][0], calibration=CALIBRATION
                )
                self.assertNotEqual(result.get("last_watering_source"), "auto")
        result = analytics.analyze({}, points, points[-1][0], calibration=CALIBRATION)
        self.assertEqual(result.get("last_watering_source"), "auto")

    def test_three_spikes_that_fall_back_are_not_watering(self) -> None:
        points = measured_rise(rise=800)
        for i in range(15, 19):
            points[i][1] = 300
        result = analytics.analyze({}, points, points[-1][0], calibration=CALIBRATION)
        self.assertNotEqual(result.get("last_watering_source"), "auto")

    def test_small_sustained_top_up_is_not_hidden_by_calibration_span(self) -> None:
        points = measured_rise(950, rise=40)
        result = analytics.analyze({}, points, points[-1][0], calibration=CALIBRATION)
        self.assertEqual(result.get("last_watering_source"), "auto")

    def test_several_rising_samples_then_plateau_are_detected(self) -> None:
        points = measured_rise(950)[:12]
        points.extend(
            [START + i * 300, value]
            for i, value in enumerate(
                [975, 1000, 1025, 1050, 1051, 1049, 1050, 1051, 1050, 1049], 12
            )
        )
        result = analytics.analyze({}, points, points[-1][0], calibration=CALIBRATION)
        self.assertEqual(result.get("last_watering_source"), "auto")
        self.assertEqual(result["last_watering_window_end"], START + 3600)

    def test_small_change_within_observed_noise_is_not_watering(self) -> None:
        points = measured_rise(950, rise=10)
        result = analytics.analyze({}, points, points[-1][0], calibration=CALIBRATION)
        self.assertNotEqual(result.get("last_watering_source"), "auto")

    def test_gradual_background_rise_is_not_an_abrupt_watering_event(self) -> None:
        points = [[START + i * 300, 800 + i * 2 + i % 2] for i in range(49)]
        result = analytics.analyze({}, points, points[-1][0], calibration=CALIBRATION)
        self.assertNotEqual(result.get("last_watering_source"), "auto")

    def test_conflicting_physical_timestamp_blocks_the_whole_window(self) -> None:
        points = measured_rise()
        points.append([START + 3600, 300])
        result = analytics.analyze({}, points, START + 5400, calibration=CALIBRATION)
        self.assertNotEqual(result.get("last_watering_source"), "auto")

    def test_observed_noise_scales_required_rise(self) -> None:
        points = measured_rise(rise=160)
        for i, point in enumerate(points):
            point[1] += (-80, 80)[i % 2]
        result = analytics.analyze({}, points, points[-1][0], calibration=CALIBRATION)
        self.assertNotEqual(result.get("last_watering_source"), "auto")

    def test_outage_or_known_invalid_interval_breaks_event_evidence(self) -> None:
        points = measured_rise()
        for previous, changed in (
            ({"reliability_invalid_intervals": [[START + 3000, START + 3700]]}, points),
            (
                {},
                [[t + (3600 if i >= 12 else 0), v] for i, (t, v) in enumerate(points)],
            ),
        ):
            with self.subTest(previous=previous):
                result = analytics.analyze(
                    previous, changed, changed[-1][0], calibration=CALIBRATION
                )
                self.assertNotEqual(result.get("last_watering_source"), "auto")

    def test_same_raw_signal_cannot_become_watering_after_rescale(self) -> None:
        points = measured_rise(rise=0)
        result = analytics.analyze({}, points, points[-1][0], calibration=[67, 221])
        result = analytics.analyze(
            result, points, points[-1][0] + 1, calibration=CALIBRATION
        )
        self.assertNotEqual(result.get("last_watering_source"), "auto")

    def test_persisted_history_deduplicates_refresh_and_restart(self) -> None:
        points = measured_rise()
        first = analytics.analyze({}, points, points[-1][0], calibration=CALIBRATION)
        restored = json.loads(json.dumps(first))
        points.extend([[START + i * 300, 540 + i % 3] for i in range(19, 31)])
        after = analytics.analyze(
            restored, points, points[-1][0], calibration=CALIBRATION
        )
        self.assertEqual(len(after["watering_history"]), 1)
        self.assertEqual(after["watering_history"], first["watering_history"])
        self.assertEqual(after["last_watering_detected_at"], START + 5400)

    def test_historical_estimate_records_actual_detection_time(self) -> None:
        points = measured_rise()
        points.extend([[START + i * 300, 540 + i % 3] for i in range(19, 43)])
        result = analytics.analyze({}, points, points[-1][0], calibration=CALIBRATION)
        self.assertEqual(result.get("last_watering_source"), "auto")
        self.assertTrue(result["last_watering_retroactive"])
        self.assertEqual(result["last_watering_detected_at"], points[-1][0])
        self.assertEqual(result["last_watering_window_end"], START + 3600)

    def test_later_invalid_interval_retracts_estimate_without_erasing_history(
        self,
    ) -> None:
        points = measured_rise()
        first = analytics.analyze({}, points, points[-1][0], calibration=CALIBRATION)
        first["reliability_invalid_intervals"] = [[START + 3500, START + 5500]]
        result = analytics.analyze(first, points, START + 5500, calibration=CALIBRATION)
        self.assertNotEqual(result.get("last_watering_source"), "auto")
        self.assertEqual(len(result["watering_history"]), 1)
        self.assertFalse(result["watering_history"][0]["valid"])
        self.assertEqual(result["watering_history"][0]["retracted_at"], START + 5500)

    def test_late_baseline_conflict_retracts_full_evidence_window(self) -> None:
        points = measured_rise()
        first = analytics.analyze({}, points, points[-1][0], calibration=CALIBRATION)
        self.assertEqual(first["last_watering_source"], "auto")
        for contradiction in (
            {"watering_conflicts": [START + 300]},
            {"reliability_invalid_intervals": [[START + 300, START + 600]]},
        ):
            with self.subTest(contradiction=contradiction):
                prior = {**json.loads(json.dumps(first)), **contradiction}
                result = analytics.analyze(
                    prior, points, START + 5500, calibration=CALIBRATION
                )
                self.assertFalse(result["watering_history"][0]["valid"])
                self.assertNotEqual(result["last_watering_source"], "auto")
                self.assertEqual(result["watering_history"][0]["evidence_start"], START)
                self.assertEqual(
                    result["watering_history"][0]["window_start"], START + 3300
                )

    def test_clustered_short_excursion_does_not_confirm_thirty_minute_rise(
        self,
    ) -> None:
        points = measured_rise()[:12]
        points.extend([START + 3600 + i * 15, 540] for i in range(13))
        points.extend([START + 3600 + offset, 300] for offset in (600, 1200, 1800))
        result = analytics.analyze({}, points, START + 5400, calibration=CALIBRATION)
        self.assertNotEqual(result.get("last_watering_source"), "auto")
        self.assertEqual(result["watering_history"], [])

    def test_new_approximate_report_preserves_known_older_day_without_fake_time(
        self,
    ) -> None:
        watering = load_component_module("watering")
        record = {
            "last_watering_at": "2026-11-03",
            "last_watering_source": "user_report",
        }
        report = {
            "date": "2026-11-17",
            "precision": "approximate_day",
            "timezone": "Europe/Belgrade",
            "note": "Вчера вроде",
        }
        watering.record_report(record, report, START)
        self.assertEqual(
            [event["at"] for event in record["watering_history"]],
            ["2026-11-03", "2026-11-17"],
        )
        self.assertIsNone(record["watering_history"][0]["detected_at"])
        self.assertEqual(record["watering_history"][1]["detected_at"], START)
        watering.record_report(record, report, START + 60)
        self.assertEqual(len(record["watering_history"]), 2)
        self.assertEqual(record["last_watering_at"], "2026-11-17")
        self.assertEqual(record["last_watering_precision"], "approximate_day")

    def test_unreliable_poll_is_a_separate_watering_discontinuity(self) -> None:
        points = measured_rise()
        prior = analytics.analyze(
            {}, points[:12], START + 3400, calibration=CALIBRATION, reliable=False
        )
        result = analytics.analyze(
            prior, points, points[-1][0], calibration=CALIBRATION
        )
        self.assertNotEqual(result.get("last_watering_source"), "auto")
        self.assertEqual(result["watering_history"], [])


if __name__ == "__main__":
    unittest.main()
