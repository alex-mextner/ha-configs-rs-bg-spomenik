"""Native HA regressions for missing and invalid probe calibration states."""

import tempfile
import unittest
from pathlib import Path

import yaml
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame
from homeassistant.helpers.template import Template

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = yaml.safe_load((ROOT / "packages/soil_moisture.yaml").read_text())
INDEX = next(
    sensor
    for block in PACKAGE["template"]
    for sensor in block.get("sensor", [])
    if sensor.get("unique_id") == "strelitzia_soil_moisture_index"
)
DRY = "input_number.strelitzia_soil_dry_adc"
WET = "input_number.strelitzia_soil_wet_adc"
RAW = "sensor.strelitzia_soil_adc"


class SoilCalibrationTests(unittest.IsolatedAsyncioTestCase):
    """Unknown calibration must not silently become a usable zero endpoint."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.hass.states.async_set("input_boolean.strelitzia_soil_dry_calibrated", "on")
        self.hass.states.async_set(RAW, "40")

    async def asyncTearDown(self) -> None:
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def test_missing_unknown_unavailable_and_nonfinite_endpoints_are_unavailable(
        self,
    ) -> None:
        for endpoint in (DRY, WET):
            for value in (None, "unknown", "unavailable", "nan", "inf"):
                with self.subTest(endpoint=endpoint, value=value):
                    self.hass.states.async_set(DRY, "0")
                    self.hass.states.async_set(WET, "200")
                    if value is None:
                        self.hass.states.async_remove(endpoint)
                    else:
                        self.hass.states.async_set(endpoint, value)
                    await self.hass.async_block_till_done()
                    self.assertIs(
                        Template(INDEX["availability"], self.hass).async_render(),
                        expr2=False,
                    )
                    self.assertIsNone(
                        Template(INDEX["state"], self.hass).async_render()
                    )

    async def test_explicit_numeric_zero_is_a_valid_endpoint(self) -> None:
        self.hass.states.async_set(DRY, "0")
        self.hass.states.async_set(WET, "200")
        await self.hass.async_block_till_done()
        self.assertIs(
            Template(INDEX["availability"], self.hass).async_render(), expr2=True
        )
        self.assertEqual(Template(INDEX["state"], self.hass).async_render(), 20.0)

    async def test_inverted_equal_and_unavailable_raw_inputs_do_not_publish_an_index(
        self,
    ) -> None:
        for dry, wet, raw in [
            (200, 100, "40"),
            (100, 100, "40"),
            (0, 200, "unavailable"),
        ]:
            with self.subTest(dry=dry, wet=wet, raw=raw):
                self.hass.states.async_set(DRY, str(dry))
                self.hass.states.async_set(WET, str(wet))
                self.hass.states.async_set(RAW, raw)
                await self.hass.async_block_till_done()
                self.assertIs(
                    Template(INDEX["availability"], self.hass).async_render(),
                    expr2=False,
                )
                self.assertIsNone(Template(INDEX["state"], self.hass).async_render())
