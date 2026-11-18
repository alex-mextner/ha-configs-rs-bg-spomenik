"""Calibration transitions must not manufacture physical recovery or watering."""

import copy
import importlib.util
import sys
import types
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from calibration_fixtures import (
    physical_attributes,
    qualified_cycle_windows,
    reference_attributes,
)

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "_plant_calibration_runtime_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "custom_components/plant_care_alerts")]
sys.modules[PACKAGE] = package


def load(name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        PACKAGE + "." + name,
        ROOT / "custom_components/plant_care_alerts" / (name + ".py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


state_machine = load("state_machine")
runtime_module = load("calibration_runtime")
MISSING = object()


class Interrupted(BaseException):
    """Simulate process loss without the normal exception recovery path."""


def raise_interrupted() -> None:
    raise Interrupted


def helper_event(
    entity_id: str, before: Any, after: Any, context_id: str
) -> SimpleNamespace:
    return SimpleNamespace(
        data={"entity_id": entity_id, "old_state": before, "new_state": after},
        context=SimpleNamespace(id=context_id),
    )


START = 1794538800.0
NOW = START + 15 * 3600


def frozen_record() -> dict:
    return {
        "source_identity": "sensor.smooth",
        "raw_source_identity": "sensor.raw",
        "last_observed_value": 100.0,
        "last_value_change_at": START,
        "last_source_value": 1331.0,
        "last_source_change_at": NOW - 300,
        "reliability_last_sample_at": NOW - 300,
        "reliability_valid": False,
        "reliability_reason": "saturated",
        "reliability_incident_reason": "saturated",
        "reliability_incident_started_at": START + 43201,
        "reliability_incident_value": 100.0,
        "reliability_incident_raw": 350.0,
        "reliability_incident_value_changed_at": START,
        "reliability_invalid_intervals": [[START, None]],
        "reliability_calibration_basis": [67.0, 221.0],
    }


def reading(at: float, value: float, raw: float) -> dict:
    return {
        "source_identity": "sensor.smooth",
        "raw_source_identity": "sensor.raw",
        "raw_source_value": raw,
        "moisture": value,
        "sample_at": at,
        "has_sensor": True,
        "calibration_revision": 1,
        "calibration_effective_at": NOW,
        "calibration_barrier_until": NOW + 1800,
        "calibration_current": [67.0, 2000.0],
        "calibration_previous": [67.0, 221.0],
    }


class CalibrationReliabilityTests(unittest.TestCase):
    """Exercise the existing state machine with a new calibration epoch."""

    def test_scale_change_during_barrier_preserves_incident_and_numeric_clock(
        self,
    ) -> None:
        record = state_machine.evaluate(
            frozen_record(), reading(NOW + 60, 66, 1340), NOW + 60
        )
        self.assertEqual(record["last_value_change_at"], START)
        self.assertEqual(record["reliability_incident_started_at"], START + 43201)
        self.assertEqual(record["status"], "sensor_error")
        self.assertEqual(record["last_source_change_at"], NOW + 60)
        self.assertEqual(record.get("recovery_sample_count"), 0)

    def test_rescaled_old_plateau_cannot_recover_after_smoothing_barrier(self) -> None:
        record = frozen_record()
        for offset, value, raw in [
            (1801, 69, 1330),
            (2101, 68, 1320),
            (2401, 67, 1310),
        ]:
            record = state_machine.evaluate(
                record, reading(NOW + offset, value, raw), NOW + offset
            )
        self.assertEqual(record["status"], "sensor_error")
        self.assertEqual(record["last_value_change_at"], START)
        self.assertEqual(record["reliability_incident_started_at"], START + 43201)
        self.assertIsNone(record["moisture"])

    def test_qualified_revision_still_requires_fresh_physical_recovery(self) -> None:
        record = frozen_record()
        for offset, raw in [(1801, 221.5), (2101, 221.3)]:
            current = reading(NOW + offset, (raw - 67) * 100 / 155, raw)
            current.update(
                calibration_current=[67.0, 222.0], calibration_verified_revision=1
            )
            record = state_machine.evaluate(record, current, NOW + offset)
            self.assertEqual(record["status"], "sensor_error")
        current = reading(NOW + 2401, (221.1 - 67) * 100 / 155, 221.1)
        current.update(
            calibration_current=[67.0, 222.0], calibration_verified_revision=1
        )
        record = state_machine.evaluate(record, current, NOW + 2401)
        self.assertTrue(record["reliability_valid"])
        self.assertIsNone(record["reliability_incident_started_at"])

    def test_expired_barrier_waits_quietly_for_next_physical_sample(self) -> None:
        previous = {
            "source_identity": "sensor.smooth",
            "raw_source_identity": "sensor.raw",
            "last_observed_value": 20.0,
            "last_value_change_at": NOW - 600,
            "last_source_value": 98.0,
            "last_source_change_at": NOW - 600,
            "reliability_last_sample_at": NOW - 300,
            "reliability_valid": True,
        }
        during = reading(NOW + 1700, 90, 98)
        during["calibration_current"] = [67, 222]
        record = state_machine.evaluate(previous, during, NOW + 1700)
        self.assertEqual(record["status"], "calibrating")
        record = state_machine.evaluate(record, during, NOW + 1801)
        self.assertNotEqual(record["status"], "sensor_error")
        self.assertIsNone(record.get("reliability_incident_started_at"))
        record = state_machine.evaluate(
            record,
            {**during, "sample_at": NOW + 2000, "raw_source_value": 99},
            NOW + 2000,
        )
        self.assertNotEqual(record["status"], "sensor_error")

    def test_barrier_does_not_trust_invalid_or_unmapped_physical_source(self) -> None:
        previous = {
            "source_identity": "sensor.smooth",
            "raw_source_identity": "sensor.raw",
            "last_observed_value": 20,
            "last_value_change_at": NOW - 600,
            "last_source_value": 98,
            "last_source_change_at": NOW - 600,
            "reliability_last_sample_at": NOW - 300,
            "reliability_valid": True,
        }
        for invalid in ({"source_invalid": True}, {"raw_source_identity": None}):
            with self.subTest(invalid=invalid):
                current = state_machine.evaluate(
                    previous, {**reading(NOW + 60, 20, 99), **invalid}, NOW + 60
                )
                self.assertEqual(current["status"], "sensor_error")
                self.assertEqual(current["reliability_reason"], "invalid_sample")

    def test_healthy_calibration_barrier_creates_no_sensor_incident(self) -> None:
        previous = {
            "source_identity": "sensor.smooth",
            "raw_source_identity": "sensor.raw",
            "last_observed_value": 20.0,
            "last_value_change_at": NOW - 600,
            "last_source_value": 98.0,
            "last_source_change_at": NOW - 600,
            "reliability_last_sample_at": NOW - 300,
            "reliability_valid": True,
            "due_since": NOW - 21600,
            "dry_since": NOW - 43200,
        }
        record = state_machine.evaluate(previous, reading(NOW + 60, 90, 98), NOW + 60)
        self.assertEqual(record["status"], "calibrating")
        self.assertFalse(record["reliability_valid"])
        self.assertIsNone(record.get("reliability_incident_started_at"))
        self.assertEqual(record["due_since"], NOW - 21600)
        self.assertEqual(record["last_value_change_at"], NOW - 600)


class MemoryStore:
    """Replace only HA's asynchronous disk boundary."""

    def __init__(self) -> None:
        self.data = None
        self.fail_save = False
        self.drop_save = False

    async def async_load(self) -> dict | None:
        return copy.deepcopy(self.data)

    async def async_save(self, data: dict) -> None:
        if self.fail_save:
            message = "Storage write unavailable"
            raise OSError(message)
        if not self.drop_save:
            self.data = copy.deepcopy(data)


def ha_state(value: float, at: float = NOW, **attributes: Any) -> SimpleNamespace:
    return SimpleNamespace(
        state=str(value),
        attributes=attributes,
        last_updated=datetime.fromtimestamp(at, UTC),
    )


class FakeServices:
    """Model service dispatch and helper events; real permissions are tested in HA."""

    def __init__(self, hass) -> None:
        self.hass = hass
        self.writes = []
        self.after_write = None
        self.before_write = None

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict,
        **options: Any,
    ) -> dict:
        context = options.get("context")
        if (domain, service) != ("plant_care_alerts", "apply_calibration") or not (
            options.get("blocking") and options.get("return_response")
        ):
            message = "Unexpected external effect"
            raise AssertionError(message)
        # This boundary may run other native call_service listeners first.
        if self.before_write:
            self.before_write()
        entity_id = data["entity_id"]

        async def set_native(value: float) -> None:
            before = self.hass.values[entity_id]
            after = ha_state(value, self.hass.now, **before.attributes)
            self.hass.values[entity_id] = after
            self.writes.append((entity_id, value))
            self.hass.runtime.note_helper_event(
                SimpleNamespace(
                    data={
                        "entity_id": entity_id,
                        "old_state": before,
                        "new_state": after,
                    },
                    time_fired=after.last_updated,
                    context=context,
                )
            )
            if self.after_write:
                self.after_write()

        entity = SimpleNamespace(
            entity_id=entity_id,
            native_value=float(self.hass.values[entity_id].state),
            async_set_native_value=set_native,
        )
        result = await self.hass.runtime.async_guarded_apply(
            entity, SimpleNamespace(data=data, context=context)
        )
        return {entity_id: result}


class CalibrationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    """Exercise persistence and actual helper effects through the runtime adapter."""

    def new_runtime(
        self, hass: Any, store: MemoryStore, config: dict, *, hooks: Any = None
    ) -> Any:
        hooks = hooks or self.runtime_module.RuntimeHooks()
        return self.runtime_module.CalibrationRuntime(
            hass,
            store,
            config,
            hooks=replace(hooks, read_persisted=store.async_load),
        )

    async def asyncSetUp(self) -> None:
        self.runtime_module = runtime_module
        self.store = MemoryStore()
        self.config = {
            "freshness_sources": {"sensor.smooth": "sensor.raw"},
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
        self.hass = SimpleNamespace(
            values={
                "sensor.raw": ha_state(
                    1331, sampled_at=NOW, samples=[1331] * 11, physical_pin=22
                ),
                "input_number.dry": ha_state(67),
                "input_number.wet": ha_state(221, step=1),
            },
            now=NOW,
            runtime=None,
        )

        async def executor(function, *args: Any) -> Any:
            return function(*args)

        self.hass.async_add_executor_job = executor
        self.hass.states = SimpleNamespace(get=self.hass.values.get)
        self.hass.services = FakeServices(self.hass)
        self.manager = self.new_runtime(self.hass, self.store, self.config)
        self.hass.runtime = self.manager
        await self.manager.async_load()
        await self.manager.async_prepare("plant", "sensor.smooth", NOW, "rest:raw")

    async def test_failed_load_offers_no_witness_replacement_on_two_restarts(
        self,
    ) -> None:
        durable = self.manager.witnesses()
        missing = MemoryStore()
        for _ in range(2):
            restarted = self.new_runtime(self.hass, missing, self.config)
            with self.assertLogs(self.runtime_module.__name__, level="ERROR"):
                await restarted.async_load(durable)
            await restarted.async_prepare("plant", "sensor.smooth", NOW + 1, "rest:raw")
            self.assertIsNone(restarted.witnesses())
            self.assertFalse(
                restarted.attributes("plant")["calibration_history_available"]
            )
            self.assertEqual(self.hass.services.writes, [])

    async def test_save_failure_then_manual_rescale_keeps_original_fault_basis(
        self,
    ) -> None:
        self.store.fail_save = True
        with self.assertLogs(self.runtime_module.__name__, level="ERROR"):
            self.assertFalse(await self.manager.async_save())
        self.hass.values["input_number.wet"] = ha_state(1600)
        record = frozen_record()
        for offset, raw in [(1801, 1330), (2101, 1340), (2401, 1350)]:
            at = NOW + offset
            self.hass.values["sensor.raw"] = ha_state(
                raw, sampled_at=at, samples=[raw] * 11, physical_pin=22
            )
            await self.manager.async_prepare("plant", "sensor.smooth", at, "rest:raw")
            fields = self.manager.reading_fields("plant")
            self.assertEqual(fields, {})
            current = {
                "source_identity": "sensor.smooth",
                "raw_source_identity": "sensor.raw",
                "raw_source_value": raw,
                "moisture": (raw - 67) * 100 / (1600 - 67),
                "sample_at": at,
                "has_sensor": True,
                "calibration_current": [67, 1600],
                **fields,
            }
            record = state_machine.evaluate(record, current, at)
        self.assertFalse(record["reliability_valid"])
        self.assertEqual(record["status"], "sensor_error")
        self.assertEqual(record["last_value_change_at"], START)
        self.assertEqual(record["reliability_incident_started_at"], START + 43201)

    async def test_queued_manual_change_is_saved_before_new_source_epoch(self) -> None:
        original = self.manager.attributes("plant")["calibration_source_fingerprint"]
        self.hass.values["sensor.raw"] = ha_state("unavailable", NOW + 1)
        self.hass.now = NOW + 10
        self.external_wet_change(230)
        await self.manager.async_prepare("plant", "sensor.smooth", NOW + 11, "rest:raw")
        self.hass.values["sensor.raw"] = ha_state(
            1400, sampled_at=NOW + 20, samples=[1400] * 11, physical_pin=23
        )
        await self.manager.async_prepare("plant", "sensor.smooth", NOW + 20, "rest:raw")
        attrs = self.manager.attributes("plant")
        self.assertTrue(attrs["calibration_history_available"])
        record = self.store.data["records"]["sensor.smooth"]
        self.assertEqual(record["history"][0]["source_fingerprint"], original)
        self.assertEqual(record["history"][0]["at"], NOW + 10)
        self.assertEqual(record["history"][0]["kind"], "manual")
        self.assertGreaterEqual(record["barrier_until"], NOW + 1820)
        self.assertEqual(record["current"], [67, 230])
        self.assertEqual(record["source_epoch"], 1)

    async def test_pre_enrollment_helper_facts_do_not_backdate_source_identity(
        self,
    ) -> None:
        self.store = MemoryStore()
        self.manager = self.new_runtime(self.hass, self.store, self.config)
        self.hass.runtime = self.manager
        await self.manager.async_load()
        self.hass.values["sensor.raw"] = ha_state("unavailable", NOW)
        self.hass.now = NOW + 1
        self.external_wet_change(230)
        await self.manager.async_prepare("plant", "sensor.smooth", NOW + 2, "rest:raw")
        self.hass.values["sensor.raw"] = ha_state(
            1400, sampled_at=NOW + 10, samples=[1400] * 11, physical_pin=22
        )
        await self.manager.async_prepare("plant", "sensor.smooth", NOW + 10, "rest:raw")
        attrs = self.manager.attributes("plant")
        self.assertTrue(attrs["calibration_history_available"])
        record = self.store.data["records"]["sensor.smooth"]
        self.assertEqual(record["source_fences"][0]["at"], NOW + 10)
        self.assertEqual(record["baseline"]["created_at"], NOW + 10)
        fact = record["helper_observations"][0]
        self.assertEqual(
            (fact["at"], fact["endpoint"], fact["before"], fact["after"]),
            (NOW + 1, "wet", 221, 230),
        )
        self.assertEqual(fact["context_id"], "external")
        self.assertIsNone(fact["source_fingerprint"])
        self.assertEqual(fact["reason"], "pre_enrollment")
        self.assertEqual(record["history"][0]["kind"], "external_discovered")
        self.assertIsNone(record["history"][0]["at"])
        self.assertEqual(record["history"][0]["detected_at"], NOW + 10)

    async def test_nested_manual_only_events_replay_actual_old_new_chain(self) -> None:
        old = self.hass.values["input_number.wet"]
        middle = ha_state(225, NOW + 1)
        final = ha_state(230, NOW + 2)
        for before, after in [(middle, final), (old, middle)]:
            self.manager.note_helper_event(
                helper_event("input_number.wet", before, after, "manual")
            )
        self.hass.values["input_number.wet"] = final
        await self.manager.async_prepare("plant", "sensor.smooth", NOW + 3, "rest:raw")
        attrs = self.manager.attributes("plant")
        self.assertTrue(attrs["calibration_history_available"])
        self.assertEqual(
            [(row["old_wet"], row["new_wet"]) for row in attrs["calibration_history"]],
            [(221, 225), (225, 230)],
        )
        self.assertEqual(
            self.store.data["records"]["sensor.smooth"]["current"], [67, 230]
        )

    async def test_without_independent_reference_observes_without_changing_endpoints(
        self,
    ) -> None:
        await self.manager.async_observe(
            "plant",
            {
                "reliability_valid": False,
                "reliability_invalid_intervals": [[START, None]],
            },
            NOW,
            allow_write=True,
        )
        attrs = self.manager.attributes("plant")
        self.assertEqual(attrs["calibration_status"], "awaiting_reference")
        self.assertEqual(attrs["calibration_observations"], 1)
        self.assertEqual(self.hass.values["input_number.wet"].state, "221")
        self.assertEqual(attrs["calibration_history"], [])
        self.assertEqual(self.hass.services.writes, [])

    async def test_coalesced_manual_changes_both_remain_in_history(self) -> None:
        old = self.hass.values["input_number.wet"]
        for offset, value in [(1, 222), (2, 223)]:
            new = ha_state(value, NOW + offset, step=1)
            self.manager.note_helper_event(
                helper_event("input_number.wet", old, new, f"manual-{offset}")
            )
            old = new
        self.hass.values["input_number.wet"] = old
        await self.manager.async_prepare("plant", "sensor.smooth", NOW + 3, "rest:raw")
        attrs = self.manager.attributes("plant")
        self.assertEqual(
            [row["new_wet"] for row in attrs["calibration_history"]], [222, 223]
        )
        self.assertEqual(attrs["calibration_baseline_wet"], 221)
        self.assertEqual(attrs["calibration_revision"], 2)
        self.assertEqual(
            self.manager.reading_fields("plant")["calibration_barrier_until"],
            NOW + 1802,
        )

    async def test_restart_preserves_manual_history_without_rebasing(self) -> None:
        self.hass.values["input_number.wet"] = ha_state(230, NOW + 1)
        await self.manager.async_prepare("plant", "sensor.smooth", NOW + 1, "rest:raw")
        restarted = self.new_runtime(self.hass, self.store, self.config)
        await restarted.async_load()
        await restarted.async_prepare("plant", "sensor.smooth", NOW + 2, "rest:raw")
        attrs = restarted.attributes("plant")
        self.assertEqual(attrs["calibration_baseline_wet"], 221)
        self.assertEqual(attrs["calibration_wet_adc"], 230)
        self.assertEqual(len(attrs["calibration_history"]), 1)
        self.assertEqual(attrs["calibration_history"][0]["kind"], "external_discovered")
        self.assertEqual(self.hass.services.writes, [])

    async def test_source_revision_change_cannot_create_a_new_baseline(self) -> None:
        original = self.manager.attributes("plant")["calibration_source_fingerprint"]
        self.config["automatic_calibration"]["sensor.smooth"]["source_revision"] = (
            "probe-v2"
        )
        await self.manager.async_prepare("plant", "sensor.smooth", NOW + 1, "rest:raw")
        attrs = self.manager.attributes("plant")
        self.assertEqual(attrs["calibration_status"], "source_changed")
        self.assertNotEqual(attrs["calibration_source_fingerprint"], original)
        self.assertEqual(attrs["calibration_baseline_wet"], 221)
        self.assertEqual(self.hass.services.writes, [])

    async def qualified_fixture(self, raw: float = 209) -> None:
        """Supply four independent reference windows, not inferred watering."""
        self.store = MemoryStore()
        self.quality = {"reliability_valid": True}
        self.config["automatic_calibration"]["sensor.smooth"].update(
            reference_entity="sensor.reference", reference_protocol="gravimetric-v1"
        )
        self.hass.now = NOW - 16 * 86400
        self.hass.values["sensor.raw"] = ha_state(
            raw, sampled_at=self.hass.now, samples=[raw] * 11, physical_pin=22
        )

        async def executor(function, *args: Any) -> Any:
            return function(*args)

        self.hass.async_add_executor_job = executor
        self.manager = self.new_runtime(
            self.hass,
            self.store,
            self.config,
            hooks=self.runtime_module.RuntimeHooks(
                clock=lambda: self.hass.now,
                quality_provider=lambda _entry, _now: self.quality,
                context_factory=lambda: SimpleNamespace(id="calibration-context"),
            ),
        )
        self.hass.runtime = self.manager
        await self.manager.async_load()
        await self.manager.async_prepare(
            "plant", "sensor.smooth", self.hass.now, "rest:raw"
        )
        self.hass.now = NOW
        fingerprint = self.manager.attributes("plant")["calibration_source_fingerprint"]
        for cycle, start, end in qualified_cycle_windows(NOW):
            states = [
                ha_state(
                    raw,
                    **physical_attributes(start + offset * 300, raw),
                )
                for offset in range(13)
            ]
            self.manager.seed_history("plant", states, self.quality, NOW)
            self.hass.values["sensor.reference"] = ha_state(
                90,
                **reference_attributes(cycle, start, end, fingerprint),
            )
            self.assertTrue(await self.manager.async_record_reference("plant", NOW))
        self.hass.values["sensor.raw"] = states[-1]

    async def test_qualified_application_persists_intent_before_real_helper_effect(
        self,
    ) -> None:
        await self.qualified_fixture()
        intents = []
        self.hass.services.before_write = lambda: intents.append(
            copy.deepcopy(self.store.data["records"]["sensor.smooth"]["pending"])
        )
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        attrs = self.manager.attributes("plant")
        self.assertEqual(self.hass.services.writes, [("input_number.wet", 222)])
        self.assertEqual(float(self.hass.values["input_number.dry"].state), 67)
        self.assertEqual(intents[0]["expected"], [67, 221])
        self.assertEqual(intents[0]["target"], [67, 222])
        self.assertIsNone(self.store.data["records"]["sensor.smooth"]["pending"])
        self.assertEqual(attrs["calibration_history"][0]["kind"], "automatic")
        self.assertEqual(attrs["calibration_history"][0]["new_wet"], 222)
        self.assertEqual(
            self.manager.reading_fields("plant")["calibration_verified_revision"], 1
        )
        self.assertEqual(attrs["calibration_baseline_wet"], 221)

    async def test_silent_store_write_failure_never_authorizes_helper_effect(
        self,
    ) -> None:
        await self.qualified_fixture()
        prior = copy.deepcopy(self.store.data)
        self.store.drop_save = True
        with self.assertLogs(self.runtime_module.__name__, level="ERROR"):
            await self.manager.async_observe(
                "plant", self.quality, NOW, allow_write=True
            )
        self.assertEqual(self.hass.services.writes, [])
        self.assertEqual(float(self.hass.values["input_number.wet"].state), 221)
        self.assertEqual(self.store.data, prior)
        self.assertIsNone(self.manager.witnesses())
        self.assertFalse(
            self.manager.attributes("plant")["calibration_history_available"]
        )

    async def test_malformed_burst_is_untrusted_without_losing_finite_observations(
        self,
    ) -> None:
        self.hass.values["sensor.raw"] = ha_state(
            100,
            sampled_at=NOW + 1,
            samples=["99", "bad", float("nan"), 101],
            physical_pin=22,
        )
        await self.manager.async_observe("plant", {"reliability_valid": True}, NOW + 1)
        self.assertTrue(await self.manager.async_save())
        point = self.store.data["records"]["sensor.smooth"]["samples"][-1]
        self.assertEqual(point["burst"], [99, 101])
        self.assertFalse(point["reliable"])
        self.assertFalse(point["rollback_eligible"])
        self.assertTrue(
            self.manager.attributes("plant")["calibration_history_available"]
        )

    async def test_disabled_calibration_keeps_history_and_consumes_helper_events(
        self,
    ) -> None:
        self.hass.now = NOW + 1
        self.external_wet_change(225)
        await self.manager.async_prepare("plant", "sensor.smooth", NOW + 1, "rest:raw")
        self.config["automatic_calibration"]["sensor.smooth"]["enabled"] = False
        self.hass.now = NOW + 2
        self.external_wet_change(230)
        await self.manager.async_prepare("plant", "sensor.smooth", NOW + 2, "rest:raw")
        attrs = self.manager.attributes("plant")
        self.assertFalse(attrs["calibration_enabled"])
        self.assertTrue(attrs["calibration_history_available"])
        self.assertEqual(
            [row["new_wet"] for row in attrs["calibration_history"]], [225, 230]
        )
        self.assertFalse(self.manager._events.get("sensor.smooth"))
        await self.manager.async_observe(
            "plant", {"reliability_valid": True}, NOW + 2, allow_write=True
        )
        self.assertEqual(self.hass.services.writes, [])

    async def test_pending_evidence_copies_only_configured_reference_protocol(
        self,
    ) -> None:
        await self.qualified_fixture()
        record = self.manager.records["sensor.smooth"]
        record["references"] += [
            {**reference, "protocol": "retired-protocol"}
            for reference in list(record["references"])
        ]
        intents = []
        self.hass.services.before_write = lambda: intents.append(
            copy.deepcopy(self.store.data["records"]["sensor.smooth"]["pending"])
        )
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        self.assertEqual(self.hass.services.writes, [("input_number.wet", 222)])
        self.assertEqual(len(intents[0]["references"]), 4)
        self.assertEqual(
            {row["protocol"] for row in intents[0]["references"]}, {"gravimetric-v1"}
        )

    async def test_accepted_reference_ids_cannot_reenter_after_retention_window(
        self,
    ) -> None:
        await self.qualified_fixture()
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        at = NOW + 92 * 86400
        source = self.manager.attributes("plant")["calibration_source_fingerprint"]
        self.hass.values["sensor.reference"] = ha_state(
            90,
            reference_id="wet-0",
            cycle_id="cycle-0",
            start_at=at - 3600,
            end_at=at,
            protocol="gravimetric-v1",
            source_fingerprint=source,
        )
        self.assertFalse(await self.manager.async_record_reference("plant", at))
        self.assertTrue(
            self.manager.attributes("plant")["calibration_history_available"]
        )

    async def test_failed_persistence_prevents_helper_write(self) -> None:
        await self.qualified_fixture()
        self.store.fail_save = True
        with self.assertLogs(self.runtime_module.__name__, level="ERROR"):
            await self.manager.async_observe(
                "plant", self.quality, NOW, allow_write=True
            )
        self.assertEqual(self.hass.services.writes, [])
        self.assertEqual(
            self.manager.attributes("plant")["calibration_status"],
            "storage_unavailable",
        )

    async def test_concurrent_manual_change_wins_over_executor_result(self) -> None:
        await self.qualified_fixture()

        async def executor(function, *args: Any) -> Any:
            result = function(*args)
            self.hass.values["input_number.wet"] = ha_state(223)
            return result

        self.hass.async_add_executor_job = executor
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        await self.manager.async_prepare("plant", "sensor.smooth", NOW, "rest:raw")
        self.assertEqual(self.hass.services.writes, [])
        self.assertEqual(float(self.hass.values["input_number.wet"].state), 223)
        row = self.manager.attributes("plant")["calibration_history"][0]
        self.assertEqual(row["kind"], "external_discovered")
        self.assertIsNone(row["at"])
        self.assertEqual(row["detected_at"], NOW)

    async def test_reference_alias_of_raw_signal_is_rejected(self) -> None:
        options = self.config["automatic_calibration"]["sensor.smooth"]
        options.update(
            reference_entity="sensor.reference", reference_protocol="qualified"
        )
        self.hass.values["sensor.reference"] = ha_state(
            90,
            reference_id="self-reference",
            cycle_id="cycle",
            start_at=NOW + 3600,
            end_at=NOW + 7200,
            protocol="qualified",
            source_entity="sensor.raw",
            source_fingerprint=self.manager.attributes("plant")[
                "calibration_source_fingerprint"
            ],
        )
        self.assertFalse(await self.manager.async_record_reference("plant", NOW + 7200))
        self.assertEqual(self.manager.attributes("plant")["calibration_references"], 0)
        self.hass.values["sensor.reference"].attributes.pop("source_entity")
        self.assertTrue(await self.manager.async_record_reference("plant", NOW + 7200))
        self.assertEqual(self.manager.attributes("plant")["calibration_references"], 1)

    async def test_restart_reconciles_uncertain_completed_effect_once(self) -> None:
        await self.qualified_fixture()

        self.hass.services.after_write = raise_interrupted
        with self.assertRaises(Interrupted):
            await self.manager.async_observe(
                "plant", self.quality, NOW, allow_write=True
            )
        self.assertEqual(float(self.hass.values["input_number.wet"].state), 222)
        self.assertIsNotNone(self.store.data["records"]["sensor.smooth"]["pending"])
        restarted = self.new_runtime(
            self.hass,
            self.store,
            self.config,
            hooks=self.runtime_module.RuntimeHooks(clock=lambda: self.hass.now),
        )
        await restarted.async_load()
        await restarted.async_prepare("plant", "sensor.smooth", NOW + 1, "rest:raw")
        await restarted.async_prepare("plant", "sensor.smooth", NOW + 2, "rest:raw")
        attrs = restarted.attributes("plant")
        self.assertEqual(len(self.hass.services.writes), 1)
        self.assertEqual(len(attrs["calibration_history"]), 1)
        self.assertEqual(attrs["calibration_history"][0]["new_wet"], 222)
        self.assertIsNone(attrs["calibration_history"][0]["at"])
        self.assertIsNone(
            restarted.reading_fields("plant")["calibration_verified_revision"]
        )

    async def add_rollback_window(self, *, old_incident: bool = False) -> None:
        at = NOW + 86400
        self.hass.now = at
        self.quality = {
            "reliability_valid": False,
            "reliability_reason": "saturated",
            "reliability_incident_reason": "saturated",
            "reliability_incident_started_at": NOW - 1 if old_incident else NOW + 43201,
            "reliability_invalid_intervals": [
                [NOW - 3600 if old_incident else NOW + 1, None]
            ],
            "last_source_change_at": at - 300,
        }
        states = [
            ha_state(
                221 + (offset % 3 - 1) * 0.2,
                sampled_at=at - 3600 + offset * 300,
                samples=[221 + (offset % 3 - 1) * 0.2] * 11,
                physical_pin=22,
            )
            for offset in range(13)
        ]
        self.manager.seed_history("plant", states, self.quality, at)
        self.hass.values["sensor.raw"] = states[-1]
        self.hass.values["sensor.reference"] = ha_state(
            100,
            reference_id="independent-later",
            cycle_id="later",
            start_at=at - 3600,
            end_at=at,
            protocol="gravimetric-v1",
            source_fingerprint=self.manager.attributes("plant")[
                "calibration_source_fingerprint"
            ],
        )
        self.assertTrue(await self.manager.async_record_reference("plant", at))
        await self.manager.async_observe("plant", self.quality, at, allow_write=True)

    async def test_independent_post_apply_reference_can_rollback_derived_saturation(
        self,
    ) -> None:
        await self.qualified_fixture(raw=203)
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        self.assertEqual(float(self.hass.values["input_number.wet"].state), 220)
        await self.add_rollback_window()
        self.assertEqual(float(self.hass.values["input_number.wet"].state), 221)
        attrs = self.manager.attributes("plant")
        self.assertEqual(
            [row["kind"] for row in attrs["calibration_history"]],
            ["automatic", "rollback"],
        )
        self.assertFalse(self.quality["reliability_valid"])
        record = self.store.data["records"]["sensor.smooth"]
        self.assertIsNone(record["previous_wet"])
        self.assertEqual(len(record["blocked_evidence_ids"]), 2)

    async def test_rollback_exception_never_overrides_a_preexisting_incident(
        self,
    ) -> None:
        await self.qualified_fixture(raw=203)
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        await self.add_rollback_window(old_incident=True)
        self.assertEqual(float(self.hass.values["input_number.wet"].state), 220)
        self.assertEqual(len(self.hass.services.writes), 1)

    def external_wet_change(self, value: float) -> None:
        before = self.hass.values["input_number.wet"]
        after = ha_state(value, self.hass.now)
        self.hass.values["input_number.wet"] = after
        self.manager.note_helper_event(
            helper_event("input_number.wet", before, after, "external")
        )

    async def test_manual_change_during_native_dispatch_is_not_overwritten(
        self,
    ) -> None:
        await self.qualified_fixture()
        self.hass.services.before_write = lambda: self.external_wet_change(230)
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        self.assertEqual(float(self.hass.values["input_number.wet"].state), 230)
        self.assertEqual(self.hass.services.writes, [])
        rows = self.manager.attributes("plant")["calibration_history"]
        self.assertEqual(
            [(row["kind"], row["new_wet"]) for row in rows], [("manual", 230)]
        )
        self.assertIsNone(
            self.manager.reading_fields("plant")["calibration_verified_revision"]
        )

    async def test_manual_change_after_native_effect_remains_the_final_value(
        self,
    ) -> None:
        await self.qualified_fixture()
        self.hass.services.after_write = lambda: self.external_wet_change(230)
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        self.assertEqual(float(self.hass.values["input_number.wet"].state), 230)
        rows = self.manager.attributes("plant")["calibration_history"]
        self.assertEqual(
            [(row["kind"], row["old_wet"], row["new_wet"]) for row in rows],
            [("automatic", 221, 222), ("manual", 222, 230)],
        )
        self.assertIsNone(
            self.manager.reading_fields("plant")["calibration_verified_revision"]
        )

    async def test_nested_manual_callback_before_capture_keeps_actual_effect_order(
        self,
    ) -> None:
        for offset in (0, 0.001):
            with self.subTest(manual_offset=offset):
                self.hass.values["input_number.wet"] = ha_state(221, step=1)
                await self.qualified_fixture()
                capture = self.manager.note_helper_event

                def nested_listener(
                    event: Any, captured: Any = capture, delta: float = offset
                ) -> None:
                    if event.context.id == "calibration-context":
                        self.hass.now = NOW + delta
                        self.external_wet_change(230)
                    captured(event)

                self.manager.note_helper_event = nested_listener
                await self.manager.async_observe(
                    "plant", self.quality, NOW, allow_write=True
                )
                self.assertEqual(float(self.hass.values["input_number.wet"].state), 230)
                attrs = self.manager.attributes("plant")
                self.assertTrue(attrs["calibration_history_available"])
                self.assertEqual(
                    [
                        (row["kind"], row["old_wet"], row["new_wet"])
                        for row in attrs["calibration_history"]
                    ],
                    [("automatic", 221, 222), ("manual", 222, 230)],
                )
                record = self.store.data["records"]["sensor.smooth"]
                self.assertIsNone(record["verified_revision"])
                self.assertIsNone(record["previous_wet"])

    async def test_observed_own_effect_after_manual_change_is_never_verified(
        self,
    ) -> None:
        await self.qualified_fixture()

        async def delayed_dispatch(
            _domain: str, _service: str, data: dict, **options: Any
        ) -> dict:
            self.external_wet_change(230)
            self.hass.now = NOW + 0.001
            before = self.hass.values["input_number.wet"]
            after = ha_state(222, self.hass.now)
            self.hass.values["input_number.wet"] = after
            self.manager.note_helper_event(
                SimpleNamespace(
                    data={
                        "entity_id": "input_number.wet",
                        "old_state": before,
                        "new_state": after,
                    },
                    context=options["context"],
                )
            )
            return {data["entity_id"]: {"applied": True}}

        self.hass.services.async_call = delayed_dispatch
        with self.assertLogs(self.runtime_module.__name__, level="ERROR"):
            await self.manager.async_observe(
                "plant", self.quality, NOW, allow_write=True
            )
        attrs = self.manager.attributes("plant")
        self.assertTrue(attrs["calibration_history_available"])
        self.assertEqual(attrs["calibration_status"], "unsupported_runtime")
        self.assertEqual(
            [
                (row["kind"], row["old_wet"], row["new_wet"])
                for row in attrs["calibration_history"]
            ],
            [("manual", 221, 230), ("automatic", 230, 222)],
        )
        record = self.store.data["records"]["sensor.smooth"]
        self.assertIsNone(record["verified_revision"])
        self.assertIsNone(record["last_evidence_id"])
        self.assertIn(
            record["history"][-1]["evidence_id"], record["blocked_evidence_ids"]
        )
        self.assertEqual(record["history"][-1]["reason"], "concurrent_change")

    async def test_ambiguous_same_time_events_preserve_uncertainty_and_observations(
        self,
    ) -> None:
        await self.qualified_fixture()

        async def ambiguous_dispatch(
            _domain: str, _service: str, data: dict, **options: Any
        ) -> dict:
            before = self.hass.values["input_number.wet"]
            for value, context in [
                (222, options["context"]),
                (230, SimpleNamespace(id="external")),
            ]:
                after = ha_state(value, NOW)
                self.manager.note_helper_event(
                    SimpleNamespace(
                        data={
                            "entity_id": "input_number.wet",
                            "old_state": before,
                            "new_state": after,
                        },
                        context=context,
                    )
                )
            self.hass.values["input_number.wet"] = after
            return {data["entity_id"]: {"applied": True}}

        self.hass.services.async_call = ambiguous_dispatch
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        attrs = self.manager.attributes("plant")
        self.assertTrue(attrs["calibration_history_available"])
        self.assertEqual(float(self.hass.values["input_number.wet"].state), 230)
        self.assertEqual(
            [
                (row["kind"], row["at"], row["old_wet"], row["new_wet"])
                for row in attrs["calibration_history"]
            ],
            [("external_discovered", None, 221, 230)],
        )
        record = self.store.data["records"]["sensor.smooth"]
        self.assertIsNone(record["verified_revision"])
        attempt = record["attempts"][-1]
        self.assertEqual(attempt["outcome"], "interrupted_unknown")
        self.assertEqual(
            [(row["before"], row["after"]) for row in attempt["observed_events"]],
            [(221, 222), (221, 230)],
        )
        self.assertIn(attempt["evidence_id"], record["blocked_evidence_ids"])

    async def test_changed_physical_source_cannot_use_previous_source_evidence(
        self,
    ) -> None:
        await self.qualified_fixture()
        before = copy.deepcopy(self.manager.records["sensor.smooth"]["samples"])
        self.hass.values["sensor.raw"] = ha_state(
            209,
            sampled_at=NOW,
            samples=[208] * 5 + [209] + [210] * 5,
            physical_pin=23,
        )
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        self.assertEqual(self.hass.services.writes, [])
        self.assertEqual(float(self.hass.values["input_number.wet"].state), 221)
        self.assertEqual(self.manager.records["sensor.smooth"]["samples"], before)
        self.assertEqual(
            self.manager.attributes("plant")["calibration_status"], "source_changed"
        )

    async def test_source_change_during_reference_save_cannot_enter_new_write_token(
        self,
    ) -> None:
        await self.qualified_fixture()
        self.hass.values["sensor.reference"].attributes["reference_id"] = "new-holdout"
        self.hass.values["sensor.reference"].attributes["cycle_id"] = "new-cycle"
        save = self.store.async_save

        async def source_changed_during_save(data: dict) -> None:
            await save(data)
            self.hass.values["sensor.raw"] = ha_state(
                209, sampled_at=NOW, samples=[209] * 11, physical_pin=23
            )

        self.store.async_save = source_changed_during_save
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        self.assertEqual(self.hass.services.writes, [])
        self.assertEqual(float(self.hass.values["input_number.wet"].state), 221)
        self.assertEqual(self.manager.attributes("plant")["calibration_history"], [])

    async def test_first_bootstrap_never_takes_current_helper_as_original(self) -> None:
        self.hass.values["input_number.wet"] = ha_state(230)
        manager = self.new_runtime(self.hass, MemoryStore(), self.config)
        await manager.async_load()
        await manager.async_prepare("plant", "sensor.smooth", NOW, "rest:raw")
        attrs = manager.attributes("plant")
        self.assertEqual(attrs["calibration_baseline_wet"], 221)
        self.assertEqual(attrs["calibration_wet_adc"], 230)
        self.assertEqual(attrs["calibration_status"], "outside_baseline_envelope")
        self.assertIsNone(attrs["calibration_history"][0]["at"])

    async def test_missing_known_store_has_unknown_history_and_never_rebases(
        self,
    ) -> None:
        witnesses = self.manager.witnesses()
        missing = self.new_runtime(self.hass, MemoryStore(), self.config)
        with self.assertLogs(self.runtime_module.__name__, level="ERROR"):
            await missing.async_load(witnesses)
        self.hass.values["input_number.wet"] = ha_state(1500)
        await missing.async_prepare("plant", "sensor.smooth", NOW + 1, "rest:raw")
        attrs = missing.attributes("plant")
        self.assertEqual(attrs["calibration_status"], "storage_unavailable")
        self.assertFalse(attrs["calibration_history_available"])
        self.assertEqual(self.hass.services.writes, [])

    async def test_partial_existing_store_cannot_bootstrap_a_deleted_source(
        self,
    ) -> None:
        self.store.data["records"].pop("sensor.smooth")
        restarted = self.new_runtime(self.hass, self.store, self.config)
        with self.assertLogs(self.runtime_module.__name__, level="ERROR"):
            await restarted.async_load()
        await restarted.async_prepare("plant", "sensor.smooth", NOW + 1, "rest:raw")
        self.assertFalse(restarted.attributes("plant")["calibration_history_available"])

    async def test_source_return_never_reactivates_earlier_evidence_epoch(self) -> None:
        first = self.manager.attributes("plant")["calibration_source_fingerprint"]
        options = self.config["automatic_calibration"]["sensor.smooth"]
        for offset, revision in [(1, "probe-v2"), (2, "probe-v1")]:
            options["source_revision"] = revision
            await self.manager.async_prepare(
                "plant", "sensor.smooth", NOW + offset, "rest:raw"
            )
        attrs = self.manager.attributes("plant")
        self.assertNotEqual(attrs["calibration_source_fingerprint"], first)
        self.assertEqual(attrs["calibration_status"], "source_changed")
        self.assertEqual(self.manager.records["sensor.smooth"]["source_epoch"], 2)

    async def test_new_source_epoch_never_relabels_earlier_recorder_samples(
        self,
    ) -> None:
        self.config["automatic_calibration"]["sensor.smooth"]["source_revision"] = (
            "probe-v2"
        )
        await self.manager.async_prepare(
            "plant", "sensor.smooth", NOW + 600, "rest:raw"
        )
        old_sample = ha_state(
            208, NOW + 300, sampled_at=NOW + 300, samples=[208] * 11, physical_pin=22
        )
        current_sample = ha_state(
            209, NOW + 900, sampled_at=NOW + 900, samples=[209] * 11, physical_pin=22
        )
        self.manager.seed_history(
            "plant",
            [old_sample, current_sample],
            {"reliability_valid": True},
            NOW + 900,
        )
        record = self.manager.records["sensor.smooth"]
        self.assertEqual(
            [
                (point["sampled_at"], point["source_fingerprint"])
                for point in record["samples"]
            ],
            [(NOW + 900, record["source_fingerprint"])],
        )

    async def test_reference_before_current_epoch_is_rejected_without_losing_history(
        self,
    ) -> None:
        options = self.config["automatic_calibration"]["sensor.smooth"]
        options.update(
            source_revision="probe-v2",
            reference_entity="sensor.reference",
            reference_protocol="gravimetric-v1",
        )
        at = NOW + 7200
        await self.manager.async_prepare("plant", "sensor.smooth", at, "rest:raw")
        self.hass.values["sensor.reference"] = ha_state(
            90,
            reference_id="stale-epoch-window",
            cycle_id="older-probe-cycle",
            start_at=NOW + 300,
            end_at=NOW + 3900,
            protocol="gravimetric-v1",
            source_fingerprint=self.manager.attributes("plant")[
                "calibration_source_fingerprint"
            ],
        )
        self.assertFalse(await self.manager.async_record_reference("plant", at))
        self.assertTrue(
            self.manager.attributes("plant")["calibration_history_available"]
        )
        self.assertEqual(self.manager.records["sensor.smooth"]["references"], [])

    async def test_blocked_native_write_reason_is_visible_with_existing_history(
        self,
    ) -> None:
        await self.qualified_fixture()
        self.manager.inhibit_writes("unsupported_runtime")
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        self.assertEqual(self.hass.services.writes, [])
        attrs = self.manager.attributes("plant")
        self.assertEqual(attrs["calibration_status"], "unsupported_runtime")
        self.assertTrue(attrs["calibration_history_available"])

    async def test_user_observation_does_not_authorize_a_system_write(self) -> None:
        await self.qualified_fixture()
        await self.manager.async_observe("plant", self.quality, NOW)
        self.assertEqual(self.hass.services.writes, [])
        self.assertIsNone(self.manager.records["sensor.smooth"]["pending"])
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        self.assertEqual(self.hass.services.writes, [("input_number.wet", 222)])

    async def test_external_dry_change_before_evaluation_cannot_enter_a_new_token(
        self,
    ) -> None:
        await self.qualified_fixture()
        before = self.hass.values["input_number.dry"]
        after = ha_state(68)
        self.hass.values["input_number.dry"] = after
        self.manager.note_helper_event(
            helper_event("input_number.dry", before, after, "external-dry")
        )
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        self.assertEqual(self.hass.services.writes, [])
        self.assertEqual(float(self.hass.values["input_number.dry"].state), 68)

    async def test_attribute_only_helper_report_invalidates_inflight_expectations(
        self,
    ) -> None:
        await self.qualified_fixture()

        async def executor(function, *args: Any) -> Any:
            decision = function(*args)
            before = self.hass.values["input_number.wet"]
            after = ha_state(221, step=0.5)
            self.hass.values["input_number.wet"] = after
            self.manager.note_helper_event(
                helper_event("input_number.wet", before, after, "changed-step")
            )
            return decision

        self.hass.async_add_executor_job = executor
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        self.assertEqual(self.hass.services.writes, [])
        self.assertEqual(self.manager.attributes("plant")["calibration_history"], [])

    async def test_removed_and_returned_helper_invalidates_inflight_expectations(
        self,
    ) -> None:
        await self.qualified_fixture()

        async def executor(function, *args: Any) -> Any:
            decision = function(*args)
            before = self.hass.values.pop("input_number.wet")
            for old, new in [(before, None), (None, before)]:
                if new is not None:
                    self.hass.values["input_number.wet"] = new
                self.manager.note_helper_event(
                    helper_event("input_number.wet", old, new, "recreated-helper")
                )
            return decision

        self.hass.async_add_executor_job = executor
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        self.assertEqual(self.hass.services.writes, [])
        self.assertEqual(float(self.hass.values["input_number.wet"].state), 221)

    async def test_shutdown_disarms_the_guard_without_touching_helper(self) -> None:
        await self.qualified_fixture()
        self.hass.services.before_write = self.manager.stop_writes
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        self.assertEqual(self.hass.services.writes, [])
        record = self.store.data["records"]["sensor.smooth"]
        self.assertEqual(record["history"], [])
        self.assertEqual(record["attempts"][-1]["outcome"], "aborted")
        self.assertEqual(record["blocked_evidence_ids"], [])

    async def test_interrupted_attempt_returning_to_expected_is_not_claimed_as_no_effect(
        self,
    ) -> None:
        await self.qualified_fixture()

        self.hass.services.before_write = raise_interrupted
        with self.assertRaises(Interrupted):
            await self.manager.async_observe(
                "plant", self.quality, NOW, allow_write=True
            )
        restarted = self.new_runtime(
            self.hass,
            self.store,
            self.config,
            hooks=self.runtime_module.RuntimeHooks(
                clock=lambda: self.hass.now,
                quality_provider=lambda _entry, _now: self.quality,
                context_factory=lambda: SimpleNamespace(id="after-unknown-attempt"),
            ),
        )
        await restarted.async_load(self.manager.witnesses())
        await restarted.async_prepare("plant", "sensor.smooth", NOW + 1, "rest:raw")
        record = self.store.data["records"]["sensor.smooth"]
        self.assertEqual(record["current"], [67, 221])
        self.assertEqual(record["history"], [])
        self.assertEqual(record["attempts"][-1]["outcome"], "interrupted_unknown")
        self.assertIn(
            record["attempts"][-1]["evidence_id"], record["blocked_evidence_ids"]
        )
        self.manager = restarted
        self.hass.runtime = restarted
        self.hass.services.before_write = None
        self.hass.now = NOW + 7200
        start = self.hass.now - 3600
        states = [
            ha_state(
                209,
                sampled_at=start + offset * 300,
                samples=[208] * 5 + [209] + [210] * 5,
                physical_pin=22,
            )
            for offset in range(13)
        ]
        restarted.seed_history("plant", states, self.quality, self.hass.now)
        self.hass.values["sensor.raw"] = states[-1]
        self.hass.values["sensor.reference"] = ha_state(
            90,
            reference_id="new-holdout-after-interruption",
            cycle_id="new-holdout-cycle",
            start_at=start,
            end_at=self.hass.now,
            protocol="gravimetric-v1",
            source_fingerprint=record["source_fingerprint"],
        )
        self.assertTrue(await restarted.async_record_reference("plant", self.hass.now))
        await restarted.async_observe(
            "plant", self.quality, self.hass.now, allow_write=True
        )
        self.assertEqual(self.hass.services.writes, [])
        attrs = restarted.attributes("plant")
        self.assertEqual(attrs["calibration_status"], "cooldown")
        self.assertEqual(attrs["calibration_history"], [])
        self.assertIsNone(attrs["calibration_last_change_at"])
        self.assertIsNone(restarted.records["sensor.smooth"]["last_applied_at"])

    async def test_unknown_attempt_witness_rejects_older_primary_snapshot(self) -> None:
        await self.qualified_fixture()
        older = copy.deepcopy(self.store.data)

        self.hass.services.before_write = raise_interrupted
        with self.assertRaises(Interrupted):
            await self.manager.async_observe(
                "plant", self.quality, NOW, allow_write=True
            )
        restarted = self.new_runtime(self.hass, self.store, self.config)
        await restarted.async_load(self.manager.witnesses())
        await restarted.async_prepare("plant", "sensor.smooth", NOW + 1, "rest:raw")
        witness = restarted.witnesses()["sensor.smooth"]
        self.assertEqual(witness.get("decision_fence_at"), NOW + 1)
        self.assertEqual(witness["revision"], 0)
        self.assertIsNone(witness["last_applied_at"])
        restored_store = MemoryStore()
        restored_store.data = older
        restored = self.new_runtime(self.hass, restored_store, self.config)
        with self.assertLogs(self.runtime_module.__name__, level="ERROR"):
            await restored.async_load(restarted.witnesses())
        await restored.async_prepare("plant", "sensor.smooth", NOW + 2, "rest:raw")
        self.assertFalse(restored.attributes("plant")["calibration_history_available"])
        self.assertEqual(self.hass.services.writes, [])

    async def test_witness_merge_keeps_decision_fence_independent_of_actual_rank(
        self,
    ) -> None:
        merge = self.runtime_module.collect_witnesses
        initial = self.manager.witnesses()["sensor.smooth"]
        stronger_actual = {
            **initial,
            "revision": 1,
            "last_event_id": "actual-event",
            "last_applied_at": NOW + 1,
            "decision_fence_at": NOW + 1,
        }
        later_unknown = {**initial, "decision_fence_at": NOW + 2}
        for values in [
            (stronger_actual, later_unknown),
            (later_unknown, stronger_actual),
        ]:
            with self.subTest(first_revision=values[0]["revision"]):
                records = {
                    key: {"calibration_witnesses": {"sensor.smooth": value}}
                    for key, value in zip(("first", "second"), values, strict=True)
                }
                before = copy.deepcopy(records)
                witness = merge(records)["sensor.smooth"]
                self.assertEqual(witness["revision"], 1)
                self.assertEqual(witness["last_applied_at"], NOW + 1)
                self.assertEqual(witness["last_event_id"], "actual-event")
                self.assertEqual(witness["decision_fence_at"], NOW + 2)
                self.assertEqual(records, before)

    async def test_witness_merge_does_not_repair_missing_or_invalid_time_fence(
        self,
    ) -> None:
        merge = self.runtime_module.collect_witnesses
        initial = {
            **self.manager.witnesses()["sensor.smooth"],
            "decision_fence_at": None,
        }
        for value in (MISSING, "123", -1, True):
            with self.subTest(fence=value):
                bad = {**initial, "revision": 1}
                if value is MISSING:
                    bad.pop("decision_fence_at")
                else:
                    bad["decision_fence_at"] = value
                records = {
                    "a": {"calibration_witnesses": {"sensor.smooth": bad}},
                    "b": {"calibration_witnesses": {"sensor.smooth": initial}},
                }
                result = merge(records)["sensor.smooth"]
                if value is MISSING:
                    self.assertNotIn("decision_fence_at", result)
                else:
                    self.assertEqual(result["decision_fence_at"], value)

    async def test_out_of_range_observations_preserve_values_without_training_flags(
        self,
    ) -> None:
        await self.qualified_fixture(raw=203)
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        cases = [
            (-1, [-1] * 11, False),
            (4096, [4096] * 11, False),
            (180, [180] * 10 + [-1], False),
            (180, [180] * 10 + [4096], False),
            (0, [0] * 11, True),
            (4095, [4095] * 11, True),
        ]
        for offset, (raw, burst, trusted) in enumerate(cases, start=1):
            with self.subTest(raw=raw, burst_tail=burst[-1]):
                state = ha_state(
                    raw,
                    sampled_at=NOW + offset,
                    samples=burst,
                    physical_pin=22,
                )
                self.manager.seed_history("plant", [state], self.quality, NOW + offset)
                point = self.manager.records["sensor.smooth"]["samples"][-1]
                self.assertEqual(point["raw"], raw)
                self.assertEqual(point["burst"], burst)
                self.assertIs(point["reliable"], trusted)
                self.assertIs(point["rollback_eligible"], trusted)
        self.assertTrue(await self.manager.async_save())
        persisted = self.store.data["records"]["sensor.smooth"]["samples"][
            -len(cases) :
        ]
        self.assertEqual(
            [(point["raw"], point["burst"]) for point in persisted],
            [(raw, burst) for raw, burst, _ in cases],
        )

    async def test_out_of_range_duplicate_revokes_both_existing_training_flags(
        self,
    ) -> None:
        await self.qualified_fixture(raw=203)
        await self.manager.async_observe("plant", self.quality, NOW, allow_write=True)
        for offset, (raw, burst) in enumerate(
            [(4096, [4096] * 11), (180, [180] * 10 + [-1])], start=1
        ):
            with self.subTest(raw=raw, burst_tail=burst[-1]):
                at = NOW + offset
                original = ha_state(
                    180, sampled_at=at, samples=[180] * 11, physical_pin=22
                )
                bad = ha_state(raw, sampled_at=at, samples=burst, physical_pin=22)
                self.manager.seed_history("plant", [original], self.quality, at)
                self.manager.seed_history("plant", [bad], self.quality, at)
                point = self.manager.records["sensor.smooth"]["samples"][-1]
                self.assertFalse(point["reliable"])
                self.assertFalse(point["rollback_eligible"])

    async def test_history_does_not_label_pre_incident_samples_from_current_fault(
        self,
    ) -> None:
        states = [
            ha_state(180, sampled_at=NOW + offset, samples=[180] * 11, physical_pin=22)
            for offset in (1, 2)
        ]
        self.manager.seed_history(
            "plant",
            states,
            {
                "reliability_valid": False,
                "reliability_invalid_intervals": [[NOW + 2, None]],
            },
            NOW + 3,
        )
        samples = self.manager.records["sensor.smooth"]["samples"]
        self.assertEqual([point["reliable"] for point in samples], [True, False])


if __name__ == "__main__":
    unittest.main()
