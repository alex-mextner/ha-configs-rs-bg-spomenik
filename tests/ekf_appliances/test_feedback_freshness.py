"""Exercise real HA polling entities, including unchanged state serialization."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import State

import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant import loader
from homeassistant.components.command_line.binary_sensor import CommandBinarySensor
from homeassistant.components.command_line.sensor import CommandSensorData
from homeassistant.components.rest.sensor import RestSensor
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame
from homeassistant.helpers.template import Template
from homeassistant.helpers.trigger_template_entity import ValueTemplate
from homeassistant.util import dt as dt_util

from custom_components.ekf_appliances.paired_media import observed_boolean


class FeedbackFreshnessTests(unittest.IsolatedAsyncioTestCase):
    """Verify fresh unchanged observations from native HA polling entities."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)

    async def asyncTearDown(self) -> None:
        await self.hass.async_block_till_done()
        self.temp.cleanup()

    def cache_old_report(self, entity_id) -> tuple[State, Any]:
        """Model a cached REST snapshot while the live HA State continues polling."""
        state = self.hass.states.get(entity_id)
        state.last_reported = dt_util.utcnow() - timedelta(seconds=60)
        cached = state.as_dict()
        self.assertIsNone(observed_boolean(self.hass, entity_id, max_age=12))
        return state, cached["last_reported"]

    async def test_unchanged_hdmi_poll_is_fresh_and_failed_read_is_unknown(
        self,
    ) -> None:
        sensor = CommandBinarySensor(
            CommandSensorData(self.hass, "read-only HDMI fixture", 5),
            {"name": Template("Projector HDMI connected", self.hass)},
            "connected",
            "disconnected",
            None,
            timedelta(seconds=2),
        )
        sensor.hass = self.hass
        sensor.entity_id = "binary_sensor.projector_hdmi_connected"
        # This is the actual HA command_line update path; only the OS command
        # result is replaced. No appliance I/O is performed.
        with patch(
            "homeassistant.components.command_line.sensor.async_check_output_or_log",
            side_effect=["disconnected", "disconnected", None, "connected"],
        ):
            await sensor._async_update()
            state, cached_report = self.cache_old_report(sensor.entity_id)
            await sensor._async_update()
            self.assertIs(self.hass.states.get(sensor.entity_id), state)
            self.assertIs(
                observed_boolean(self.hass, sensor.entity_id, max_age=12), expr2=False
            )
            # HA 2026.9.3 retains the old cached JSON even though the in-memory
            # property used by the pair controller was just updated.
            self.assertEqual(state.as_dict()["last_reported"], cached_report)
            self.assertNotEqual(state.last_reported.isoformat(), cached_report)
            await sensor._async_update()
            self.assertEqual(self.hass.states.get(sensor.entity_id).state, "unknown")
            self.assertIsNone(observed_boolean(self.hass, sensor.entity_id, max_age=12))
            await sensor._async_update()
            self.assertIs(
                observed_boolean(self.hass, sensor.entity_id, max_age=12), expr2=True
            )

    async def test_unchanged_kodi_rest_poll_is_fresh_and_failed_poll_unavailable(
        self,
    ) -> None:
        rest = SimpleNamespace(data='{"state":"off"}')
        rest.data_without_xml = lambda: rest.data
        coordinator = SimpleNamespace(last_update_success=True)
        sensor = RestSensor(
            self.hass,
            coordinator,
            rest,
            {
                "force_update": False,
                "value_template": ValueTemplate.from_template(
                    Template("{{ value_json.state }}", self.hass)
                ),
            },
            {"name": Template("Kodi service state", self.hass)},
        )
        sensor.hass = self.hass
        sensor.entity_id = "sensor.kodi_service_state"
        sensor._handle_coordinator_update()
        state, cached_report = self.cache_old_report(sensor.entity_id)
        sensor._handle_coordinator_update()
        self.assertIs(self.hass.states.get(sensor.entity_id), state)
        self.assertIs(
            observed_boolean(self.hass, sensor.entity_id, max_age=20), expr2=False
        )
        self.assertEqual(state.as_dict()["last_reported"], cached_report)
        self.assertNotEqual(state.last_reported.isoformat(), cached_report)
        rest.data = None  # RestData clears data on a timeout/transport failure.
        coordinator.last_update_success = False
        sensor._handle_coordinator_update()
        self.assertEqual(self.hass.states.get(sensor.entity_id).state, "unavailable")
        self.assertIsNone(observed_boolean(self.hass, sensor.entity_id, max_age=20))


if __name__ == "__main__":
    unittest.main()
