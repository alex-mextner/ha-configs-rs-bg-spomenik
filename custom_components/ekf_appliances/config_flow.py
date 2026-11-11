"""Configure existing local entities for the EKF appliance controls."""

from collections.abc import Mapping
from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers import selector
import voluptuous as vol

from .const import (
    CONF_PROJECTOR_OFF_SCRIPT,
    CONF_PROJECTOR_ON_SCRIPT,
    CONF_PROJECTOR_POWER_ENTITY_ID,
    CONF_REMOTE_ENTITY_ID,
    CONF_TEMPERATURE_ENTITY_ID,
    DEFAULT_PROJECTOR_OFF_SCRIPT,
    DEFAULT_PROJECTOR_ON_SCRIPT,
    DOMAIN,
)

_ENTITY_DOMAINS = {
    CONF_REMOTE_ENTITY_ID: "remote",
    CONF_TEMPERATURE_ENTITY_ID: "sensor",
    CONF_PROJECTOR_POWER_ENTITY_ID: "input_boolean",
    CONF_PROJECTOR_ON_SCRIPT: "script",
    CONF_PROJECTOR_OFF_SCRIPT: "script",
}
_OPTIONAL_FIELDS = {CONF_PROJECTOR_ON_SCRIPT, CONF_PROJECTOR_OFF_SCRIPT}
_DEFAULT_ENTITIES = {
    CONF_REMOTE_ENTITY_ID: "remote.ekf_connect_ir",
    CONF_TEMPERATURE_ENTITY_ID: "sensor.ekf_connect_ir_temperatura",
    CONF_PROJECTOR_POWER_ENTITY_ID: "input_boolean.projector_power",
    CONF_PROJECTOR_ON_SCRIPT: DEFAULT_PROJECTOR_ON_SCRIPT,
    CONF_PROJECTOR_OFF_SCRIPT: DEFAULT_PROJECTOR_OFF_SCRIPT,
}


class EkfAppliancesConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Connect one local remote to its appliance entities."""

    VERSION = 1

    def _schema(self, values: Mapping[str, Any] | None = None) -> vol.Schema:
        """Prefill existing entities while allowing optional overrides to clear."""
        if values is None:
            values = {
                key: entity_id
                for key, entity_id in _DEFAULT_ENTITIES.items()
                if self.hass.states.get(entity_id) is not None
            }

        fields = {}
        for key, domain in _ENTITY_DOMAINS.items():
            if key in _OPTIONAL_FIELDS:
                # A voluptuous default would reinsert an override when cleared.
                description = (
                    {"suggested_value": values[key]} if key in values else {}
                )
                marker = vol.Optional(key, description=description)
            else:
                marker = vol.Required(key, default=values.get(key, vol.UNDEFINED))
            fields[marker] = selector.EntitySelector(
                selector.EntitySelectorConfig(filter={"domain": domain})
            )
        return vol.Schema(fields)

    def _validate_entities(self, values: Mapping[str, Any]) -> dict[str, str]:
        """Reject missing or wrong-domain entities without operating them."""
        errors = {}
        for key, domain in _ENTITY_DOMAINS.items():
            if key in _OPTIONAL_FIELDS and key not in values:
                continue
            entity_id = values.get(key)
            if (
                not isinstance(entity_id, str)
                or entity_id.partition(".")[0] != domain
                or self.hass.states.get(entity_id) is None
            ):
                errors[key] = "invalid_entity"
        return errors

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create an entry from existing HA entities."""
        errors = {}
        if user_input is not None:
            errors = self._validate_entities(user_input)
            if not errors:
                remote_id = user_input[CONF_REMOTE_ENTITY_ID]
                await self.async_set_unique_id(remote_id)
                self._abort_if_unique_id_configured()
                self._async_abort_entries_match({CONF_REMOTE_ENTITY_ID: remote_id})
                remote = self.hass.states.get(remote_id)
                assert remote is not None
                return self.async_create_entry(
                    title=remote.name, data=dict(user_input)
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self._schema(user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change entity connections without creating another appliance set."""
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            errors = self._validate_entities(user_input)
            if not errors:
                remote_id = user_input[CONF_REMOTE_ENTITY_ID]
                existing = await self.async_set_unique_id(remote_id)
                if existing is not None and existing.entry_id != entry.entry_id:
                    self._abort_if_unique_id_configured()
                self._async_abort_entries_match({CONF_REMOTE_ENTITY_ID: remote_id})
                data = {
                    key: value
                    for key, value in entry.data.items()
                    if key not in _ENTITY_DOMAINS
                }
                data.update(user_input)
                remote = self.hass.states.get(remote_id)
                assert remote is not None
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=remote_id,
                    title=remote.name,
                    data=data,
                    reload_even_if_entry_is_unchanged=False,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._schema(
                user_input if user_input is not None else entry.data
            ),
            errors=errors,
        )
