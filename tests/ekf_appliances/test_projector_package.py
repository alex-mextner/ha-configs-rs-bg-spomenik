"""Projector power scripts execute against real HA services without appliance I/O."""
import asyncio
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest

import yaml
from homeassistant.core import HomeAssistant, Context
from homeassistant import loader
from homeassistant.helpers import config_validation as cv, frame
from homeassistant.helpers.script import Script
from homeassistant.util import dt as dt_util

PACKAGE = Path(__file__).resolve().parents[2] / "packages/projector.yaml"


class ProjectorPackageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
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

        async def press(call):
            self.presses.append(dict(call.data))
            if self.feedback_after_press:
                self.feedback_after_press(len(self.presses))

        async def helper(call):
            self.helper_writes.append(call.service)
            self.hass.states.async_set(
                "input_boolean.projector_power", "on" if call.service == "turn_on" else "off"
            )

        async def lifecycle(call):
            self.lifecycle.append((call.domain, call.service, dict(call.data)))

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

    async def asyncTearDown(self):
        await self.hass.async_block_till_done()
        self.temp.cleanup()

    async def run_sequence(self, sequence):
        script = Script(self.hass, cv.SCRIPT_SCHEMA(sequence), "Projector test", "script")
        await script.async_run({"trigger": {"id": "off", "platform": "state"}}, context=Context())

    def stable_on(self):
        self.hass.states.async_set("binary_sensor.projector_hdmi_connected", "on")
        self.hass.states.get("binary_sensor.projector_hdmi_connected").last_changed = (
            dt_util.utcnow() - timedelta(seconds=31)
        )

    async def test_on_script_sends_one_power_without_claiming_helper_state(self):
        self.feedback_after_press = lambda count: self.hass.states.async_set(
            "binary_sensor.projector_hdmi_connected", "on"
        )
        await self.run_sequence(self.config["script"]["projector_turn_on"]["sequence"])
        self.assertEqual(len(self.presses), 1)
        self.assertEqual(self.helper_writes, [])

    async def test_on_script_does_not_toggle_an_observed_on_projector(self):
        self.hass.states.async_set("binary_sensor.projector_hdmi_connected", "on")
        await self.run_sequence(self.config["script"]["projector_turn_on"]["sequence"])
        self.assertEqual(self.presses, [])

    async def test_off_script_confirms_only_while_feedback_remains_on(self):
        self.stable_on()
        self.feedback_after_press = lambda count: self.hass.states.async_set(
            "binary_sensor.projector_hdmi_connected", "off"
        ) if count == 2 else None
        await self.run_sequence(self.config["script"]["projector_turn_off"]["sequence"])
        self.assertEqual(len(self.presses), 2)
        self.assertEqual(self.helper_writes, [])

    async def test_off_script_skips_second_power_after_observed_disconnect(self):
        self.stable_on()
        self.feedback_after_press = lambda count: self.hass.states.async_set(
            "binary_sensor.projector_hdmi_connected", "off"
        )
        await self.run_sequence(self.config["script"]["projector_turn_off"]["sequence"])
        self.assertEqual(len(self.presses), 1)
        self.assertEqual(self.helper_writes, [])

    async def test_off_script_does_not_toggle_an_observed_off_projector(self):
        await self.run_sequence(self.config["script"]["projector_turn_off"]["sequence"])
        self.assertEqual(self.presses, [])

    async def test_toggle_dispatches_to_observed_media_state_without_helper_branch(self):
        await self.run_sequence(self.config["script"]["projector_toggle"]["sequence"])
        self.assertEqual(self.lifecycle, [
            ("media_player", "toggle", {"entity_id": ["media_player.projector"]})
        ])
        self.assertEqual(self.presses, [])

    async def test_observed_off_reconciles_stale_helper_and_stops_projector_session(self):
        await self.run_sequence(self.config["automation"][0]["action"])
        self.assertEqual(self.helper_writes, ["turn_off"])
        self.assertEqual([item[:2] for item in self.lifecycle], [
            ("rest_command", "host_projector_off"), ("input_text", "set_value")
        ])
        self.assertEqual(self.presses, [])

    async def test_observed_off_does_not_stop_kodi_owned_by_pc_source(self):
        self.hass.states.async_set("input_text.tv_source", "pc")
        await self.run_sequence(self.config["automation"][0]["action"])
        self.assertEqual(self.helper_writes, ["turn_off"])
        self.assertEqual(self.lifecycle, [])
        self.assertEqual(self.presses, [])


    async def test_unknown_recovery_does_not_restart_kodi(self):
        self.hass.states.async_set("binary_sensor.projector_hdmi_connected", "on")
        self.hass.states.async_set("input_boolean.projector_power", "on")
        await self.run_sequence(self.config["automation"][0]["action"])
        self.assertEqual(self.helper_writes, [])
        self.assertEqual(self.lifecycle, [])

    async def test_new_observed_on_starts_kodi_once(self):
        self.hass.states.async_set("binary_sensor.projector_hdmi_connected", "on")
        self.hass.states.async_set("input_boolean.projector_power", "off")
        await self.run_sequence(self.config["automation"][0]["action"])
        await self.run_sequence(self.config["automation"][0]["action"])
        self.assertEqual(self.helper_writes, ["turn_on"])
        self.assertEqual([item[:2] for item in self.lifecycle], [
            ("rest_command", "host_projector_on"), ("input_text", "set_value")
        ])

    async def test_power_off_waits_during_warmup_without_sending(self):
        self.hass.states.async_set("binary_sensor.projector_hdmi_connected", "on")
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.05):
                await self.run_sequence(self.config["script"]["projector_turn_off"]["sequence"])
        self.assertEqual(self.presses, [])
