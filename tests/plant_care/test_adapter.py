# Stdlib unittest runs inside the installed HA container without pytest.
# ruff: noqa: PLC0415
# HA imports stay inside tests so non-HA hosts can run the pure suite.
# Copyright (c) 2026 Alex Ultra
"""Run in the installed Home Assistant container; no live service calls."""

import importlib.util
import unittest
from copy import deepcopy
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
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
        with patch("custom_components.plant_care_alerts.new_store"):
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

    def test_configuration_rejects_incomplete_or_reversed_calibration(self) -> None:
        import voluptuous as vol

        from custom_components.plant_care_alerts import CONFIG_SCHEMA, DOMAIN

        valid = {
            "freshness_sources": {"sensor.smooth": "sensor.raw"},
            "calibration_sources": {
                "sensor.smooth": {"dry": "input_number.dry", "wet": "input_number.wet"}
            },
            "automatic_calibration": {
                "sensor.smooth": {
                    "original_dry": 67,
                    "original_wet": 221,
                    "source_revision": "v1",
                }
            },
        }
        self.assertTrue(
            CONFIG_SCHEMA({DOMAIN: valid})[DOMAIN]["automatic_calibration"][
                "sensor.smooth"
            ]["enabled"]
        )
        for update, removed in (
            ({"original_wet": 67}, None),
            ({"reference_entity": "sensor.reference"}, None),
            ({}, "freshness_sources"),
            ({}, "calibration_sources"),
        ):
            with self.subTest(update=update, removed=removed):
                config = deepcopy(valid)
                config["automatic_calibration"]["sensor.smooth"].update(update)
                if removed:
                    config.pop(removed)
                with self.assertRaises(vol.Invalid):
                    CONFIG_SCHEMA({DOMAIN: config})

    def test_approximate_report_keeps_the_reported_day_and_uncertainty(self) -> None:
        from custom_components.plant_care_alerts import CONFIG_SCHEMA, DOMAIN

        report = {
            "date": "2026-11-17",
            "precision": "approximate_day",
            "note": "Вчера вроде",
            "timezone": "Europe/Belgrade",
        }
        config = CONFIG_SCHEMA({DOMAIN: {"user_reports": {"sensor.smooth": report}}})
        self.manager.config["user_reports"] = config[DOMAIN]["user_reports"]
        analysis = {
            "last_watering_at": "2026-11-03",
            "last_watering_source": "user_report",
        }
        now = datetime(2026, 11, 18, 20, tzinfo=UTC).timestamp()
        self.manager._restore_reported_watering(
            analysis, ("2026-11-03", "user_report"), self.plant, now
        )
        self.assertEqual(analysis["last_watering_at"], "2026-11-17")
        self.assertEqual(analysis["last_watering_precision"], "approximate_day")
        self.assertEqual(analysis["last_watering_note"], "Вчера вроде")
        self.assertIsNone(analysis["last_watering_window_start"])
        self.assertIsNone(analysis["watering_history"][0]["detected_at"])
        self.assertEqual(analysis["watering_history"][1]["detected_at"], now)
        self.manager._restore_reported_watering(
            analysis, ("2026-11-17", "user_report"), self.plant, now + 60
        )
        self.assertEqual(len(analysis["watering_history"]), 2)
        self.assertIsNone(analysis["watering_history"][0]["detected_at"])
        self.assertEqual(analysis["watering_history"][1]["detected_at"], now)

    def test_collector_does_not_hide_conflicting_physical_timestamp(self) -> None:
        current = {"adc_identity": "sensor.raw", "adc_samples": [[self.now, 300]]}
        raw = SimpleNamespace(
            state="340",
            attributes={"sampled_at": datetime.fromtimestamp(self.now, UTC)},
        )
        self.manager._collect_adc_samples(
            current, "sensor.raw", raw, [67, 1298], self.now
        )
        self.assertEqual(current["watering_conflicts"], [self.now])

    async def test_user_refresh_does_not_inherit_background_write_authority(
        self,
    ) -> None:
        phases = []

        async def refresh_once() -> None:
            phases.append(self.manager._calibration_write_active)
            if len(phases) == 1:
                await self.manager.async_refresh()

        self.manager._refresh_once = AsyncMock(side_effect=refresh_once)
        await self.manager.async_background_refresh()
        await self.manager.async_refresh()
        self.assertEqual(phases, [True, False, False])

    async def test_failed_existing_store_preserves_frozen_fault_basis(self) -> None:
        from custom_components.plant_care_alerts.state_machine import evaluate

        sampled = datetime.fromtimestamp(self.now, UTC)
        states = {
            "sensor.smooth": SimpleNamespace(
                state="50", attributes={}, last_reported=sampled
            ),
            "sensor.raw": SimpleNamespace(
                state="1500",
                attributes={"sampled_at": sampled.isoformat(), "physical_pin": 22},
                last_reported=sampled,
            ),
            "input_number.dry": SimpleNamespace(state="67", attributes={}),
            "input_number.wet": SimpleNamespace(state="2933", attributes={}),
        }
        self.hass.states.get = states.get
        self.manager.config["calibration_sources"] = {
            "sensor.smooth": {"dry": "input_number.dry", "wet": "input_number.wet"}
        }
        self.manager.config["automatic_calibration"] = {
            "sensor.smooth": {
                "enabled": True,
                "original_dry": 67,
                "original_wet": 221,
                "source_revision": "probe-v1",
            }
        }
        states["input_number.wet"].state = "221"
        self.hass.async_add_executor_job = AsyncMock(
            side_effect=lambda function, *args: function(*args)
        )
        self.manager.calibration._identity_provider = lambda _source: "registered:raw"
        self.manager.calibration.store = SimpleNamespace(
            async_load=AsyncMock(return_value=None),
            async_save=AsyncMock(),
        )
        persisted = None

        async def save(data: dict) -> None:
            nonlocal persisted
            persisted = deepcopy(data)

        self.manager.calibration.store.async_save.side_effect = save
        self.manager.calibration._read_persisted = AsyncMock(
            side_effect=lambda: deepcopy(persisted)
        )
        await self.manager.calibration.async_load()
        await self.manager.calibration.async_prepare(
            "a", "sensor.smooth", self.now - 600
        )
        self.assertEqual(
            self.manager.calibration.reading_fields("a")["calibration_current"],
            [67, 221],
        )
        self.manager.calibration.store.async_save.side_effect = OSError(
            "disk unavailable"
        )
        with self.assertLogs(
            "custom_components.plant_care_alerts.calibration_runtime", level="ERROR"
        ):
            self.assertFalse(await self.manager.calibration.async_save())
        states["input_number.wet"].state = "2933"
        old = {
            "source_identity": "sensor.smooth",
            "raw_source_identity": "sensor.raw",
            "last_observed_value": 100,
            "last_value_change_at": self.now - 5 * 86400,
            "last_source_value": 1490,
            "last_source_change_at": self.now - 300,
            "reliability_last_sample_at": self.now - 300,
            "reliability_valid": False,
            "reliability_incident_started_at": self.now - 4 * 86400,
            "reliability_incident_reason": "saturated",
            "reliability_incident_value": 100,
            "reliability_incident_value_changed_at": self.now - 5 * 86400,
            "reliability_calibration_basis": [67, 221],
        }
        self.manager.records["a"] = old
        current = old
        for offset, raw in ((0, 1500), (300, 1490), (600, 1480)):
            at = self.now + offset
            states["sensor.raw"].state = str(raw)
            states["sensor.raw"].attributes["sampled_at"] = at
            states["sensor.smooth"].state = str((raw - 67) * 100 / (2933 - 67))
            self.manager.records["a"] = current
            reading = self.manager._reading(self.plant, at)
            self.assertEqual(reading["calibration_current"], [67, 2933])
            current = evaluate(current, reading, at, self.manager.settings)
        self.assertEqual(current["status"], "sensor_error")
        self.assertEqual(current["last_value_change_at"], old["last_value_change_at"])
        self.assertEqual(
            current["reliability_incident_started_at"],
            old["reliability_incident_started_at"],
        )
        self.assertEqual(current["reliability_calibration_basis"], [67, 221])
        self.assertFalse(current["reliability_valid"])

    async def test_failed_calibration_load_retains_care_witness_across_refresh_and_restart(
        self,
    ) -> None:
        from custom_components.plant_care_alerts import CareManager
        from custom_components.plant_care_alerts.calibration_runtime import (
            CalibrationRuntime,
            RuntimeHooks,
        )

        options = {
            **self.manager.config,
            "calibration_sources": {
                "sensor.smooth": {"dry": "input_number.dry", "wet": "input_number.wet"}
            },
            "automatic_calibration": {
                "sensor.smooth": {
                    "enabled": True,
                    "source_revision": "probe-v1",
                    "original_dry": 67,
                    "original_wet": 221,
                }
            },
        }
        identity = "registered-source"
        record = CalibrationRuntime._new_record(
            "sensor.smooth", (67, 221), identity, self.now - 86400
        )
        producer = CalibrationRuntime(
            self.hass,
            SimpleNamespace(),
            options,
            hooks=RuntimeHooks(
                read_persisted=AsyncMock(return_value=None),
                confirm_witnesses=AsyncMock(),
            ),
        )
        witness = producer._witness_snapshot({"sensor.smooth": record})
        prior = {"a": {"status": "sensor_error", "calibration_witnesses": witness}}
        self.hass.data["adaptive_plant"] = {"a": self.plant}
        for _ in range(2):
            with patch("custom_components.plant_care_alerts.new_store"):
                manager = CareManager(self.hass, options)
            manager.add_entities = Mock()
            manager._buttons = Mock(return_value={})
            manager._load_history = AsyncMock()
            manager.store.async_load = AsyncMock(return_value=deepcopy(prior))
            manager.store.async_save = AsyncMock()
            manager.calibration = CalibrationRuntime(
                self.hass,
                SimpleNamespace(async_load=AsyncMock(return_value=None)),
                options,
                hooks=RuntimeHooks(
                    read_persisted=AsyncMock(return_value=None),
                    confirm_witnesses=manager._confirm_calibration_witnesses,
                    identity_provider=lambda _source: identity,
                ),
            )
            with self.assertLogs(
                "custom_components.plant_care_alerts.calibration_runtime", level="ERROR"
            ):
                await manager.load()
            await manager.async_refresh()
            self.assertEqual(manager.records["a"]["calibration_witnesses"], witness)
            manager.store.async_save.assert_not_awaited()
            self.assertIsNone(manager.calibration.witnesses())
            prior = deepcopy(manager.records)

    async def test_silent_care_save_does_not_acknowledge_or_replace_witness(
        self,
    ) -> None:
        prior = {"source": {"revision": 1}}
        current = {"source": {"revision": 2}}
        self.manager.records["a"] = {
            "status": "ok",
            "calibration_witnesses": deepcopy(prior),
        }
        self.manager.calibration.witnesses = Mock(return_value=current)
        self.manager.store.async_save = AsyncMock()
        self.manager._read_persisted = AsyncMock(
            return_value=deepcopy(self.manager.records)
        )
        with self.assertLogs("custom_components.plant_care_alerts", level="ERROR"):
            self.assertFalse(await self.manager._persist_calibration_witnesses("a"))
        self.assertEqual(self.manager.records["a"]["calibration_witnesses"], prior)
        self.assertEqual(
            self.manager.calibration._disabled_reason, "witness_unavailable"
        )

    async def test_identical_durable_witness_is_confirmed_each_time(self) -> None:
        witness = {"source": {"revision": 0, "decision_fence_at": None}}
        self.manager.records["a"] = {"calibration_witnesses": deepcopy(witness)}
        self.manager.store.async_save = AsyncMock()
        self.manager._read_persisted = AsyncMock(
            return_value=deepcopy(self.manager.records)
        )
        for _ in range(2):
            await self.manager._confirm_calibration_witnesses("a", witness)
        self.assertEqual(self.manager.store.async_save.await_count, 2)
        self.assertEqual(self.manager._read_persisted.await_count, 2)
        self.assertEqual(self.manager.records["a"]["calibration_witnesses"], witness)

    async def test_witness_confirmation_does_not_restore_stale_care_fields(
        self,
    ) -> None:
        witness = {"source": {"revision": 0, "decision_fence_at": None}}
        self.manager.records["a"] = {"notified_status": "due"}
        persisted = None

        async def save(snapshot: dict) -> None:
            nonlocal persisted
            persisted = deepcopy(snapshot)
            self.manager.records["a"]["notified_status"] = "overdue"

        self.manager.store.async_save = AsyncMock(side_effect=save)
        self.manager._read_persisted = AsyncMock(side_effect=lambda _key: persisted)
        await self.manager._confirm_calibration_witnesses("a", witness)
        self.assertEqual(self.manager.records["a"]["notified_status"], "overdue")
        self.assertEqual(self.manager.records["a"]["calibration_witnesses"], witness)

    async def test_failed_refresh_drops_queued_background_authority(self) -> None:
        observed = []

        async def broken() -> None:
            await self.manager.async_background_refresh()
            message = "refresh interrupted"
            raise OSError(message)

        self.manager._refresh_once = broken
        with self.assertLogs("custom_components.plant_care_alerts", level="ERROR"):
            await self.manager.async_background_refresh()
        self.manager._refresh_once = AsyncMock(
            side_effect=lambda: observed.append(self.manager._calibration_write_active)
        )
        await self.manager.async_refresh()
        self.assertEqual(observed, [False])

    def test_calibration_pause_keeps_existing_warning_delivery(self) -> None:
        self.manager.records["a"] = {
            "status": "calibrating",
            "due_since": self.now - 90000,
            "notified_status": "overdue",
            "reliability_reason": "calibrating",
        }
        self.manager.details["a"] = {"plant_name": "Тест"}
        self.manager.entities["a"] = SimpleNamespace(entity_id="sensor.test")
        self.manager.emit_transition("a", "overdue")
        event = self.hass.bus.async_fire.call_args.args[1]
        self.assertFalse(event["clear"])
        self.assertFalse(event["notify"])
        self.assertEqual(self.manager.records["a"]["notified_status"], "overdue")
        self.manager.records["a"].update(
            status="checking",
            reliability_reason=None,
            calibration_confirmation_pending=True,
        )
        self.manager.emit_transition("a", "calibrating")
        self.assertFalse(self.hass.bus.async_fire.call_args.args[1]["clear"])
        self.manager.records["a"].update(
            status="overdue", calibration_confirmation_pending=False
        )
        self.manager.emit_transition("a", "checking")
        self.assertFalse(self.hass.bus.async_fire.call_args.args[1]["notify"])

    def test_blueprint_clear_consumer_obeys_explicit_delivery_permission(self) -> None:
        import yaml
        from jinja2 import Environment, StrictUndefined

        class BlueprintLoader(yaml.SafeLoader):
            """Keep blueprint input placeholders while reading the real branches."""

        BlueprintLoader.add_constructor(
            "!input", lambda loader, node: loader.construct_scalar(node)
        )
        path = (
            Path(__file__).resolve().parents[2]
            / "blueprints/automation/plant_care/alerts.yaml"
        )
        loader = BlueprintLoader(path.read_text())
        try:
            document = loader.get_single_data()
        finally:
            loader.dispose()
        choices = document["actions"][0]["choose"][1]["sequence"][0]["choose"]
        environment = Environment(undefined=StrictUndefined, autoescape=True)
        healthy = environment.from_string(choices[1]["conditions"])
        fault = environment.from_string(choices[0]["sequence"][3]["if"])
        for status in ("calibrating", "checking", "ok", "sensor_error"):
            with self.subTest(status=status, clear=False):
                variables = {
                    "change_status": status,
                    "trigger": {
                        "event": {"data": {"clear": False, "previous": "overdue"}}
                    },
                }
                self.assertEqual(healthy.render(**variables).strip(), "False")
                self.assertEqual(fault.render(**variables).strip(), "False")
        for status in ("ok", "checking"):
            with self.subTest(status=status, clear=True):
                variables = {
                    "change_status": status,
                    "trigger": {
                        "event": {"data": {"clear": True, "previous": "checking"}}
                    },
                }
                self.assertEqual(healthy.render(**variables).strip(), "True")

    def test_delayed_clear_is_emitted_once_confirmation_finishes(self) -> None:
        self.manager.records["a"] = {
            "status": "ok",
            "due_since": None,
            "notified_status": None,
            "calibration_confirmation_pending": False,
            "clear_pending": True,
        }
        self.manager.details["a"] = {"plant_name": "Тест"}
        self.manager.entities["a"] = SimpleNamespace(entity_id="sensor.test")
        self.manager.emit_transition("a", "checking")
        event = self.hass.bus.async_fire.call_args.args[1]
        self.assertTrue(event["clear"])
        self.assertFalse(event["notify"])

    def test_recorder_excludes_large_calibration_journal(self) -> None:
        from custom_components.plant_care_alerts.sensor import PlantCareSensor

        self.assertIn("calibration_history", PlantCareSensor._unrecorded_attributes)

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

        self.hass.services = SimpleNamespace(
            async_register=Mock(),
            has_service=Mock(return_value=False),
            async_services=Mock(return_value={DOMAIN: {"apply_calibration": object()}}),
            async_remove=Mock(),
        )
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
