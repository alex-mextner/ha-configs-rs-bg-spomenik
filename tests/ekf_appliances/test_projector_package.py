"""Projector power scripts execute against real HA services without appliance I/O."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import yaml
from homeassistant import loader
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import frame
from homeassistant.helpers.script import Script
from homeassistant.util import dt as dt_util

PACKAGE = Path(__file__).resolve().parents[2] / "packages/projector.yaml"


class ProjectorPackageTests(unittest.IsolatedAsyncioTestCase):
    """Verify actual HA power scripts without appliance writes."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.config = yaml.safe_load(PACKAGE.read_text())
        self.hass.states.async_set("binary_sensor.projector_hdmi_connected", "off")
        self.hass.states.async_set("input_boolean.projector_power", "on")
        self.hass.states.async_set("input_text.tv_source", "projector")
        self.presses = []
        self.helper_writes = []
        self.lifecycle = []
        self.feedback_after_press = None
        self.refreshes = 0
        self.feedback_after_refresh = None

        async def press(call) -> None:
            self.presses.append(dict(call.data))
            if self.feedback_after_press:
                self.feedback_after_press(len(self.presses))

        async def helper(call) -> None:
            self.helper_writes.append(call.service)
            self.hass.states.async_set(
                "input_boolean.projector_power",
                "on" if call.service == "turn_on" else "off",
            )

        async def lifecycle(call) -> None:
            self.lifecycle.append((call.domain, call.service, dict(call.data)))

        async def refresh(_call) -> None:
            self.refreshes += 1
            if self.feedback_after_refresh:
                self.feedback_after_refresh(self.refreshes)

        self.hass.services.async_register("homeassistant", "update_entity", refresh)
        self.hass.services.async_register("button", "press", press)
        for service in ("turn_on", "turn_off"):
            self.hass.services.async_register("input_boolean", service, helper)
        for service in ("host_projector_on", "host_projector_off"):
            self.hass.services.async_register("rest_command", service, lifecycle)
        self.hass.services.async_register("input_text", "set_value", lifecycle)
        self.hass.services.async_register("script", "source_to_chromecast", lifecycle)
        for service in ("projector_turn_on", "projector_turn_off"):
            self.hass.services.async_register("script", service, lifecycle)
        self.hass.services.async_register("media_player", "toggle", lifecycle)

    async def asyncTearDown(self) -> None:
        await self.hass.async_block_till_done()
        self.temp.cleanup()

    async def run_sequence(self, sequence) -> None:
        script = Script(
            self.hass, cv.SCRIPT_SCHEMA(sequence), "Projector test", "script"
        )
        await script.async_run(
            {"trigger": {"id": "off", "platform": "state"}}, context=Context()
        )

    def stable_on(self) -> None:
        self.hass.states.async_set("binary_sensor.projector_hdmi_connected", "on")
        self.hass.states.get("binary_sensor.projector_hdmi_connected").last_changed = (
            dt_util.utcnow() - timedelta(seconds=31)
        )

    async def test_on_script_sends_one_power_without_claiming_helper_state(
        self,
    ) -> None:
        self.feedback_after_press = lambda _count: self.hass.states.async_set(
            "binary_sensor.projector_hdmi_connected", "on"
        )
        await self.run_sequence(self.config["script"]["projector_turn_on"]["sequence"])
        self.assertEqual(len(self.presses), 1)
        self.assertEqual(self.helper_writes, [])

    async def test_on_script_does_not_toggle_an_observed_on_projector(self) -> None:
        self.hass.states.async_set("binary_sensor.projector_hdmi_connected", "on")
        await self.run_sequence(self.config["script"]["projector_turn_on"]["sequence"])
        self.assertEqual(self.presses, [])

    async def test_off_script_confirms_only_while_feedback_remains_on(self) -> None:
        self.stable_on()
        self.feedback_after_press = lambda count: (
            self.hass.states.async_set("binary_sensor.projector_hdmi_connected", "off")
            if count == 2
            else None
        )
        await self.run_sequence(self.config["script"]["projector_turn_off"]["sequence"])
        self.assertEqual(len(self.presses), 2)
        self.assertEqual(self.helper_writes, [])

    async def test_off_script_skips_second_power_after_observed_disconnect(
        self,
    ) -> None:
        self.stable_on()
        self.feedback_after_press = lambda _count: self.hass.states.async_set(
            "binary_sensor.projector_hdmi_connected", "off"
        )
        await self.run_sequence(self.config["script"]["projector_turn_off"]["sequence"])
        self.assertEqual(len(self.presses), 1)
        self.assertEqual(self.helper_writes, [])

    async def test_off_script_does_not_toggle_an_observed_off_projector(self) -> None:
        await self.run_sequence(self.config["script"]["projector_turn_off"]["sequence"])
        self.assertEqual(self.presses, [])

    async def test_toggle_dispatches_to_observed_media_state_without_helper_branch(
        self,
    ) -> None:
        await self.run_sequence(self.config["script"]["projector_toggle"]["sequence"])
        self.assertEqual(
            self.lifecycle,
            [("media_player", "toggle", {"entity_id": ["media_player.projector"]})],
        )
        self.assertEqual(self.presses, [])

    async def test_observed_power_only_mirrors_feedback_without_source_or_lifecycle(
        self,
    ) -> None:
        for source in ("pc", "chromecast", "projector"):
            self.hass.states.async_set("input_text.tv_source", source)
            for power in ("on", "off"):
                self.helper_writes.clear()
                self.hass.states.async_set(
                    "binary_sensor.projector_hdmi_connected", power
                )
                await self.run_sequence(self.config["automation"][0]["action"])
                self.assertEqual(self.helper_writes, ["turn_" + power])
                self.assertEqual(self.lifecycle, [])
                self.assertEqual(self.presses, [])

    async def test_power_off_waits_during_warmup_without_sending(self) -> None:
        self.hass.states.async_set("binary_sensor.projector_hdmi_connected", "on")
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.05):
                await self.run_sequence(
                    self.config["script"]["projector_turn_off"]["sequence"]
                )
        self.assertEqual(self.presses, [])

    async def test_second_power_is_blocked_by_fresh_unknown_feedback(self) -> None:
        self.stable_on()
        self.feedback_after_refresh = lambda count: (
            self.hass.states.async_set(
                "binary_sensor.projector_hdmi_connected", "unknown"
            )
            if count == 2
            else None
        )
        await self.run_sequence(self.config["script"]["projector_turn_off"]["sequence"])
        self.assertEqual(self.refreshes, 2)
        self.assertEqual(len(self.presses), 1)
