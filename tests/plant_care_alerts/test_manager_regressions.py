# Copyright (c) 2026 Alex Ultra
# SPDX-License-Identifier: MIT
"""Exercise delivery retries and manager scheduling without live devices."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Context

from custom_components.plant_care_alerts import (
    DOMAIN,
    CareManager,
    acknowledge_delivery,
    async_setup,
    needs_notification,
)
from custom_components.plant_care_alerts.state_machine import evaluate

MODULE = "custom_components.plant_care_alerts"
TEST_TIMEOUT = 5


class TestManagerRegressions(unittest.IsolatedAsyncioTestCase):
    """Check observable manager behavior at asynchronous boundaries."""

    def setUp(self) -> None:
        self.now = datetime(2026, 11, 18, 12, tzinfo=UTC)
        self.hass = SimpleNamespace(
            data={},
            states=SimpleNamespace(get=lambda _: None),
            services=SimpleNamespace(async_register=Mock()),
            bus=SimpleNamespace(
                async_fire=Mock(),
                async_listen=Mock(return_value=lambda: None),
                async_listen_once=Mock(),
            ),
            config_entries=SimpleNamespace(async_get_entry=Mock(return_value=object())),
        )
        with patch(f"{MODULE}.Store"):
            self.manager = CareManager(
                self.hass,
                {
                    "confirmation_hours": 6,
                    "overdue_hours": 24,
                    "stale_minutes": 20,
                    "freshness_sources": {"sensor.smooth": "sensor.raw"},
                    "voice_speaker": "media_player.yandex_station_test",
                },
            )
        self.manager.add_entities = Mock()
        self.manager.active_plants = {"a"}
        self.manager.records["a"] = {
            "status": "due",
            "due_since": self.now.timestamp() - 3600,
            "notified_status": "due",
        }
        self.manager.details["a"] = {"plant_name": "Тест"}
        self.entity = SimpleNamespace(
            entity_id="sensor.test", hass=self.hass, async_write_ha_state=Mock()
        )
        self.manager.entities["a"] = self.entity

    async def _register_callbacks(self) -> dict:
        """Capture registered callbacks while keeping unrelated HA IO mocked."""
        self.manager.load = AsyncMock()
        with (
            patch(f"{MODULE}.CareManager", return_value=self.manager),
            patch(f"{MODULE}.discovery.async_load_platform", new=AsyncMock()),
            patch(f"{MODULE}.async_track_time_interval"),
            patch(f"{MODULE}._register_photo_service"),
        ):
            await async_setup(self.hass, {DOMAIN: self.manager.config})
        self.source_changed = next(
            call.args[1]
            for call in self.hass.bus.async_listen.call_args_list
            if call.args[0] == EVENT_STATE_CHANGED
        )
        return {
            call.args[1]: call.args[2]
            for call in self.hass.services.async_register.call_args_list
        }

    def test_failed_sensor_clear_retries_until_acknowledged(self) -> None:
        record = self.manager.records["a"]
        record["status"] = "sensor_error"
        with patch(f"{MODULE}.dt_util.utcnow", return_value=self.now) as clock:
            self.manager.emit_transition("a", "due")
            self.assertTrue(record["clear_pending"])
            self.assertFalse(self.hass.bus.async_fire.call_args.args[1]["notify"])
            self.assertEqual(record["delivery_attempts"], 1)

            self.manager.records["a"] = evaluate(
                record,
                {"has_sensor": True, "moisture": None},
                self.now.timestamp() + 59,
            )
            self.manager._publish_existing(
                "a", record, self.now.timestamp() + 59, write_state=False
            )
            self.assertEqual(self.hass.bus.async_fire.call_count, 1)

            clock.return_value = self.now + timedelta(seconds=60)
            self.manager._publish_existing(
                "a", record, clock.return_value.timestamp(), write_state=False
            )
            current = self.manager.records["a"]
            self.assertEqual(self.hass.bus.async_fire.call_count, 2)
            self.assertTrue(current["clear_pending"])
            self.assertFalse(self.hass.bus.async_fire.call_args.args[1]["notify"])
            self.assertEqual(current["delivery_attempts"], 2)

            acknowledge_delivery(current, "sensor_error")
            self.assertNotIn("clear_pending", current)
            self.assertIsNone(current["notified_status"])
            self.manager._publish_existing(
                "a", dict(current), self.now.timestamp() + 3600, write_state=False
            )
            self.assertEqual(self.hass.bus.async_fire.call_count, 2)

    def test_clear_ack_after_recovery_rearms_the_current_warning(self) -> None:
        record = self.manager.records["a"]
        record.update(status="sensor_error", clear_pending=True)
        self.manager.records["a"] = evaluate(
            record,
            {"has_sensor": True, "moisture": 10, "sample_at": self.now.timestamp()},
            self.now.timestamp(),
        )
        current = self.manager.records["a"]
        self.assertEqual(current["status"], "due")
        self.assertTrue(current["clear_pending"])
        self.assertFalse(needs_notification(current))

        acknowledge_delivery(current, "sensor_error")
        self.assertNotIn("clear_pending", current)
        self.assertTrue(needs_notification(current))
        with patch(f"{MODULE}.dt_util.utcnow", return_value=self.now):
            self.manager._publish_existing(
                "a", dict(current), self.now.timestamp(), write_state=False
            )
        event = self.hass.bus.async_fire.call_args.args[1]
        self.assertEqual(event["status"], "due")
        self.assertTrue(event["notify"])

    def test_failed_clear_after_recovery_retries_until_acknowledged(self) -> None:
        fault = self.manager.records["a"]
        fault["status"] = "sensor_error"
        with patch(f"{MODULE}.dt_util.utcnow", return_value=self.now) as clock:
            self.manager.emit_transition("a", "due")
            self.assertTrue(fault["clear_pending"])

            # The first clear failed; watering and a valid sample recover to ok.
            clock.return_value = self.now + timedelta(seconds=1)
            recovered_at = clock.return_value.timestamp()
            current = evaluate(
                fault,
                {
                    "has_sensor": True,
                    "moisture": 80,
                    "sample_at": recovered_at,
                    "watered_at": recovered_at,
                },
                recovered_at,
            )
            self.manager.records["a"] = current
            self.assertEqual(current["status"], "ok")
            self.manager._publish_existing("a", fault, recovered_at, write_state=False)
            self.assertTrue(current["clear_pending"])
            self.assertEqual(current["delivery_attempts"], 2)

            # The recovery clear also failed; unchanged ok still retries it.
            self.manager._publish_existing(
                "a", dict(current), recovered_at + 299, write_state=False
            )
            self.assertEqual(self.hass.bus.async_fire.call_count, 2)
            clock.return_value += timedelta(seconds=300)
            self.manager._publish_existing(
                "a", dict(current), clock.return_value.timestamp(), write_state=False
            )
            self.assertEqual(self.hass.bus.async_fire.call_count, 3)
            event = self.hass.bus.async_fire.call_args.args[1]
            self.assertEqual(event["status"], "ok")
            self.assertEqual(event["previous"], "ok")
            self.assertTrue(event["clear"])
            self.assertFalse(event["notify"])
            self.assertEqual(current["delivery_attempts"], 3)

            acknowledge_delivery(current, "sensor_error")
            self.assertNotIn("clear_pending", current)
            self.assertEqual(current["delivery_attempts"], 0)
            self.manager._publish_existing(
                "a", dict(current), recovered_at + 3600, write_state=False
            )
            self.assertEqual(self.hass.bus.async_fire.call_count, 3)

            for status in ("ok", "checking", "snoozed"):
                with self.subTest(direct_recovery=status):
                    self.manager.records["a"] = {
                        "status": status,
                        "notified_status": None,
                    }
                    self.manager.emit_transition("a", "due")
                    self.assertTrue(self.manager.records["a"]["clear_pending"])
                    event = self.hass.bus.async_fire.call_args.args[1]
                    self.assertTrue(event["clear"])
                    self.assertFalse(event["notify"])

    async def test_explicit_reports_wait_for_a_coalesced_watering_refresh(self) -> None:
        callbacks = await self._register_callbacks()
        self.hass.data["adaptive_plant"] = {
            key: SimpleNamespace(moisture_sensor=f"sensor.{key}") for key in ("a", "b")
        }
        entered, release = asyncio.Event(), asyncio.Event()
        watered = False
        passes = []

        async def load_history(entry_id, _plant, _now) -> None:
            if entry_id == "b" and not entered.is_set():
                entered.set()
                await release.wait()

        def refresh_plant(entry_id, _plant, _now) -> bool:
            passes.append(entry_id)
            if entry_id == "a":
                self.manager.records["a"]["status"] = "ok" if watered else "due"
            return True

        async def read_report() -> str:
            await callbacks["refresh"](SimpleNamespace(context=Context()))
            return self.manager.records["a"]["status"]

        self.manager._load_history = AsyncMock(side_effect=load_history)
        self.manager._refresh_plant = Mock(side_effect=refresh_plant)
        owner = asyncio.create_task(self.manager.async_refresh())
        reports = []
        try:
            await asyncio.wait_for(entered.wait(), timeout=TEST_TIMEOUT)
            self.assertEqual(self.manager.records["a"]["status"], "due")
            watered = True
            reports = [asyncio.create_task(read_report()) for _ in range(2)]
            await asyncio.sleep(0)
            self.assertTrue(all(not task.done() for task in reports))
        finally:
            release.set()
            results = await asyncio.wait_for(
                asyncio.gather(owner, *reports), timeout=TEST_TIMEOUT
            )
        self.assertEqual(results[1:], ["ok", "ok"])
        self.assertEqual(passes, ["a", "b", "a", "b"])
        self.assertFalse(self.manager.busy)

    async def test_source_event_during_history_wait_requests_another_pass(self) -> None:
        await self._register_callbacks()
        self.manager.watched = {"sensor.raw"}
        self.hass.data["adaptive_plant"] = {
            "a": SimpleNamespace(moisture_sensor="sensor.smooth")
        }
        entered, release = asyncio.Event(), asyncio.Event()

        async def load_history(_entry_id, _plant, _now) -> None:
            if not entered.is_set():
                entered.set()
                await release.wait()

        self.manager._load_history = AsyncMock(side_effect=load_history)
        self.manager._refresh_plant = Mock(return_value=False)
        owner = asyncio.create_task(self.manager.async_refresh())
        try:
            await asyncio.wait_for(entered.wait(), timeout=TEST_TIMEOUT)
            await asyncio.wait_for(
                self.source_changed(SimpleNamespace(data={"entity_id": "sensor.raw"})),
                timeout=TEST_TIMEOUT,
            )
            self.assertTrue(self.manager._refresh_pending)
        finally:
            release.set()
            await asyncio.wait_for(owner, timeout=TEST_TIMEOUT)
        self.assertEqual(self.manager._refresh_plant.call_count, 2)

    async def test_reentrant_refresh_coalesces_without_waiting_on_itself(self) -> None:
        calls = 0

        async def refresh_once() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                await self.manager.async_refresh(wait=True)

        self.manager._refresh_once = AsyncMock(side_effect=refresh_once)
        await asyncio.wait_for(self.manager.async_refresh(), timeout=TEST_TIMEOUT)
        self.assertEqual(calls, 2)
        self.assertFalse(self.manager.busy)

    async def test_voice_events_update_metadata_without_recomputing_forecast(
        self,
    ) -> None:
        await self._register_callbacks()
        target = self.manager.voice_target
        toggle = "input_boolean.plant_care_voice_enabled"
        states = {
            toggle: SimpleNamespace(state="on"),
            target: SimpleNamespace(
                entity_id=target,
                state="idle",
                attributes={"supported_features": 512, "friendly_name": "Тест"},
            ),
        }
        self.hass.states.get = states.get
        record = self.manager.records["a"]
        record["forecast_due_at"] = self.now.timestamp() + 86400
        record["adc_samples"] = [[self.now.timestamp(), 100]]
        self.manager.watched = {"sensor.raw"}
        self.manager.async_refresh = AsyncMock()
        self.manager._load_history = AsyncMock()
        with (
            patch(f"{MODULE}.dt_util.utcnow", return_value=self.now),
            patch(f"{MODULE}.analyze") as analyze,
        ):
            self.manager._update_voice_estimates(record, self.now.timestamp())
            forecast = record["forecast_due_at"]
            states[target].state = "unavailable"
            await self.source_changed(SimpleNamespace(data={"entity_id": target}))
            self.assertFalse(record["voice_target_available"])
            self.assertEqual(record["voice_unavailable_reason"], "Колонка недоступна")

            selected = "notify.selected_speaker"
            self.manager.voice_entry = SimpleNamespace(
                data={}, options={"voice_target": selected}
            )
            states[selected] = SimpleNamespace(
                entity_id=selected,
                state="unknown",
                attributes={"friendly_name": "Выбранная колонка"},
            )
            await self.source_changed(SimpleNamespace(data={"entity_id": selected}))
            self.assertEqual(record["voice_target"], selected)
            self.assertEqual(record["voice_target_name"], "Выбранная колонка")
            self.assertTrue(record["voice_target_available"])
            self.assertIsNotNone(record["voice_next_at"])

            states[toggle].state = "off"
            await self.source_changed(SimpleNamespace(data={"entity_id": toggle}))
            self.assertIsNone(record["voice_next_at"])
            self.assertEqual(
                record["voice_unavailable_reason"], "Голосовые напоминания выключены"
            )
            writes = self.entity.async_write_ha_state.call_count
            await self.source_changed(SimpleNamespace(data={"entity_id": toggle}))
            self.assertEqual(self.entity.async_write_ha_state.call_count, writes)
            self.assertEqual(record["forecast_due_at"], forecast)
            self.assertEqual(record["adc_samples"], [[self.now.timestamp(), 100]])
            analyze.assert_not_called()
            self.manager._load_history.assert_not_awaited()
            self.manager.async_refresh.assert_not_awaited()

            await self.source_changed(SimpleNamespace(data={"entity_id": "sensor.raw"}))
            self.manager.async_refresh.assert_awaited_once_with()
