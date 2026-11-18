# Stdlib unittest runs inside the installed HA container without pytest.
# ruff: noqa: PLC0415
# HA imports stay inside tests so non-HA hosts can run the pure suite.
# Copyright (c) 2026 Alex Ultra
"""Run in the installed Home Assistant container; no live service calls."""

import importlib.util
import unittest
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

HA = importlib.util.find_spec("homeassistant") is not None


@unittest.skipUnless(
    HA, "HA adapter checks run inside installed Home Assistant container"
)
class AdapterTests(unittest.IsolatedAsyncioTestCase):
    """Exercise plant-care regression scenarios."""

    def setUp(self) -> None:
        from homeassistant.core import CoreState

        from custom_components.plant_care_alerts import (
            CareManager,
        )

        self.hass = SimpleNamespace(
            data={},
            is_running=True,
            state=CoreState.running,
            states=SimpleNamespace(get=lambda _entity: None),
            bus=SimpleNamespace(async_fire=Mock()),
            config_entries=SimpleNamespace(async_get_entry=Mock(return_value=object())),
        )
        with patch("custom_components.plant_care_alerts.Store"):
            self.manager = CareManager(
                self.hass,
                {
                    "confirmation_hours": 6,
                    "overdue_hours": 24,
                    "stale_minutes": 20,
                    "freshness_sources": {"sensor.smooth": "sensor.raw"},
                },
            )
        self.manager.add_entities = Mock()
        self.plant = SimpleNamespace(
            entry_id="a",
            plant_name="Тест",
            moisture_sensor="sensor.smooth",
            dry_threshold=25,
            next_watering="2026-10-26",
            last_watered="2026-10-26",
            image_path=None,
            enable_image=False,
        )
        self.manager._buttons = Mock(return_value={})
        self.now = datetime.now(UTC).timestamp()

    def test_unavailable_raw_source_invalidates_buffered_median(self) -> None:
        states = {
            "sensor.smooth": SimpleNamespace(
                state="20", attributes={}, last_reported=datetime.now(UTC)
            ),
            "sensor.raw": SimpleNamespace(
                state="unavailable",
                attributes={"sampled_at": datetime.now(UTC).isoformat()},
                last_reported=datetime.now(UTC),
            ),
        }
        self.hass.states.get = states.get
        reading = self.manager._reading(self.plant, self.now)
        self.assertIsNone(reading["moisture"])
        self.assertEqual(reading["raw_source_identity"], "sensor.raw")

    def test_starting_phase_is_not_a_running_measurement(self) -> None:
        from homeassistant.core import CoreState

        unavailable = SimpleNamespace(
            state="unavailable", attributes={}, last_reported=datetime.now(UTC)
        )
        states = {"sensor.smooth": unavailable, "sensor.raw": unavailable}
        self.hass.states.get = states.get
        # Real HA reports is_running=True during both phases.
        self.hass.state = CoreState.starting
        self.assertTrue(self.hass.is_running)
        reading = self.manager._reading(self.plant, self.now)
        self.assertTrue(reading["starting"])
        self.assertIsNone(reading["moisture"])
        self.hass.state = CoreState.running
        reading = self.manager._reading(self.plant, self.now)
        self.assertFalse(reading["starting"])
        self.assertIsNone(reading["moisture"])

    def test_absent_index_with_fresh_raw_report_is_missing_measurement(self) -> None:
        sampled = datetime.now(UTC)
        states = {
            "sensor.smooth": SimpleNamespace(
                state="unknown", attributes={}, last_reported=sampled
            ),
            "sensor.raw": SimpleNamespace(
                state="180",
                attributes={"sampled_at": sampled.isoformat()},
                last_reported=sampled,
            ),
        }
        self.hass.states.get = states.get
        reading = self.manager._reading(self.plant, sampled.timestamp())
        self.assertFalse(reading["starting"])
        self.assertTrue(reading["measurement_missing"])
        self.assertEqual(reading["raw_source_value"], "180")
        self.assertEqual(reading["raw_source_identity"], "sensor.raw")
        self.assertEqual(reading["sample_at"], sampled.timestamp())

    def test_unmapped_measurement_has_no_separate_raw_identity(self) -> None:
        sampled = datetime.now(UTC)
        self.manager.config["freshness_sources"] = {}
        states = {
            "sensor.smooth": SimpleNamespace(
                state="20", attributes={}, last_reported=sampled
            ),
        }
        self.hass.states.get = states.get
        reading = self.manager._reading(self.plant, sampled.timestamp())
        self.assertIsNone(reading["raw_source_identity"])
        self.assertIsNone(reading["raw_source_value"])
        self.assertEqual(reading["sample_at"], sampled.timestamp())

    async def test_changed_raw_mapping_loads_history_before_reliability(self) -> None:
        sampled = datetime.now(UTC)
        recent = sampled - timedelta(minutes=10)
        old_clock = (sampled - timedelta(hours=13)).timestamp()

        def state(value: str, at: datetime) -> SimpleNamespace:
            return SimpleNamespace(
                state=value,
                attributes={"sampled_at": at.isoformat()},
                last_updated=at,
                last_reported=at,
            )

        self.hass.data["adaptive_plant"] = {"a": self.plant}
        self.manager.config["freshness_sources"] = {"sensor.smooth": "sensor.new_raw"}
        states = {
            "sensor.smooth": state("50", sampled),
            "sensor.new_raw": state("180", sampled),
        }
        self.hass.states.get = states.get
        # A legacy record has no explicit raw_source_identity. Recorder loading
        # replaces adc_identity before the real reliability evaluation runs.
        self.manager.records["a"] = {
            "status": "ok",
            "source_identity": "sensor.smooth",
            "last_observed_value": 50,
            "last_value_change_at": recent.timestamp(),
            "last_source_value": 180,
            "last_source_change_at": old_clock,
            "adc_identity": "sensor.old_raw",
            "adc_samples": [[old_clock, 180]],
        }
        recorded = {
            "sensor.smooth": [state("50", recent), state("50", sampled)],
            "sensor.new_raw": [state("180", recent), state("180", sampled)],
        }
        recorder = SimpleNamespace(
            async_add_executor_job=AsyncMock(return_value=recorded)
        )
        with (
            patch(
                "custom_components.plant_care_alerts.get_instance",
                return_value=recorder,
            ),
            patch(
                "custom_components.plant_care_alerts.dt_util.utcnow",
                return_value=sampled,
            ),
        ):
            await self.manager.async_refresh()
        recorder.async_add_executor_job.assert_awaited_once()
        self.manager.add_entities.assert_called_once()
        current = self.manager.records["a"]
        self.assertTrue(current["reliability_valid"])
        self.assertNotEqual(current["status"], "sensor_error")
        self.assertEqual(current["raw_source_identity"], "sensor.new_raw")
        self.assertEqual(current["last_source_change_at"], recent.timestamp())

    async def test_options_reload_preserves_record(self) -> None:
        self.hass.data["adaptive_plant"] = {}
        self.manager.records["a"] = {"status": "overdue", "due_since": 1234}
        entity = SimpleNamespace(entity_id="sensor.test", async_remove=AsyncMock())
        self.manager.entities["a"] = entity
        await self.manager.async_refresh()
        self.assertIn("a", self.manager.records)
        entity.async_remove.assert_not_called()

    async def test_actual_removal_prunes_record(self) -> None:
        self.hass.data["adaptive_plant"] = {}
        self.hass.config_entries.async_get_entry.return_value = None
        self.manager.records["a"] = {"status": "overdue", "due_since": 1234}
        entity = SimpleNamespace(entity_id="sensor.test", async_remove=AsyncMock())
        self.manager.entities["a"] = entity
        await self.manager.async_refresh()
        self.assertNotIn("a", self.manager.records)
        entity.async_remove.assert_awaited_once()
        self.assertEqual(
            self.hass.bus.async_fire.call_args.args[0], "plant_care_removed"
        )

    def test_creation_emits_status_after_entity_is_ready(self) -> None:
        self.assertTrue(hasattr(self.manager, "emit_transition"))
        self.manager.records["a"] = {
            "status": "due",
            "due_since": self.now,
            "notified_status": None,
        }
        self.manager.details["a"] = {"plant_name": "Тест"}
        self.manager.entities["a"] = SimpleNamespace(entity_id="sensor.test")
        self.manager.emit_transition("a", None)
        event = self.hass.bus.async_fire.call_args.args[1]
        self.assertEqual(event["status"], "due")
        self.assertTrue(event["notify"])

    def test_recovery_does_not_repeat_same_due_notification(self) -> None:
        self.assertTrue(hasattr(self.manager, "emit_transition"))
        self.manager.records["a"] = {
            "status": "overdue",
            "due_since": self.now - 90000,
            "notified_status": "overdue",
        }
        self.manager.details["a"] = {"plant_name": "Тест"}
        self.manager.entities["a"] = SimpleNamespace(entity_id="sensor.test")
        self.manager.emit_transition("a", "sensor_error")
        self.assertFalse(self.hass.bus.async_fire.call_args.args[1]["notify"])

    def test_emitting_does_not_mark_undelivered_notification(self) -> None:
        self.manager.records["a"] = {
            "status": "due",
            "due_since": self.now,
            "notified_status": None,
        }
        self.manager.details["a"] = {"plant_name": "Тест"}
        self.manager.entities["a"] = SimpleNamespace(entity_id="sensor.test")
        self.manager.emit_transition("a", None)
        self.assertIsNone(self.manager.records["a"]["notified_status"])

    def test_upstream_automatic_date_change_is_not_manual_ack(self) -> None:
        self.manager.records["a"] = {"watered_date": "2026-10-25"}
        result = self.manager._reading(self.plant, self.now)
        self.assertIsNone(result["watered_at"])

    def test_acknowledged_red_covers_orange_without_retry(self) -> None:
        from custom_components.plant_care_alerts import (
            needs_notification,
        )

        self.assertFalse(
            needs_notification({"status": "due", "notified_status": "overdue"})
        )
        self.assertTrue(
            needs_notification({"status": "overdue", "notified_status": "due"})
        )
        self.assertTrue(needs_notification({"status": "due", "notified_status": None}))

    def test_quiet_hour_boundaries_use_real_ha_condition(self) -> None:
        from homeassistant.helpers import (
            condition,
        )

        for hour, minute, expected in [
            (7, 59, False),
            (8, 0, True),
            (21, 59, True),
            (22, 0, False),
            (1, 0, False),
        ]:
            with patch(
                "homeassistant.helpers.condition.dt_util.now",
                return_value=datetime(2026, 10, 27, hour, minute, tzinfo=UTC),
            ):
                self.assertEqual(
                    condition.time(self.hass, after=time(8), before=time(22)), expected
                )

    def test_cleared_fault_delivery_allows_a_new_recovery_push(self) -> None:
        from custom_components.plant_care_alerts import (
            acknowledge_delivery,
            needs_notification,
        )

        record = {
            "status": "sensor_error",
            "notified_status": "due",
            "delivery_attempts": 4,
        }
        acknowledge_delivery(record, "sensor_error")
        self.assertIsNone(record["notified_status"])
        self.assertEqual(record["delivery_attempts"], 0)
        record["status"] = "due"
        self.assertTrue(needs_notification(record))
        acknowledge_delivery(record, "due")
        self.assertFalse(needs_notification(record))

    def test_recovery_during_clear_ack_requires_replacement_warning(self) -> None:
        from custom_components.plant_care_alerts import (
            acknowledge_delivery,
            needs_notification,
        )
        from custom_components.plant_care_alerts.state_machine import (
            evaluate,
        )

        record = {"status": "due", "due_since": 1000, "notified_status": "due"}
        record = evaluate(record, {"has_sensor": True, "moisture": None}, 2000)
        self.assertEqual(record["status"], "sensor_error")
        # The actual phone-clear call yields; the sensor recovers before its ACK.
        record = evaluate(
            record, {"has_sensor": True, "moisture": 10, "sample_at": 2001}, 2001
        )
        self.assertEqual(record["status"], "sensor_error")
        for stamp in (2301, 2601):
            record = evaluate(
                record, {"has_sensor": True, "moisture": 10, "sample_at": stamp}, stamp
            )
        # Reliability recovery does not immediately reuse the old dry claim.
        self.assertEqual(record["status"], "checking")
        self.assertEqual(record["due_since"], 1000)
        for stamp in (2901, 3201, 3501):
            record = evaluate(
                record, {"has_sensor": True, "moisture": 10, "sample_at": stamp}, stamp
            )
        self.assertEqual(record["status"], "due")
        acknowledge_delivery(record, "sensor_error")
        self.assertTrue(needs_notification(record))
        self.assertEqual(record["delivery_attempts"], 0)

    async def test_clear_ack_service_accepts_recovered_status(self) -> None:
        from homeassistant.core import Context

        from custom_components.plant_care_alerts import (
            DOMAIN,
            async_setup,
        )

        self.hass.services = SimpleNamespace(async_register=Mock())
        self.hass.bus.async_listen = Mock(return_value=lambda: None)
        self.hass.bus.async_listen_once = Mock()
        self.manager.load = AsyncMock()
        self.manager.records["a"] = {
            "status": "overdue",
            "notified_status": "overdue",
            "delivery_attempts": 3,
        }
        self.manager.entities["a"] = SimpleNamespace(entity_id="sensor.test")
        with (
            patch(
                "custom_components.plant_care_alerts.CareManager",
                return_value=self.manager,
            ),
            patch(
                "custom_components.plant_care_alerts.discovery.async_load_platform",
                new=AsyncMock(),
            ),
            patch("custom_components.plant_care_alerts.async_track_time_interval"),
            patch("custom_components.plant_care_alerts.async_register_admin_service"),
        ):
            await async_setup(self.hass, {DOMAIN: self.manager.config})
        registration = next(
            c
            for c in self.hass.services.async_register.call_args_list
            if c.args[1] == "acknowledge"
        )
        await registration.args[2](
            SimpleNamespace(
                data={"entity_id": "sensor.test", "status": "sensor_error"},
                context=Context(),
            )
        )
        self.assertIsNone(self.manager.records["a"]["notified_status"])
        self.manager.store.async_delay_save.assert_called()

    async def test_initial_transition_waits_for_home_assistant_start(self) -> None:
        from custom_components.plant_care_alerts.sensor import (
            PlantCareSensor,
        )

        self.manager.details["a"] = {"plant_name": "Тест"}
        self.manager.emit_transition = Mock()
        entity = PlantCareSensor(self.manager, "a")
        entity.hass = self.hass
        entity.async_write_ha_state = Mock()
        with (
            patch(
                "homeassistant.components.sensor.SensorEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.plant_care_alerts.sensor.async_at_started"
            ) as deferred,
        ):
            await entity.async_added_to_hass()
            self.manager.emit_transition.assert_not_called()
            callback = deferred.call_args.args[1]
            await callback(self.hass)
        entity.async_write_ha_state.assert_called_once()
        self.manager.emit_transition.assert_called_once_with("a", None)


if __name__ == "__main__":
    unittest.main()
