"""Render the deployed Alice package with native HA templates and script actions."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import yaml
from homeassistant import loader
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import frame
from homeassistant.helpers.script import Script
from homeassistant.helpers.template import Template

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = yaml.safe_load((ROOT / "packages/plant_care_yandex.yaml").read_text())
SENSOR = PACKAGE["template"][0]["sensor"][0]
SEQUENCE = PACKAGE["script"]["plant_care_status"]["sequence"]
CARE = "sensor.strelitsiia_poliv"
RAW = "sensor.strelitzia_soil_adc"
DRY = "input_number.strelitzia_soil_dry_adc"
WET = "input_number.strelitzia_soil_wet_adc"
CALIBRATED = "input_boolean.strelitzia_soil_dry_calibrated"


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

    async def report(self, variables=None) -> dict[str, Any]:
        script = Script(self.hass, cv.SCRIPT_SCHEMA(SEQUENCE), "Plant status", "script")
        result = await script.async_run(variables or {"speak": False}, Context())
        self.assertIsNotNone(result)
        return result.service_response

    async def test_sensor_and_report_reject_the_same_invalid_data(self) -> None:
        now = datetime.now(UTC)
        cases = [
            ("valid", {}, None, None, True),
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
        # A nonempty variables mapping leaves speak genuinely omitted.
        report = await self.report({"test_context": True})
        self.assertEqual(report["moisture"], 22)
        self.assertIn("Сухость подтверждена", report["message"])
        self.announce.assert_awaited_once()
        call = self.announce.call_args.args[0]
        self.assertEqual(call.data, {"message": report["message"]})
