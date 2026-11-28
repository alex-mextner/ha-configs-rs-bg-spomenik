# Copyright (c) 2026 Alex Ultra
# SPDX-License-Identifier: MIT
"""Prove helper concurrency through installed HA services in native CI only."""

from __future__ import annotations

import copy
import importlib.util
import os
import tempfile
import time
import unittest
from typing import TYPE_CHECKING
from unittest.mock import patch

from calibration_fixtures import (
    physical_attributes,
    qualified_cycle_windows,
    reference_attributes,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

HA_AVAILABLE = importlib.util.find_spec("homeassistant") is not None
if not HA_AVAILABLE and os.environ.get("PLANT_NATIVE_HA_REQUIRED") == "1":
    message = "Native Home Assistant is required by this CI job"
    raise RuntimeError(message)
if HA_AVAILABLE:
    from homeassistant import auth, bootstrap, config_entries, loader
    from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_READ_ONLY
    from homeassistant.const import EVENT_CALL_SERVICE, EVENT_STATE_CHANGED
    from homeassistant.core import Context, HomeAssistant, State, callback
    from homeassistant.exceptions import Unauthorized
    from homeassistant.setup import async_setup_component
    from homeassistant.util.file import WriteError

    from custom_components import plant_care_alerts
    from custom_components.plant_care_alerts.calibration_runtime import (
        CalibrationRuntime,
        RuntimeHooks,
    )
    from custom_components.plant_care_alerts.const import (
        APPLY_CALIBRATION_SERVICE,
        CALIBRATION_STORE_KEY,
        DOMAIN,
    )
    from custom_components.plant_care_alerts.persistence import new_store

SOURCE = "sensor.smooth"
RAW = "sensor.raw"
DRY = "input_number.dry"
WET = "input_number.wet"
REFERENCE = "sensor.reference"
DAY = 86400


class BoundaryStore:
    """Use native disk storage with a controlled pending-save callback in CI."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.native = new_store(hass, CALIBRATION_STORE_KEY)
        self.data: dict | None = None
        self.on_pending: Callable[[], Awaitable[None]] | None = None
        self.fail_pending_write = False
        self.failed_write_results: list[None] = []

    async def async_load(self) -> dict | None:
        return await self.native.async_load()

    async def async_read_persisted(self) -> dict | None:
        reader = new_store(self.hass, CALIBRATION_STORE_KEY)
        return copy.deepcopy(await reader.async_load())

    async def async_save(self, data: dict) -> None:
        pending = any(row.get("pending") for row in data["records"].values())
        if pending and self.fail_pending_write:
            with patch(
                "homeassistant.helpers.storage.write_utf8_file_atomic",
                side_effect=WriteError("Isolated native CI write failure"),
            ):
                result = await self.native.async_save(data)
            self.failed_write_results.append(result)
        else:
            await self.native.async_save(data)
        self.data = await self.async_read_persisted()
        if self.on_pending and pending:
            pending_callback, self.on_pending = self.on_pending, None
            await pending_callback()


@unittest.skipUnless(
    HA_AVAILABLE, "Run native service tests in the installed-HA CI job"
)
class CalibrationNativeServiceTests(unittest.IsolatedAsyncioTestCase):
    """Exercise actual native dispatch, event reentrancy and helper values."""

    async def asyncSetUp(self) -> None:
        config_dir = tempfile.TemporaryDirectory(prefix="ha-calibration-native-")
        self.addCleanup(config_dir.cleanup)
        self.hass = HomeAssistant(config_dir.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        loader.async_setup(self.hass)
        self.hass.config_entries = config_entries.ConfigEntries(self.hass, {})
        # Match native HA's eager registry/bootstrap ordering before AuthStore
        # requests the loaded device registry. Do not create partial registries.
        self.assertTrue(await bootstrap.async_load_base_functionality(self.hass))
        self.hass.auth = await auth.auth_manager_from_config(self.hass, [], [])
        configured = await async_setup_component(
            self.hass,
            "input_number",
            {
                "input_number": {
                    "dry": {"initial": 67, "min": 0, "max": 4095, "step": 1},
                    "wet": {"initial": 221, "min": 0, "max": 4095, "step": 1},
                }
            },
        )
        self.assertTrue(configured)
        await self.hass.async_start()
        await self.hass.async_block_till_done()
        self.now = time.time() - 16 * DAY
        self.auto_context = Context()
        self.quality = {"reliability_valid": True}
        self.store = BoundaryStore(self.hass)
        await self._prepare_runtime()
        self.now += 16 * DAY
        await self._seed_qualified_history()

    async def _prepare_runtime(self) -> None:
        config = {
            "confirmation_hours": 6,
            "overdue_hours": 24,
            "stale_minutes": 60,
            "freshness_sources": {SOURCE: RAW},
            "calibration_sources": {SOURCE: {"dry": DRY, "wet": WET}},
            "automatic_calibration": {
                SOURCE: {
                    "enabled": True,
                    "source_revision": "native-test-probe-v1",
                    "original_dry": 67,
                    "original_wet": 221,
                    "reference_entity": REFERENCE,
                    "reference_protocol": "gravimetric-v1",
                }
            },
        }
        self.manager = plant_care_alerts.CareManager(self.hass, config)
        self.runtime = CalibrationRuntime(
            self.hass,
            self.store,
            config,
            hooks=RuntimeHooks(
                clock=lambda: self.now,
                quality_provider=lambda _entry, _at: self.quality,
                context_factory=lambda: self.auto_context,
                read_persisted=self.store.async_read_persisted,
                confirm_witnesses=self.manager._confirm_calibration_witnesses,
            ),
        )
        self.manager.calibration = self.runtime
        self.manager.records["plant"] = {}
        self.hass.data[plant_care_alerts.DOMAIN] = self.manager
        plant_care_alerts._register_calibration_service(self.hass, self.manager)

        @callback
        def capture_helper_change(event) -> None:
            self.runtime.note_helper_event(event)

        self._capture_helper_change = capture_helper_change
        self._helper_change_unsubscribe = self.hass.bus.async_listen(
            EVENT_STATE_CHANGED, capture_helper_change
        )
        self.addCleanup(self._unsubscribe_helper_changes)
        self._set_raw(self.now)
        await self.runtime.async_load()
        await self.runtime.async_prepare("plant", SOURCE, self.now, "rest:raw")
        await self.manager.store.async_save(copy.deepcopy(self.manager.records))
        self.assertEqual(
            await new_store(self.hass, DOMAIN).async_load(), self.manager.records
        )
        self.assertTrue(
            self.hass.services.has_service(
                plant_care_alerts.DOMAIN, "apply_calibration"
            ),
            "The native calibration service must be registered for these cases",
        )
        self.assertNotIn(
            self.runtime.attributes("plant")["calibration_status"],
            {"unsupported_runtime", "service_conflict", "storage_unavailable"},
            "The native HA service and durable storage preconditions must be ready",
        )

    def _unsubscribe_helper_changes(self) -> None:
        if self._helper_change_unsubscribe is not None:
            self._helper_change_unsubscribe()
            self._helper_change_unsubscribe = None

    def _set_raw(self, sampled_at: float) -> None:
        self.hass.states.async_set(
            RAW,
            "209",
            physical_attributes(sampled_at),
        )

    async def _seed_qualified_history(self) -> None:
        fingerprint = self.runtime.attributes("plant")["calibration_source_fingerprint"]
        for cycle, start, end in qualified_cycle_windows(self.now):
            samples = [
                State(
                    RAW,
                    "209",
                    physical_attributes(start + offset * 300),
                )
                for offset in range(13)
            ]
            self.runtime.seed_history("plant", samples, self.quality, self.now)
            self.hass.states.async_set(
                REFERENCE,
                "90",
                reference_attributes(
                    cycle,
                    start,
                    end,
                    fingerprint,
                ),
            )
            self.assertTrue(
                await self.runtime.async_record_reference("plant", self.now)
            )
        self._set_raw(self.now)

    async def _manual_write(self) -> None:
        await self.hass.services.async_call(
            "input_number",
            "set_value",
            {"entity_id": WET, "value": 230},
            blocking=True,
            context=Context(),
        )

    async def _complete_cycle(self) -> dict:
        await self.runtime.async_observe(
            "plant", self.quality, self.now, allow_write=True
        )
        await self.hass.async_block_till_done()
        await self.runtime.async_prepare("plant", SOURCE, self.now, "rest:raw")
        await self.hass.async_block_till_done()
        return self.runtime.attributes("plant")

    def _assert_manual_value_survived(self, attributes: dict) -> None:
        self.assertEqual(float(self.hass.states.get(WET).state), 230)
        self.assertEqual(float(self.hass.states.get(DRY).state), 67)
        self.assertEqual(attributes["calibration_wet_adc"], 230)
        history = attributes["calibration_history"]
        self.assertTrue(history)
        self.assertEqual(history[-1]["new_wet"], 230)
        self.assertEqual(history[-1]["kind"], "manual")
        self.assertIsNone(
            self.runtime.reading_fields("plant")["calibration_verified_revision"]
        )

    async def test_qualified_change_uses_the_real_registered_helper(self) -> None:
        attributes = await self._complete_cycle()
        self.assertEqual(float(self.hass.states.get(WET).state), 222)
        self.assertEqual(float(self.hass.states.get(DRY).state), 67)
        self.assertEqual(
            [row["kind"] for row in attributes["calibration_history"]], ["automatic"]
        )
        self.assertIsNone(self.store.data["records"][SOURCE]["pending"])

    async def test_native_store_swallows_write_error_without_authorizing_effect(
        self,
    ) -> None:
        before = await self.store.async_read_persisted()
        self.assertIsNotNone(before)
        self.store.fail_pending_write = True
        with (
            self.assertLogs("homeassistant.helpers.storage", level="ERROR"),
            self.assertLogs(
                "custom_components.plant_care_alerts.calibration_runtime", level="ERROR"
            ),
        ):
            attributes = await self._complete_cycle()
        self.assertEqual(self.store.failed_write_results, [None])
        self.assertEqual(await self.store.async_read_persisted(), before)
        self.assertEqual(float(self.hass.states.get(WET).state), 221)
        self.assertEqual(float(self.hass.states.get(DRY).state), 67)
        self.assertFalse(attributes["calibration_history_available"])
        self.assertEqual(attributes["calibration_history"], [])
        self.assertIsNone(self.runtime.witnesses())

    async def test_care_witness_write_error_prevents_native_helper_effect(self) -> None:
        before = await new_store(self.hass, DOMAIN).async_load()
        self.assertIsNotNone(before)
        native_save = self.manager.store.async_save
        attempted_snapshots = []
        calibration_calls = []

        async def fail_care_write(snapshot: dict) -> None:
            attempted_snapshots.append(copy.deepcopy(snapshot))
            with patch(
                "homeassistant.helpers.storage.write_utf8_file_atomic",
                side_effect=WriteError("Isolated native care witness write failure"),
            ):
                result = await native_save(snapshot)
            self.assertIsNone(result)

        @callback
        def capture_calibration_call(event) -> None:
            if (
                event.data.get("domain") == DOMAIN
                and event.data.get("service") == APPLY_CALIBRATION_SERVICE
            ):
                calibration_calls.append(event)

        self.addCleanup(
            self.hass.bus.async_listen(EVENT_CALL_SERVICE, capture_calibration_call)
        )
        with (
            patch.object(self.manager.store, "async_save", fail_care_write),
            self.assertLogs("homeassistant.helpers.storage", level="ERROR"),
        ):
            attributes = await self._complete_cycle()
        self.assertTrue(attempted_snapshots)
        self.assertNotEqual(attempted_snapshots[0], before)
        self.assertEqual(await new_store(self.hass, DOMAIN).async_load(), before)
        self.assertEqual(self.manager.records, before)
        self.assertEqual(calibration_calls, [])
        self.assertEqual(float(self.hass.states.get(WET).state), 221)
        self.assertEqual(float(self.hass.states.get(DRY).state), 67)
        self.assertEqual(attributes["calibration_history"], [])
        self.assertIsNone(
            self.runtime.reading_fields("plant")["calibration_verified_revision"]
        )

    async def test_manual_write_during_pending_save_prevents_stale_application(
        self,
    ) -> None:
        self.store.on_pending = self._manual_write
        attributes = await self._complete_cycle()
        self._assert_manual_value_survived(attributes)
        self.assertEqual(len(attributes["calibration_history"]), 1)

    async def test_queued_manual_write_after_automatic_setter_remains_last(
        self,
    ) -> None:
        manual_tasks = []

        @callback
        def after_automatic_write(event) -> None:
            if event.data.get("entity_id") != WET or event.context != self.auto_context:
                return
            if event.data.get("new_state") is None:
                return
            manual_tasks.append(
                self.hass.async_create_task(
                    self._manual_write(),
                    "queued calibration manual change",
                    eager_start=False,
                )
            )

        self.addCleanup(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, after_automatic_write)
        )
        attributes = await self._complete_cycle()
        self.assertEqual(len(manual_tasks), 1)
        self.assertTrue(manual_tasks[0].done())
        self.assertIsNone(manual_tasks[0].exception())
        self._assert_manual_value_survived(attributes)
        self.assertEqual(
            [row["kind"] for row in attributes["calibration_history"]],
            ["automatic", "manual"],
        )

    async def _assert_eager_manual_history(
        self, *, manual_listener_first: bool
    ) -> None:
        manual_tasks = []
        observed_transitions = []
        values_before_outer_setter_returns = []

        @callback
        def write_manual_after_actual_automatic_change(event) -> None:
            if event.data.get("entity_id") != WET:
                return
            old_state = event.data.get("old_state")
            new_state = event.data.get("new_state")
            if old_state is None or new_state is None:
                return
            observed_transitions.append(
                (float(old_state.state), float(new_state.state), event.context.id)
            )
            if event.context != self.auto_context or manual_tasks:
                return
            manual_tasks.append(
                self.hass.async_create_task(
                    self._manual_write(),
                    "eager manual change after automatic state event",
                    eager_start=True,
                )
            )
            values_before_outer_setter_returns.append(
                float(self.hass.states.get(WET).state)
            )

        if manual_listener_first:
            self._unsubscribe_helper_changes()
        self.addCleanup(
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, write_manual_after_actual_automatic_change
            )
        )
        if manual_listener_first:
            self._helper_change_unsubscribe = self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._capture_helper_change
            )
        attributes = await self._complete_cycle()
        self.assertEqual(len(manual_tasks), 1)
        self.assertTrue(manual_tasks[0].done())
        self.assertIsNone(manual_tasks[0].exception())
        self.assertEqual(values_before_outer_setter_returns, [230])
        self.assertEqual(
            [(old, new) for old, new, _context in observed_transitions],
            [(221, 222), (222, 230)],
        )
        self.assertEqual(observed_transitions[0][2], self.auto_context.id)
        self.assertNotEqual(observed_transitions[1][2], self.auto_context.id)
        self._assert_manual_value_survived(attributes)
        self.assertEqual(
            [
                (row["kind"], row["old_wet"], row["new_wet"])
                for row in attributes["calibration_history"]
            ],
            [("automatic", 221, 222), ("manual", 222, 230)],
        )

    async def test_eager_manual_change_when_capture_listener_registered_first(
        self,
    ) -> None:
        await self._assert_eager_manual_history(manual_listener_first=False)

    async def test_eager_manual_change_when_manual_listener_registered_first(
        self,
    ) -> None:
        await self._assert_eager_manual_history(manual_listener_first=True)

    async def test_synchronous_call_event_reentrancy_cannot_overwrite_manual_value(
        self,
    ) -> None:
        manual_tasks = []
        values_before_outer_handler = []

        @callback
        def reentrant_manual_write(event) -> None:
            if event.context != self.auto_context or manual_tasks:
                return
            task = self.hass.async_create_task(
                self._manual_write(),
                "reentrant calibration manual change",
                eager_start=True,
            )
            manual_tasks.append(task)
            values_before_outer_handler.append(float(self.hass.states.get(WET).state))

        self.addCleanup(
            self.hass.bus.async_listen(EVENT_CALL_SERVICE, reentrant_manual_write)
        )
        attributes = await self._complete_cycle()
        self.assertEqual(len(manual_tasks), 1)
        self.assertTrue(manual_tasks[0].done())
        self.assertIsNone(manual_tasks[0].exception())
        self.assertEqual(values_before_outer_handler, [230])
        self._assert_manual_value_survived(attributes)
        self.assertEqual(len(attributes["calibration_history"]), 1)

    async def test_admin_cannot_reuse_a_live_transaction_from_another_context(
        self,
    ) -> None:
        user = await self.hass.auth.async_create_user(
            "Calibration administrator", group_ids=[GROUP_ID_ADMIN]
        )
        foreign_context = Context(user_id=user.id)
        foreign_tasks = []
        observed_contexts = []
        values_before_original_handler = []
        completed_before_original_handler = []

        async def copied_request(data: dict) -> dict:
            return await self.hass.services.async_call(
                plant_care_alerts.DOMAIN,
                "apply_calibration",
                dict(data),
                blocking=True,
                return_response=True,
                context=foreign_context,
            )

        @callback
        def use_exposed_transaction(event) -> None:
            if event.context == foreign_context:
                observed_contexts.append((event.context.id, event.context.user_id))
                return
            if event.context != self.auto_context or foreign_tasks:
                return
            foreign_tasks.append(
                self.hass.async_create_task(
                    copied_request(event.data["service_data"]),
                    "foreign calibration transaction attempt",
                    eager_start=True,
                )
            )
            values_before_original_handler.append(
                float(self.hass.states.get(WET).state)
            )
            completed_before_original_handler.append(foreign_tasks[-1].done())

        self.addCleanup(
            self.hass.bus.async_listen(EVENT_CALL_SERVICE, use_exposed_transaction)
        )
        attributes = await self._complete_cycle()
        self.assertEqual(len(foreign_tasks), 1)
        self.assertTrue(foreign_tasks[0].done())
        self.assertEqual(foreign_tasks[0].result(), {WET: {"applied": False}})
        self.assertEqual(observed_contexts, [(foreign_context.id, user.id)])
        self.assertEqual(values_before_original_handler, [221])
        self.assertEqual(completed_before_original_handler, [True])
        self.assertEqual(float(self.hass.states.get(WET).state), 222)
        self.assertEqual(self.hass.states.get(WET).context, self.auto_context)
        self.assertEqual(
            [row["kind"] for row in attributes["calibration_history"]], ["automatic"]
        )

    async def test_read_only_user_cannot_invoke_the_guarded_write_service(self) -> None:
        # HA makes its first human user the owner, regardless of the requested group.
        owner = await self.hass.auth.async_create_user(
            "Calibration owner", group_ids=[GROUP_ID_ADMIN]
        )
        self.assertTrue(owner.is_owner)
        self.assertTrue(owner.is_admin)
        user = await self.hass.auth.async_create_user(
            "Calibration observer", group_ids=[GROUP_ID_READ_ONLY]
        )
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_owner)
        self.assertFalse(user.is_admin)
        self.assertEqual([group.id for group in user.groups], [GROUP_ID_READ_ONLY])
        caller_context = Context(user_id=user.id)
        observed_contexts = []

        @callback
        def capture_context(event) -> None:
            observed_contexts.append((event.context.id, event.context.user_id))

        self.addCleanup(self.hass.bus.async_listen(EVENT_CALL_SERVICE, capture_context))
        with self.assertRaises(Unauthorized):
            await self.hass.services.async_call(
                plant_care_alerts.DOMAIN,
                "apply_calibration",
                {"entity_id": WET, "transaction_id": self.auto_context.id},
                blocking=True,
                return_response=True,
                context=caller_context,
            )
        self.assertEqual(observed_contexts, [(caller_context.id, user.id)])
        self.assertEqual(float(self.hass.states.get(WET).state), 221)
        self.assertEqual(self.runtime.attributes("plant")["calibration_history"], [])


if __name__ == "__main__":
    unittest.main()
