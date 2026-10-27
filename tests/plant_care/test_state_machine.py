# Stdlib unittest runs inside the installed HA container without pytest.
# ruff: noqa: PT009
# Copyright (c) 2026 Alex Ultra
"""Behavioral contract for plant alerts; no Home Assistant installation required."""

import copy
import importlib.util
import json
import unittest
from pathlib import Path
from typing import Any

FILE = (
    Path(__file__).resolve().parents[2]
    / "custom_components/plant_care_alerts/state_machine.py"
)


class PlantCareTests(unittest.TestCase):
    """Exercise plant-care regression scenarios."""

    def setUp(self) -> None:
        self.assertTrue(FILE.exists(), "plant alert engine has not been implemented")
        spec = importlib.util.spec_from_file_location("plant_care_engine", FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.evaluate = module.evaluate
        self.mean = module.robust_mean
        self.now = 1_800_000_000.0
        self.cfg = {
            "confirm_seconds": 21600,
            "overdue_seconds": 86400,
            "window_seconds": 1800,
            "stale_seconds": 1200,
            "min_samples": 4,
            "recovery_margin": 10,
        }

    def step(
        self, old: Any = None, value: Any = 20, offset: Any = 0, **extra: Any
    ) -> Any:
        now = self.now + offset
        reading = {
            "has_sensor": True,
            "moisture": value,
            "sample_at": now,
            "dry_threshold": 25,
        }
        reading.update(extra)
        return self.evaluate(old or {}, reading, now, self.cfg)

    def sequence(
        self, duration: Any, value: Any = 20, old: Any = None, start: Any = 0
    ) -> Any:
        for sec in range(start, start + duration + 1, 300):
            old = self.step(old, value, sec)
        return old

    def test_single_dry_sample_is_not_a_warning(self) -> None:
        self.assertNotIn(self.step()["status"], ("due", "overdue"))

    def test_one_high_outlier_does_not_wet_the_plant(self) -> None:
        self.assertEqual(self.mean([19, 20, 20, 20, 21, 100]), 20.25)

    def test_duplicate_sample_not_counted_twice(self) -> None:
        state = self.step()
        state = self.step(state, offset=60, sample_at=self.now)
        self.assertEqual(len(state["samples"]), 1)

    def test_six_hours_required_after_reliable_dry_baseline(self) -> None:
        state = self.sequence(22500 - 300)
        self.assertNotIn(state["status"], ("due", "overdue"))
        state = self.step(state, offset=22500)
        self.assertEqual(state["status"], "due")
        self.assertEqual(state["due_since"], self.now + 22500)

    def test_red_exactly_twenty_four_hours_after_due(self) -> None:
        state = self.sequence(22500)
        state = self.sequence(86400 - 300, old=state, start=22800)
        self.assertEqual(state["status"], "overdue")

    def test_hysteresis_holds_warning_in_middle_band(self) -> None:
        state = self.sequence(22500)
        state = self.sequence(2100, value=30, old=state, start=22800)
        self.assertEqual(state["status"], "due")
        state = self.sequence(2100, value=60, old=state, start=25200)
        self.assertEqual(state["status"], "ok")
        self.assertIsNone(state["due_since"])

    def test_stale_or_invalid_reading_is_not_watering_demand(self) -> None:
        due = self.sequence(22500)
        for value in [None, "unavailable", float("nan"), float("inf"), -1, 101]:
            state = self.step(due, value=value, offset=22800)
            self.assertEqual(state["status"], "sensor_error")
        stale = self.step(due, offset=24000, sample_at=self.now + 22500)
        self.assertEqual(stale["status"], "sensor_error")

    def test_gap_resets_unconfirmed_dry_duration(self) -> None:
        state = self.sequence(18000)
        state = self.step(state, offset=21600)
        self.assertNotIn(state["status"], ("due", "overdue"))
        self.assertIsNone(state["dry_since"])

    def test_json_restart_preserves_due_timer(self) -> None:
        state = self.sequence(22500)
        restored = json.loads(json.dumps(state))
        state = self.step(restored, offset=22800)
        self.assertEqual(state["due_since"], restored["due_since"])

    def test_watered_ack_clears_immediately(self) -> None:
        state = self.sequence(22500)
        state = self.step(state, offset=22800, watered_at=self.now + 22800)
        self.assertEqual(state["status"], "ok")
        self.assertIsNone(state["due_since"])

    def test_snooze_prevents_due_and_voice(self) -> None:
        state = self.sequence(22500)
        state = self.step(state, offset=22800, snoozed_at=self.now + 22800)
        self.assertEqual(state["status"], "snoozed")
        self.assertEqual(state["snoozed_until"], self.now + 22800 + 86400)

    def test_schedule_only_plant(self) -> None:
        state = self.step(has_sensor=False, next_watering_at=self.now - 3600)
        self.assertEqual(state["status"], "due")
        state = self.step(has_sensor=False, next_watering_at=self.now - 86400)
        self.assertEqual(state["status"], "overdue")

    def test_future_sample_rejected(self) -> None:
        self.assertEqual(self.step(sample_at=self.now + 3600)["status"], "sensor_error")

    def test_no_mutation_of_previous_state(self) -> None:
        previous = self.step()
        frozen = copy.deepcopy(previous)
        self.step(previous, offset=300)
        self.assertEqual(previous, frozen)


class ReviewRegressionTests(unittest.TestCase):
    """Exercise plant-care regression scenarios."""

    setUp = PlantCareTests.setUp
    step = PlantCareTests.step
    sequence = PlantCareTests.sequence

    def test_replacement_sensor_resets_old_dry_history(self) -> None:
        state = self.sequence(22500)
        state["source_identity"] = "sensor.old"
        state = self.step(state, value=90, offset=22800, source_identity="sensor.new")
        self.assertEqual(state["status"], "ok")
        self.assertIsNone(state["due_since"])
        self.assertEqual(state["moisture"], 90)

    def test_snooze_gives_new_orange_period_not_immediate_red(self) -> None:
        state = self.sequence(22500)
        state = self.step(state, offset=22800, snoozed_at=self.now + 22800)
        state = self.sequence(86400, old=state, start=23100)
        self.assertEqual(state["status"], "due")
        self.assertLess(state["overdue_hours"], 1)

    def test_one_wet_reading_after_outage_does_not_clear_due(self) -> None:
        state = self.sequence(22500)
        state = self.step(state, value=90, offset=26100)
        self.assertIsNotNone(state["due_since"])

    def test_scheduled_snooze_expiry_is_orange(self) -> None:
        state = self.step(has_sensor=False, next_watering_at=self.now - 108000)
        state = self.step(
            state,
            offset=300,
            has_sensor=False,
            next_watering_at=self.now - 108000,
            snoozed_at=self.now + 300,
        )
        state = self.step(
            state, offset=87000, has_sensor=False, next_watering_at=self.now - 108000
        )
        self.assertEqual(state["status"], "due")
        self.assertLess(state["overdue_hours"], 1)

    def test_checking_snooze_does_not_age_due_in_pause(self) -> None:
        state = self.sequence(18000)
        state = self.step(state, offset=18300, snoozed_at=self.now + 18300)
        state = self.sequence(86400, old=state, start=18600)
        self.assertEqual(state["status"], "due")
        self.assertLess(state["overdue_hours"], 1)

    def test_watering_during_fault_starts_new_notification_cycle(self) -> None:
        state = self.sequence(22500)
        state["notified_status"] = "due"
        state = self.step(state, value=None, offset=22800, watered_at=self.now + 22800)
        self.assertIsNone(state["notified_status"])

    def test_short_invalid_blip_pauses_instead_of_erasing_confirmation(self) -> None:
        state = self.sequence(21000)
        dry = state["dry_since"]
        state = self.step(state, value=None, offset=21300)
        self.assertEqual(state["dry_since"], dry)
        state = self.step(state, offset=21360)
        self.assertEqual(state["dry_since"], dry + 60)

    def test_middle_band_cannot_complete_initial_dry_confirmation(self) -> None:
        state = self.sequence(1800)
        state = self.sequence(24000, value=30, old=state, start=2100)
        self.assertEqual(state["status"], "ok")
        self.assertIsNone(state["dry_since"])

    def test_derived_correction_replaces_same_stamp_without_extra_sample(self) -> None:
        state = self.step(value=20)
        state = self.step(state, value=90, offset=1, sample_at=self.now)
        self.assertEqual(len(state["samples"]), 1)
        self.assertEqual(state["samples"][0][1], 90)


if __name__ == "__main__":
    unittest.main()
