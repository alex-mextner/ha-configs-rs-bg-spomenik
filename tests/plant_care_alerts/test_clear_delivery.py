"""Run the real blueprint clear/ack protocol across fault and recovery failures."""

import tempfile
import unittest
from pathlib import Path

import yaml
from homeassistant import loader
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import frame
from homeassistant.helpers.script import Script

from custom_components.plant_care_alerts import acknowledge_delivery

ROOT = Path(__file__).resolve().parents[2]
CARE = "sensor.test_plant"
INPUTS = {
    "notify_target": "test_mobile",
    "voice_toggle": "",
    "voice_start": "08:00:00",
    "voice_end": "22:00:00",
    "dashboard_url": "/lovelace",
}


class InputLoader(yaml.SafeLoader):
    """Resolve only the blueprint's declared fixture inputs."""


def _input(parser, node) -> str:
    return INPUTS[parser.construct_scalar(node)]


InputLoader.add_constructor("!input", _input)
with (ROOT / "blueprints/automation/plant_care/alerts.yaml").open() as stream:
    parser = InputLoader(stream)
    try:
        BLUEPRINT = parser.get_single_data()
    finally:
        parser.dispose()


class ClearDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """A failed clear stays unacknowledged until the real retry succeeds."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.failures = 2
        self.notify_target = "test_mobile"
        self.clears = []
        self.acknowledgements = []
        self.record = {"status": "sensor_error", "clear_pending": True}
        self.hass.services.async_register("notify", "test_mobile", self.notify)
        self.hass.services.async_register("plant_care_alerts", "acknowledge", self.ack)
        for service in ("create", "dismiss"):
            self.hass.services.async_register(
                "persistent_notification", service, self.persistent
            )

    async def asyncTearDown(self) -> None:
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def notify(self, call) -> None:
        self.clears.append(dict(call.data))
        if self.failures:
            self.failures -= 1
            message = "Simulated phone clear failure"
            raise HomeAssistantError(message)

    async def ack(self, call) -> None:
        self.acknowledgements.append(dict(call.data))
        acknowledge_delivery(self.record, call.data["status"])

    async def persistent(self, _call) -> None:
        return

    async def dispatch(self, status, previous, *, clear: bool) -> None:
        self.record["status"] = status
        self.hass.states.async_set(
            CARE, status, {"plant_care": True, "plant_name": "Test plant"}
        )
        variables = {**BLUEPRINT["variables"], "notify_target": self.notify_target}
        sequence = [{"variables": variables}, *BLUEPRINT["actions"]]
        script = Script(
            self.hass, cv.SCRIPT_SCHEMA(sequence), "Plant alert fixture", "automation"
        )
        await script.async_run(
            {
                "trigger": {
                    "id": "change",
                    "event": {
                        "data": {
                            "entity_id": CARE,
                            "status": status,
                            "previous": previous,
                            "notify": False,
                            "clear": clear,
                        }
                    },
                }
            },
            Context(),
        )

    async def test_failed_fault_and_recovery_clears_retry_and_ack_only_on_success(
        self,
    ) -> None:
        for status, previous in [("sensor_error", "due"), ("ok", "sensor_error")]:
            with self.subTest(status=status), self.assertRaises(HomeAssistantError):
                await self.dispatch(status, previous, clear=True)
            self.assertEqual(self.acknowledgements, [])
            self.assertTrue(self.record["clear_pending"])
        await self.dispatch("ok", "ok", clear=True)
        self.assertEqual(len(self.clears), 3)
        self.assertTrue(
            all(call["message"] == "clear_notification" for call in self.clears)
        )
        self.assertEqual(
            self.acknowledgements, [{"entity_id": CARE, "status": "sensor_error"}]
        )
        self.assertNotIn("clear_pending", self.record)
        await self.dispatch("ok", "ok", clear=False)
        self.assertEqual(len(self.clears), 3)
        self.assertEqual(len(self.acknowledgements), 1)

    async def test_no_phone_target_acknowledges_configured_local_actions(self) -> None:
        self.notify_target = ""
        for status, previous in [("sensor_error", "due"), ("ok", "sensor_error")]:
            with self.subTest(status=status):
                self.record["clear_pending"] = True
                await self.dispatch(status, previous, clear=True)
                self.assertNotIn("clear_pending", self.record)
        self.assertEqual(self.clears, [])
        self.assertEqual(len(self.acknowledgements), 2)
