"""User-visible configuration contracts against an isolated, real HA core."""

import importlib
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow
import voluptuous as vol


DOMAIN = "ekf_appliances"
VALID = {
    "remote_entity_id": "remote.ekf_connect_ir",
    "temperature_entity_id": "sensor.ekf_connect_ir_temperatura",
    "projector_power_entity_id": "input_boolean.projector_power",
    "projector_on_script": "script.projector_turn_on",
    "projector_off_script": "script.projector_turn_off",
}


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tempdir.name)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.transmissions = []

        async def record_service(call):
            self.transmissions.append((call.domain, call.service, dict(call.data)))

        self.hass.services.async_register("remote", "send_command", record_service)
        self.hass.services.async_register("script", "turn_on", record_service)
        self.addCleanup(self.tempdir.cleanup)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        try:
            self.flow_module = importlib.import_module(
                "custom_components.ekf_appliances.config_flow"
            )
        except ModuleNotFoundError as err:
            if err.name != "custom_components.ekf_appliances.config_flow":
                raise
            self.fail("The EKF configuration flow has not been implemented.")

    def populate_entities(self, state="off"):
        for entity_id in VALID.values():
            self.hass.states.async_set(entity_id, state)

    def flow(self, source="user", entry=None):
        flow = self.flow_module.EkfAppliancesConfigFlow()
        flow.hass = self.hass
        flow.handler = DOMAIN
        flow.flow_id = "isolated-config-flow"
        flow.context = {"source": source}
        if entry is not None:
            flow.context["entry_id"] = entry.entry_id
        return flow

    def add_entry(self, data=None, unique_id=None):
        data = dict(VALID if data is None else data)
        entry = ConfigEntry(
            domain=DOMAIN,
            title="EKF Appliances",
            data=data,
            options={},
            source="user",
            unique_id=unique_id or data["remote_entity_id"],
            version=1,
            minor_version=1,
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        self.hass.config_entries._entries[entry.entry_id] = entry
        return entry

    async def test_form_selects_expected_domains_and_only_existing_defaults(self):
        self.populate_entities()
        result = await self.flow().async_step_user()
        self.assertEqual(result["type"], "form")
        fields = {
            marker.schema: (marker, selector)
            for marker, selector in result["data_schema"].schema.items()
        }
        self.assertEqual(set(fields), set(VALID))
        domains = {
            "remote_entity_id": "remote",
            "temperature_entity_id": "sensor",
            "projector_power_entity_id": "input_boolean",
            "projector_on_script": "script",
            "projector_off_script": "script",
        }
        for key, domain in domains.items():
            with self.subTest(field=key):
                marker, selector = fields[key]
                self.assertEqual(selector.config["filter"], [{"domain": [domain]}])
                default = (marker.default() if marker.default is not vol.UNDEFINED
                           else (marker.description or {}).get("suggested_value"))
                self.assertEqual(default, VALID[key])
                self.assertEqual(
                    isinstance(marker, vol.Required),
                    key in (
                        "remote_entity_id",
                        "temperature_entity_id",
                        "projector_power_entity_id",
                    ),
                )

    async def test_absent_entities_are_not_suggested_as_defaults(self):
        result = await self.flow().async_step_user()
        for marker in result["data_schema"].schema:
            with self.subTest(field=marker.schema):
                self.assertIs(marker.default, vol.UNDEFINED)
                self.assertNotIn("suggested_value", marker.description or {})

    async def test_valid_selection_creates_entry_without_transmitting(self):
        self.populate_entities()
        flow = self.flow()
        result = await flow.async_step_user(dict(VALID))
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], VALID)
        self.assertEqual(flow.unique_id, "remote.ekf_connect_ir")
        self.assertEqual(self.transmissions, [])

    async def test_missing_or_wrong_domain_selection_is_rejected_per_field(self):
        self.populate_entities()
        self.hass.states.async_set("light.wrong_device", "on")
        for key in VALID:
            for value in ("light.wrong_device", "sensor.not_present"):
                data = {**VALID, key: value}
                with self.subTest(field=key, value=value):
                    result = await self.flow().async_step_user(data)
                    self.assertEqual(result["type"], "form")
                    self.assertEqual(result["errors"], {key: "invalid_entity"})
        for key in (
            "remote_entity_id",
            "temperature_entity_id",
            "projector_power_entity_id",
        ):
            data = {field: value for field, value in VALID.items() if field != key}
            with self.subTest(missing=key):
                result = await self.flow().async_step_user(data)
                self.assertEqual(result["errors"], {key: "invalid_entity"})
        self.assertEqual(self.transmissions, [])

    async def test_unavailable_entities_can_be_configured_without_transmitting(self):
        self.populate_entities("unavailable")
        result = await self.flow().async_step_user(dict(VALID))
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(self.transmissions, [])

    async def test_optional_scripts_can_be_omitted(self):
        self.populate_entities()
        data = {
            key: value for key, value in VALID.items() if not key.endswith("_script")
        }
        result = await self.flow().async_step_user(data)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], data)

    async def test_same_remote_cannot_create_a_second_entry(self):
        self.populate_entities()
        self.add_entry()
        with self.assertRaises(AbortFlow) as raised:
            await self.flow().async_step_user(dict(VALID))
        self.assertEqual(raised.exception.reason, "already_configured")
        self.assertEqual(self.transmissions, [])

    async def test_reconfigure_prefills_current_selection(self):
        self.populate_entities()
        self.hass.states.async_set("sensor.other_temperature", "21.5")
        entry = self.add_entry({**VALID, "temperature_entity_id": "sensor.other_temperature"})
        result = await self.flow("reconfigure", entry).async_step_reconfigure()
        defaults = {
            marker.schema: (marker.default() if marker.default is not vol.UNDEFINED
                            else marker.description["suggested_value"])
            for marker in result["data_schema"].schema
        }
        self.assertEqual(defaults, dict(entry.data))

    async def test_reconfigure_updates_data_identity_and_preserves_unrelated_data(self):
        self.populate_entities()
        self.hass.states.async_set("remote.replacement", "off")
        entry = self.add_entry({**VALID, "future_setting": "keep"})
        data = {**VALID, "remote_entity_id": "remote.replacement"}
        with patch.object(self.hass.config_entries, "async_schedule_reload"):
            result = await self.flow("reconfigure", entry).async_step_reconfigure(data)
        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(entry.unique_id, "remote.replacement")
        self.assertEqual(dict(entry.data), {**data, "future_setting": "keep"})
        self.assertEqual(self.transmissions, [])

    async def test_reconfigure_can_clear_optional_script_overrides(self):
        self.populate_entities()
        self.hass.states.async_set("script.custom_on", "off")
        entry = self.add_entry({**VALID, "projector_on_script": "script.custom_on"})
        data = {
            key: value for key, value in VALID.items() if not key.endswith("_script")
        }
        flow = self.flow("reconfigure", entry)
        form = await flow.async_step_reconfigure()
        data = form["data_schema"](data)
        with patch.object(self.hass.config_entries, "async_schedule_reload"):
            result = await flow.async_step_reconfigure(data)
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(dict(entry.data), data)

    async def test_reconfigure_rejects_a_remote_owned_by_another_entry(self):
        self.populate_entities()
        self.hass.states.async_set("remote.other", "off")
        original = self.add_entry()
        other_data = {**VALID, "remote_entity_id": "remote.other"}
        self.add_entry(other_data)
        with self.assertRaises(AbortFlow) as raised:
            await self.flow("reconfigure", original).async_step_reconfigure(other_data)
        self.assertEqual(raised.exception.reason, "already_configured")
        self.assertEqual(dict(original.data), VALID)
        self.assertEqual(original.unique_id, "remote.ekf_connect_ir")
        self.assertEqual(self.transmissions, [])
