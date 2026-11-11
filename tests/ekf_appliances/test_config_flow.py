"""User-visible configuration contracts against an isolated, real HA core."""

import importlib
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import patch

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame
import voluptuous as vol


DOMAIN = "ekf_appliances"
VALID = {
    "remote_entity_id": "remote.ekf_connect_ir",
    "temperature_entity_id": "sensor.ekf_connect_ir_temperatura",
    "projector_power_entity_id": "binary_sensor.projector_hdmi_connected",
    "projector_on_script": "script.projector_turn_on",
    "projector_off_script": "script.projector_turn_off",
}


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tempdir.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        self.tv_counter = 0
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

    def register_tv(
        self, *, platform="samsungtv", device=True, samsung_identifier=True,
        config_domain="samsungtv", state="off", native_data=None
    ):
        self.tv_counter += 1
        unique_id = f"test-tv-{self.tv_counter}"
        entry = ConfigEntry(
            domain=config_domain, title="Test Samsung", options={},
            data=({"host": "192.0.2.10", "model": "UE48J5500"}
                  if native_data is None else native_data),
            source="user", unique_id=unique_id, version=1, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=None,
        )
        self.hass.config_entries._entries[entry.entry_id] = entry
        device_entry = None
        if device:
            device_entry = dr.async_get(self.hass).async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={("samsungtv" if samsung_identifier else "other", unique_id)},
            )
        entity = er.async_get(self.hass).async_get_or_create(
            "media_player", platform, unique_id, config_entry=entry,
            device_id=device_entry.id if device_entry else None,
            suggested_object_id=unique_id,
        )
        self.hass.states.async_set(entity.entity_id, state)
        return entity.entity_id

    async def test_form_selects_expected_domains_and_only_existing_defaults(self):
        self.populate_entities()
        result = await self.flow().async_step_user()
        self.assertEqual(result["type"], "form")
        fields = {
            marker.schema: (marker, selector)
            for marker, selector in result["data_schema"].schema.items()
        }
        self.assertEqual(set(fields), set(VALID) | {"tv_entity_id"})
        domains = {
            "remote_entity_id": "remote",
            "temperature_entity_id": "sensor",
            "projector_power_entity_id": "binary_sensor",
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
            if marker.default is not vol.UNDEFINED
            or "suggested_value" in (marker.description or {})
        }
        self.assertEqual(defaults, dict(entry.data))

    async def test_saved_legacy_helper_is_not_a_valid_feedback_selection(self):
        self.populate_entities()
        self.hass.states.async_set("input_boolean.projector_power", "on")
        legacy = {**VALID, "projector_power_entity_id": "input_boolean.projector_power"}
        result = await self.flow().async_step_user(legacy)
        self.assertEqual(result["type"], "form")
        self.assertEqual(
            result["errors"], {"projector_power_entity_id": "invalid_entity"}
        )
        self.assertEqual(self.transmissions, [])

    async def test_legacy_reconfigure_requires_a_new_feedback_selection(self):
        self.populate_entities()
        self.hass.states.async_set("input_boolean.projector_power", "on")
        legacy = {**VALID, "projector_power_entity_id": "input_boolean.projector_power"}
        entry = self.add_entry({**legacy, "future_setting": "keep"})
        flow = self.flow("reconfigure", entry)
        result = await flow.async_step_reconfigure()
        power_marker = next(
            marker for marker in result["data_schema"].schema
            if marker.schema == "projector_power_entity_id"
        )
        self.assertIsInstance(power_marker, vol.Required)
        self.assertIs(power_marker.default, vol.UNDEFINED)
        self.assertNotIn("suggested_value", power_marker.description or {})
        result = await flow.async_step_reconfigure(legacy)
        self.assertEqual(result["type"], "form")
        self.assertEqual(
            result["errors"], {"projector_power_entity_id": "invalid_entity"}
        )
        self.assertEqual(dict(entry.data), {**legacy, "future_setting": "keep"})
        self.assertEqual(self.transmissions, [])

    async def test_reconfigure_migrates_feedback_without_losing_script_overrides(self):
        self.populate_entities()
        self.hass.states.async_set("input_boolean.projector_power", "on")
        self.hass.states.async_set("script.custom_on", "off")
        self.hass.states.async_set("script.custom_off", "off")
        legacy = {
            **VALID,
            "projector_power_entity_id": "input_boolean.projector_power",
            "projector_on_script": "script.custom_on",
            "projector_off_script": "script.custom_off",
        }
        entry = self.add_entry({**legacy, "future_setting": "keep"})
        selected = {
            **legacy,
            "projector_power_entity_id": "binary_sensor.projector_hdmi_connected",
        }
        with patch.object(self.hass.config_entries, "async_schedule_reload"):
            result = await self.flow("reconfigure", entry).async_step_reconfigure(selected)
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(dict(entry.data), {**selected, "future_setting": "keep"})
        self.assertEqual(self.transmissions, [])

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

    async def test_tv_selector_is_optional_native_samsung_without_guessed_default(self):
        self.populate_entities()
        self.register_tv()
        result = await self.flow().async_step_user()
        fields = {
            marker.schema: (marker, selected)
            for marker, selected in result["data_schema"].schema.items()
        }
        self.assertIn("tv_entity_id", fields)
        marker, selected = fields["tv_entity_id"]
        self.assertIsInstance(marker, vol.Optional)
        self.assertNotIsInstance(marker, vol.Required)
        self.assertIs(marker.default, vol.UNDEFINED)
        self.assertNotIn("suggested_value", marker.description or {})
        self.assertEqual(
            selected.config["filter"],
            [{"domain": ["media_player"], "integration": "samsungtv"}],
        )

    async def test_tv_backend_rejects_unregistered_wrappers_and_wrong_devices(self):
        self.populate_entities()
        self.hass.states.async_set("media_player.spoofed", "off")
        invalid = [
            "media_player.spoofed",
            "media_player.missing",
            "sensor.ekf_connect_ir_temperatura",
            self.register_tv(platform="ekf_appliances"),
            self.register_tv(platform="universal"),
            self.register_tv(device=False),
            self.register_tv(samsung_identifier=False),
            self.register_tv(config_domain="other"),
            self.register_tv(native_data={}),
            self.register_tv(native_data={"host": " ", "model": "UE48J5500"}),
            self.register_tv(native_data={"host": "192.0.2.10", "model": ""}),
            self.register_tv(native_data={"host": 123, "model": "UE48J5500"}),
        ]
        for entity_id in invalid:
            with self.subTest(entity_id=entity_id):
                result = await self.flow().async_step_user(
                    {**VALID, "tv_entity_id": entity_id}
                )
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"], {"tv_entity_id": "invalid_tv_entity"})
        self.assertEqual(self.transmissions, [])

    async def test_registered_native_tv_can_be_configured_while_unavailable(self):
        self.populate_entities()
        for state in ("off", "unknown", "unavailable"):
            with self.subTest(state=state):
                entity_id = self.register_tv(state=state)
                values = {**VALID, "tv_entity_id": entity_id}
                result = await self.flow().async_step_user(values)
                self.assertEqual(result["type"], "create_entry")
                self.assertEqual(result["data"], values)
        self.assertEqual(self.transmissions, [])

    async def test_tv_reconfigure_prefills_and_can_clear_only_tv_backend(self):
        self.populate_entities()
        self.hass.states.async_set("script.custom_on", "off")
        self.hass.states.async_set("script.custom_off", "off")
        connections = {
            **VALID, "projector_on_script": "script.custom_on",
            "projector_off_script": "script.custom_off",
        }
        entity_id = self.register_tv()
        entry = self.add_entry({
            **connections, "tv_entity_id": entity_id, "future_setting": "keep",
        })
        flow = self.flow("reconfigure", entry)
        form = await flow.async_step_reconfigure()
        fields = {field.schema: field for field in form["data_schema"].schema}
        self.assertIn("tv_entity_id", fields)
        marker = fields["tv_entity_id"]
        self.assertIs(marker.default, vol.UNDEFINED)
        self.assertEqual(marker.description["suggested_value"], entity_id)
        submitted = form["data_schema"](connections)
        with patch.object(self.hass.config_entries, "async_schedule_reload"):
            result = await flow.async_step_reconfigure(submitted)
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(dict(entry.data), {**connections, "future_setting": "keep"})
        self.assertEqual(self.transmissions, [])

    async def test_explicit_empty_tv_backend_disables_optional_adapter(self):
        self.populate_entities()
        for empty in (None, ""):
            with self.subTest(empty=empty):
                result = await self.flow().async_step_user({**VALID, "tv_entity_id": empty})
                self.assertEqual(result["type"], "create_entry")
                self.assertEqual(result["data"], VALID)
        self.assertEqual(self.transmissions, [])

    async def test_adding_tv_backend_preserves_other_saved_connections(self):
        self.populate_entities()
        self.hass.states.async_set("script.custom_on", "off")
        connections = {**VALID, "projector_on_script": "script.custom_on"}
        entry = self.add_entry({**connections, "future_setting": "keep"})
        values = {**connections, "tv_entity_id": self.register_tv()}
        with patch.object(self.hass.config_entries, "async_schedule_reload"):
            result = await self.flow("reconfigure", entry).async_step_reconfigure(values)
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(dict(entry.data), {**values, "future_setting": "keep"})
        self.assertEqual(self.transmissions, [])
