# Copyright (c) 2026 Alex Ultra
# SPDX-License-Identifier: MIT
"""Render the deployed Alice package with native HA templates and script actions."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock, patch

import yaml
from homeassistant import loader
from homeassistant.components import automation, trace
from homeassistant.components.automation import config as automation_config
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import Context, CoreState, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame, restore_state
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import trigger as trigger_helper
from homeassistant.helpers.entity import entity_sources
from homeassistant.helpers.script import Script
from homeassistant.helpers.template import Template
from homeassistant.helpers.trace import script_execution_get, trace_clear

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = yaml.safe_load((ROOT / "packages/plant_care_yandex.yaml").read_text())
SENSOR = PACKAGE["template"][0]["sensor"][0]
SCRIPT_CONFIG = PACKAGE["script"]["plant_care_status"]
SEQUENCE = SCRIPT_CONFIG["sequence"]
CARE = "sensor.strelitsiia_poliv"
RAW = "sensor.strelitzia_soil_adc"
DRY = "input_number.strelitzia_soil_dry_adc"
WET = "input_number.strelitzia_soil_wet_adc"
CALIBRATED = "input_boolean.strelitzia_soil_dry_calibrated"
STATION_A = "media_player.living_room_station"
STATION_B = "media_player.bedroom_renamed"
OTHER_PLAYER = "media_player.yandex_station_impostor"
ORPHAN_PLAYER = "media_player.unlinked_station"
NON_PLAYER = "sensor.station_diagnostic"
TEST_TIMEOUT = 5


class YandexStatusTests(unittest.IsolatedAsyncioTestCase):
    """Exercise freshness, calibration and speech without any external device."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.refresh = AsyncMock()
        self.announce = AsyncMock()
        self.hass.services.async_register("plant_care_alerts", "refresh", self.refresh)
        self.hass.services.async_register(
            "plant_care_alerts", "announce", self.announce
        )
        self.hass.config_entries = ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        await ir.async_load(self.hass, load_empty=True)
        self.station_entry = ConfigEntry(
            domain="yandex_station",
            title="Household Stations",
            data={},
            options={},
            source="user",
            unique_id="test-stations",
            version=1,
            minor_version=1,
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        self.cast_entry = ConfigEntry(
            domain="cast",
            title="Cast player",
            data={},
            options={},
            source="user",
            unique_id="test-cast",
            version=1,
            minor_version=1,
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        # Keep native registration/persistence; only hardware integration startup
        # is external to these routing tests.
        with patch.object(
            self.hass.config_entries, "async_setup", new=AsyncMock(return_value=True)
        ):
            await self.hass.config_entries.async_add(self.station_entry)
            await self.hass.config_entries.async_add(self.cast_entry)
        registry = er.async_get(self.hass)
        for entity_id, platform, config_entry in (
            (STATION_A, "yandex_station", self.station_entry),
            (STATION_B, "yandex_station", self.station_entry),
            (OTHER_PLAYER, "cast", self.cast_entry),
            (NON_PLAYER, "yandex_station", self.station_entry),
            (ORPHAN_PLAYER, "yandex_station", None),
        ):
            domain, object_id = entity_id.split(".", 1)
            entry = registry.async_get_or_create(
                domain,
                platform,
                object_id,
                suggested_object_id=object_id,
                config_entry=config_entry,
            )
            source = {"domain": platform}
            if config_entry is not None:
                source["config_entry"] = config_entry.entry_id
            entity_sources(self.hass)[entry.entity_id] = source
            self.hass.states.async_set(
                entry.entity_id, "idle", {"supported_features": 512}
            )
        self.deliveries: list[tuple[dict[str, Any], Context]] = []

        async def deliver(call) -> None:
            # The external speaker boundary is recorded, never contacted.
            self.deliveries.append((dict(call.data), call.context))

        self.hass.services.async_register("media_player", "play_media", deliver)
        self.script = Script(
            self.hass,
            cv.SCRIPT_SCHEMA(SEQUENCE),
            "Plant status",
            "script",
            script_mode=SCRIPT_CONFIG.get("mode", "single"),
            max_runs=SCRIPT_CONFIG.get("max", 10),
            max_exceeded=SCRIPT_CONFIG.get("max_exceeded", "WARNING").upper(),
        )
        self.reset_states()

    async def asyncTearDown(self) -> None:
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    def reset_states(self) -> None:
        stamp = datetime.now(UTC) - timedelta(seconds=1)
        self.hass.states.async_set(RAW, "390", {"sampled_at": stamp.isoformat()})
        self.hass.states.async_set(DRY, "67")
        self.hass.states.async_set(WET, "221")
        self.hass.states.async_set(CALIBRATED, "on")
        self.hass.states.async_set(
            CARE,
            "ok",
            {
                "moisture": 100.0,
                "reliability_valid": True,
                "last_sample_at": stamp.timestamp(),
                "wet_risk": "prolonged",
                "forecast_status": "saturated",
                "forecast_due_at": None,
                "next_watering": "2099-01-01",
            },
        )

    def set_care(self, state="ok", **attributes: Any) -> None:
        current = self.hass.states.get(CARE)
        self.hass.states.async_set(CARE, state, {**current.attributes, **attributes})

    async def report(
        self, variables=None, context: Context | None = None
    ) -> dict[str, Any]:
        result = await self.script.async_run(
            {"speak": False} if variables is None else variables, context or Context()
        )
        self.assertIsNotNone(result)
        return result.service_response

    def delivered_targets(self) -> list[tuple[str, ...]]:
        targets = []
        for data, _context in self.deliveries:
            value = data.get("entity_id", [])
            targets.append((value,) if isinstance(value, str) else tuple(value))
        return targets

    def assert_no_speech(self) -> None:
        self.assertEqual(self.deliveries, [])
        self.announce.assert_not_called()

    async def test_sensor_and_report_reject_the_same_invalid_data(self) -> None:
        now = datetime.now(UTC)
        cases = [
            ("valid", {}, None, None, True),
            ("unreliable", {"reliability_valid": False}, None, None, False),
            (
                "care stale",
                {"last_sample_at": now.timestamp() - 1201},
                None,
                None,
                False,
            ),
            (
                "care future",
                {"last_sample_at": now.timestamp() + 30},
                None,
                None,
                False,
            ),
            ("care missing", {"last_sample_at": None}, None, None, False),
            ("moisture missing", {"moisture": None}, None, None, False),
            ("moisture low", {"moisture": -1}, None, None, False),
            ("moisture high", {"moisture": 101}, None, None, False),
            ("moisture infinite", {"moisture": "inf"}, None, None, False),
            ("raw stale", {}, now - timedelta(seconds=1201), None, False),
            ("raw future", {}, now + timedelta(seconds=30), None, False),
            ("raw unavailable", {}, None, (RAW, "unavailable"), False),
            ("calibration off", {}, None, (CALIBRATED, "off"), False),
            ("calibration inverted", {}, None, (WET, "67"), False),
            ("calibration unknown", {}, None, (DRY, "unknown"), False),
            ("care unavailable", {}, None, (CARE, "unavailable"), False),
            ("sensor error", {}, None, (CARE, "sensor_error"), False),
        ]
        for name, attributes, raw_stamp, replacement, expected in cases:
            with self.subTest(name=name):
                self.reset_states()
                self.set_care(**attributes)
                if raw_stamp is not None:
                    self.hass.states.async_set(
                        RAW, "390", {"sampled_at": raw_stamp.isoformat()}
                    )
                if replacement is not None:
                    self.hass.states.async_set(*replacement)
                available = Template(SENSOR["availability"], self.hass).async_render()
                report = await self.report()
                self.assertIs(available, expected)
                self.assertIs(report["available"], expected)
                self.assertEqual(report["moisture"], 100.0 if expected else None)
                self.assertLessEqual(len(report["message"]), 100)
        self.announce.assert_not_called()

    async def test_current_high_readings_do_not_invent_a_watering_date(self) -> None:
        report = await self.report()
        self.assertEqual(
            report["message"],
            "Стрелиция: индекс влажности 100%. Показания долго высокие. Проверь грунт перед поливом.",
        )
        self.assertEqual(report["forecast_status"], "saturated")
        self.assertNotIn("2099", report["message"])
        self.refresh.assert_awaited_once()
        self.announce.assert_not_called()

    async def test_all_care_branches_remain_short_and_forecast_is_guarded(self) -> None:
        for state, fragment in [
            ("due", "Сухость подтверждена"),
            ("overdue", "Полив просрочен"),
            ("checking", "Проверяю показания"),
            ("snoozed", "Напоминание отложено"),
            ("ok", "Точного прогноза полива пока нет"),
        ]:
            with self.subTest(state=state):
                self.reset_states()
                self.set_care(state, wet_risk="normal")
                report = await self.report()
                self.assertIn(fragment, report["message"])
                self.assertLessEqual(len(report["message"]), 100)
        for offset, expected in [(3600, True), (-3600, False)]:
            self.set_care(
                wet_risk="normal",
                forecast_status="available",
                forecast_due_at=(
                    datetime.now(UTC) + timedelta(seconds=offset)
                ).isoformat(),
            )
            report = await self.report()
            self.assertEqual("Полив ожидается около" in report["message"], expected)
            self.assertLessEqual(len(report["message"]), 100)

    async def test_refresh_precedes_report_and_default_speaks_once(self) -> None:
        async def update(_call) -> None:
            self.set_care("due", moisture=22, wet_risk="normal")

        self.refresh.side_effect = update
        # Omit speak to exercise the real default, with an explicit requester.
        report = await self.report({"reply_target": STATION_B})
        self.assertEqual(report["moisture"], 22)
        self.assertIn("Сухость подтверждена", report["message"])
        self.assertEqual(self.delivered_targets(), [(STATION_B,)])
        self.assertEqual(self.deliveries[0][0]["media_content_id"], report["message"])
        self.announce.assert_not_called()

    async def test_approximate_user_report_never_speaks_an_invented_time(self) -> None:
        self.set_care(
            last_watering_at="2026-11-17",
            last_watering_source="user_report",
            last_watering_precision="approximate_day",
        )
        report = await self.report()
        self.assertIn("примерно 17 ноября", report["message"])
        self.assertNotIn("00:00", report["message"])
        self.assertEqual(report["last_watering_precision"], "approximate_day")
        self.assert_no_speech()

    async def test_automatic_watering_keeps_the_estimate_interval(self) -> None:
        start = "2026-11-18T10:00:00+00:00"
        end = "2026-11-18T10:05:00+00:00"
        self.set_care(
            last_watering_at=end,
            last_watering_source="auto",
            last_watering_precision="interval",
            last_watering_window_start=start,
            last_watering_window_end=end,
        )
        report = await self.report()
        self.assertIn("Автоматически определён вероятный полив", report["message"])
        self.assertEqual(report["last_watering_window_start"], start)
        self.assertEqual(report["last_watering_window_end"], end)
        self.assert_no_speech()

    async def test_requested_station_receives_default_speech(self) -> None:
        """A renamed requesting Station must never use the reminder speaker."""
        for target in (STATION_A, STATION_B):
            with self.subTest(target=target):
                self.deliveries.clear()
                context = Context(user_id="requesting-user")
                report = await self.report({"reply_target": target}, context)
                self.assertEqual(self.delivered_targets(), [(target,)])
                data, delivered_context = self.deliveries[0]
                self.assertEqual(data["media_content_type"], "text")
                self.assertEqual(data["media_content_id"], report["message"])
                self.assertIs(delivered_context, context)
                self.announce.assert_not_called()

    async def test_missing_or_invalid_requester_never_falls_back(self) -> None:
        """Missing origins and lookalike entity names cannot select a speaker."""
        cases = (
            ("omitted", {}, "idle"),
            ("empty", {"reply_target": ""}, "idle"),
            ("null", {"reply_target": None}, "idle"),
            ("absent entity", {"reply_target": "media_player.absent"}, "idle"),
            ("wrong integration", {"reply_target": OTHER_PLAYER}, "idle"),
            ("missing config entry", {"reply_target": ORPHAN_PLAYER}, "idle"),
            ("wrong domain", {"reply_target": NON_PLAYER}, "idle"),
            ("unavailable", {"reply_target": STATION_B}, "unavailable"),
            ("unknown", {"reply_target": STATION_B}, "unknown"),
        )
        for name, variables, state in cases:
            with self.subTest(name=name):
                self.hass.states.async_set(
                    STATION_B, state, {"supported_features": 512}
                )
                self.deliveries.clear()
                self.announce.reset_mock()
                self.refresh.reset_mock()
                # A native top-level error stop returns an aborted run, not an
                # exception. It must reject the call before refresh or delivery.
                trace_clear()
                result = await self.script.async_run(variables, Context())
                self.assertIsNotNone(result)
                self.assertIsNone(result.service_response)
                self.assertEqual(script_execution_get(), "aborted")
                self.refresh.assert_not_awaited()
                self.assert_no_speech()

    async def test_entry_title_cannot_impersonate_the_station_integration(self) -> None:
        """A Cast entry named yandex_station cannot replace the actual domain."""
        self.hass.config_entries.async_update_entry(
            self.cast_entry, title="yandex_station"
        )
        for target in (STATION_A, STATION_B):
            with self.subTest(target=target):
                self.deliveries.clear()
                report = await self.report({"reply_target": target})
                self.assertEqual(self.delivered_targets(), [(target,)])
                self.assertEqual(
                    self.deliveries[0][0]["media_content_id"], report["message"]
                )
                self.announce.assert_not_called()

        self.deliveries.clear()
        self.refresh.reset_mock()
        trace_clear()
        result = await self.script.async_run({"reply_target": OTHER_PLAYER}, Context())
        self.assertIsNotNone(result)
        self.assertIsNone(result.service_response)
        self.assertEqual(script_execution_get(), "aborted")
        self.refresh.assert_not_awaited()
        self.assert_no_speech()

    async def test_silent_report_requires_no_valid_speaker(self) -> None:
        """Diagnostics return plant data even with no usable reply destination."""
        for variables in (
            {"speak": False},
            {"speak": False, "reply_target": OTHER_PLAYER},
        ):
            with self.subTest(variables=variables):
                report = await self.report(variables)
                self.assertEqual(report["moisture"], 100.0)
                self.assertIn("Показания долго высокие", report["message"])
                self.assert_no_speech()

    async def test_overlapping_requests_keep_their_own_station_and_context(
        self,
    ) -> None:
        """A slow first refresh cannot overwrite or suppress another Station."""
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        first_context = Context(user_id="first-station-user")
        second_context = Context(user_id="second-station-user")

        async def refresh(call) -> None:
            if call.context is first_context:
                first_entered.set()
                await release_first.wait()

        self.refresh.side_effect = refresh
        first = asyncio.create_task(
            self.report({"reply_target": STATION_A}, first_context)
        )
        try:
            await asyncio.wait_for(first_entered.wait(), timeout=TEST_TIMEOUT)
            second_report = await asyncio.wait_for(
                self.report({"reply_target": STATION_B}, second_context),
                timeout=TEST_TIMEOUT,
            )
            self.assertEqual(self.delivered_targets(), [(STATION_B,)])
            self.assertEqual(
                self.deliveries[0][0]["media_content_id"], second_report["message"]
            )
            self.assertIs(self.deliveries[0][1], second_context)
        finally:
            release_first.set()
            first_report = await asyncio.wait_for(first, timeout=TEST_TIMEOUT)

        self.assertEqual(self.delivered_targets(), [(STATION_B,), (STATION_A,)])
        self.assertEqual(
            self.deliveries[1][0]["media_content_id"], first_report["message"]
        )
        self.assertIs(self.deliveries[1][1], first_context)
        self.announce.assert_not_called()

    async def test_station_event_marker_filters_commands_and_tts_echoes(self) -> None:
        """Only the dedicated request event may invoke the plant report."""
        self.hass.config_entries.async_update_entry(
            self.cast_entry, title="yandex_station"
        )
        self.hass.config.skip_pip = True
        await restore_state.async_load(self.hass, load_empty=True)
        self.assertTrue(await trace.async_setup(self.hass, {}))
        requests: list[dict[str, Any]] = []
        reports: list[dict[str, Any]] = []

        async def run_report(call) -> None:
            requests.append(dict(call.data))
            reports.append(await self.report(dict(call.data), call.context))

        self.hass.services.async_register("script", "plant_care_status", run_report)
        await trigger_helper.async_setup(self.hass)
        config = await automation_config.async_validate_config(
            self.hass, {"automation": deepcopy(PACKAGE.get("automation", []))}
        )
        # Load HA's real automation engine without an unrelated HTTP listener.
        self.hass.state = CoreState.running
        self.assertTrue(await automation.async_setup(self.hass, config))
        await self.hass.async_block_till_done()

        async def emit(instance: str, value: str) -> None:
            self.hass.bus.async_fire(
                "yandex_speaker",
                {
                    "entity_id": STATION_B,
                    "instance": instance,
                    "value": value,
                },
                context=Context(),
            )
            await self.hass.async_block_till_done()

        marker = "ничего не делай...,,::..,,..."
        await emit("text_action", marker)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["reply_target"], STATION_B)
        self.assertTrue(requests[0]["speak"])
        self.assertEqual(self.delivered_targets(), [(STATION_B,)])
        self.assertEqual(
            self.deliveries[0][0]["media_content_id"], reports[0]["message"]
        )

        for instance, value in (
            ("text_action", "включи свет"),
            ("tts", marker),
            ("text_action", reports[0]["message"]),
        ):
            with self.subTest(instance=instance, value=value):
                await emit(instance, value)
                self.assertEqual(len(requests), 1)
                self.assertEqual(self.delivered_targets(), [(STATION_B,)])
        self.announce.assert_not_called()
