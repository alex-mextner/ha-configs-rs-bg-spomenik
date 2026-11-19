"""Guard the deployed inference thresholds while extracting lintable helpers."""

import unittest

from custom_components.plant_care_alerts.analytics import analyze


class AnalyticsTests(unittest.TestCase):
    """Verify physical-sample inference, stale handling and bounded forecasts."""

    def test_saturated_readings_have_no_forecast_and_confirm_prolonged_risk(
        self,
    ) -> None:
        points = [[i * 900, 390] for i in range(289)]
        result = analyze({}, points, points[-1][0], calibration=[67, 221])
        self.assertEqual(result["wet_risk"], "prolonged")
        self.assertEqual(result["forecast_status"], "saturated")
        self.assertIsNone(result["forecast_due_at"])

    def test_contiguous_drying_forecast_uses_relative_calibration(self) -> None:
        points = [[i * 300, 80 - i / 6] for i in range(157)]
        result = analyze({}, points, points[-1][0], calibration=[0, 100])
        self.assertEqual(result["forecast_status"], "available")
        self.assertAlmostEqual(result["forecast_due_at"], 27.5 * 3600)
        self.assertEqual(result["wet_risk"], "normal")

    def test_stale_or_invalid_calibration_cannot_leave_an_old_forecast(self) -> None:
        previous = {"forecast_status": "available", "forecast_due_at": 10000}
        for calibration in (None, [100, 100], [100, 0]):
            with self.subTest(calibration=calibration):
                result = analyze(previous, [[100, 50]], 101, calibration=calibration)
                self.assertIsNone(result["forecast_due_at"])
        result = analyze(previous, [[100, 50]], 1301, calibration=[0, 100])
        self.assertEqual(result["forecast_status"], "stale")
        self.assertIsNone(result["forecast_due_at"])
        self.assertEqual(previous["forecast_due_at"], 10000)

    def test_watering_requires_a_baseline_and_thirty_minute_confirmation(self) -> None:
        points = [[i * 300, 10] for i in range(12)]
        points.extend([[i * 300, 70] for i in range(12, 19)])
        result = analyze({}, points, points[-1][0], calibration=[0, 100])
        self.assertEqual(result["last_watering_at"], 3600)
        self.assertEqual(result["last_watering_source"], "auto")
        for values in ([10, 10, 10, 70, 10, 10], [10, 10, 10, 70, 70, 70]):
            with self.subTest(values=values):
                samples = [[i * 300, value] for i, value in enumerate(values)]
                self.assertNotIn(
                    "last_watering_at", analyze({}, samples, 1500, calibration=[0, 100])
                )

    def test_outages_break_watering_and_wet_continuity(self) -> None:
        points = [[0, 10], [300, 10], [600, 10], [3000, 70], [3300, 70], [3600, 70]]
        self.assertNotIn(
            "last_watering_at", analyze({}, points, 3600, calibration=[0, 100])
        )
        points = [[0, 100], [300, 100], [72 * 3600, 100]]
        result = analyze({}, points, points[-1][0], calibration=[0, 100])
        self.assertEqual(result["wet_risk"], "checking")
        self.assertEqual(result["wet_since"], points[-1][0])

    def test_duplicates_future_and_nonfinite_samples_cannot_make_a_forecast(
        self,
    ) -> None:
        points = [[0, 50], [0, 80], [1000, 50], [10, "nan"], ["bad", 10], [1]]
        result = analyze({}, points, 1, calibration=[0, 100])
        self.assertEqual(result["forecast_status"], "insufficient_data")
        self.assertIsNone(result["forecast_due_at"])
