"""Configure existing local entities for the EKF appliance controls."""

from collections.abc import Mapping
from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_MODEL
from homeassistant.helpers import device_registry as dr, entity_registry as er, selector
from homeassistant.exceptions import HomeAssistantError
from .samsung_network import async_prepare_websocket, async_apply_websocket
import voluptuous as vol

from .const import (
    CONF_PROJECTOR_OFF_SCRIPT,
    CONF_PROJECTOR_ON_SCRIPT,
    CONF_PROJECTOR_POWER_ENTITY_ID,
    CONF_REMOTE_ENTITY_ID,
    CONF_TEMPERATURE_ENTITY_ID,
    CONF_TV_ENTITY_ID,
    CONF_TV_NETWORK_PROTOCOL,
    CONF_TV_CAST_ENTITY_ID,
    CONF_TV_ANDROIDTV_ENTITY_ID,
    CONF_TV_MUSIC_ASSISTANT_ENTITY_ID,
    CONF_TV_KODI_ENTITY_ID,
    CONF_TV_SOURCE_ENTITY_ID,
    DEFAULT_PROJECTOR_OFF_SCRIPT,
    DEFAULT_PROJECTOR_ON_SCRIPT,
    DOMAIN,
)

_ENTITY_DOMAINS = {
    CONF_REMOTE_ENTITY_ID: "remote",
    CONF_TEMPERATURE_ENTITY_ID: "sensor",
    CONF_PROJECTOR_POWER_ENTITY_ID: "binary_sensor",
    CONF_PROJECTOR_ON_SCRIPT: "script",
    CONF_PROJECTOR_OFF_SCRIPT: "script",
    CONF_TV_ENTITY_ID: "media_player",
    CONF_TV_CAST_ENTITY_ID: "media_player",
    CONF_TV_ANDROIDTV_ENTITY_ID: "media_player",
    CONF_TV_MUSIC_ASSISTANT_ENTITY_ID: "media_player",
    CONF_TV_KODI_ENTITY_ID: "media_player",
    CONF_TV_SOURCE_ENTITY_ID: "input_text",
}
_OPTIONAL_FIELDS = {
    CONF_PROJECTOR_ON_SCRIPT, CONF_PROJECTOR_OFF_SCRIPT, CONF_TV_ENTITY_ID,
    CONF_TV_CAST_ENTITY_ID, CONF_TV_ANDROIDTV_ENTITY_ID,
    CONF_TV_MUSIC_ASSISTANT_ENTITY_ID, CONF_TV_KODI_ENTITY_ID,
    CONF_TV_SOURCE_ENTITY_ID, CONF_TV_NETWORK_PROTOCOL,
}
_MEDIA_PLATFORMS = {
    CONF_TV_CAST_ENTITY_ID: "cast",
    CONF_TV_ANDROIDTV_ENTITY_ID: "androidtv_remote",
    CONF_TV_MUSIC_ASSISTANT_ENTITY_ID: "music_assistant",
    CONF_TV_KODI_ENTITY_ID: "kodi",
}
_DEFAULT_ENTITIES = {
    CONF_REMOTE_ENTITY_ID: "remote.ekf_connect_ir",
    CONF_TEMPERATURE_ENTITY_ID: "sensor.ekf_connect_ir_temperatura",
    CONF_PROJECTOR_POWER_ENTITY_ID: "binary_sensor.projector_hdmi_connected",
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
                default = values.get(key, vol.UNDEFINED)
                if key == CONF_PROJECTOR_POWER_ENTITY_ID and (
                    not isinstance(default, str)
                    or default.partition(".")[0] != domain
                ):
                    # Legacy helpers require an explicit feedback sensor selection.
                    default = vol.UNDEFINED
                marker = vol.Required(key, default=default)
            entity_filter = {"domain": domain}
            if key == CONF_TV_ENTITY_ID:
                entity_filter["integration"] = "samsungtv"
            elif key in _MEDIA_PLATFORMS:
                entity_filter["integration"] = _MEDIA_PLATFORMS[key]
            fields[marker] = selector.EntitySelector(
                selector.EntitySelectorConfig(filter=entity_filter)
            )
        description = (
            {"suggested_value": values[CONF_TV_NETWORK_PROTOCOL]}
            if CONF_TV_NETWORK_PROTOCOL in values else {}
        )
        fields[vol.Optional(CONF_TV_NETWORK_PROTOCOL, description=description)] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["current", "websocket_8001"],
                translation_key="tv_network_protocol",
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
        return vol.Schema(fields)

    @staticmethod
    def _normalize_input(values: Mapping[str, Any]) -> dict[str, Any]:
        """An empty optional TV selection disables only the TV adapter."""
        result = dict(values)
        for key in _OPTIONAL_FIELDS:
            if result.get(key) in (None, ""):
                result.pop(key, None)
        return result

    def _is_native_tv(self, entity_id: str) -> bool:
        """Validate native identity and saved connection data without probing TV."""
        entity = er.async_get(self.hass).async_get(entity_id)
        if (
            entity is None
            or entity.platform != "samsungtv"
            or entity.device_id is None
            or entity.config_entry_id is None
        ):
            return False
        entry = self.hass.config_entries.async_get_entry(entity.config_entry_id)
        if entry is None or entry.domain != "samsungtv":
            return False
        if any(
            not isinstance(value := entry.data.get(key), str) or not value.strip()
            for key in (CONF_HOST, CONF_MODEL)
        ):
            return False
        device = dr.async_get(self.hass).async_get(entity.device_id)
        return device is not None and any(
            domain == "samsungtv" and identifier
            for domain, identifier in device.identifiers
        )

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
                errors[key] = (
                    "invalid_tv_entity" if key == CONF_TV_ENTITY_ID else "invalid_entity"
                )
            elif key == CONF_TV_ENTITY_ID and not self._is_native_tv(entity_id):
                errors[key] = "invalid_tv_entity"
            elif key in _MEDIA_PLATFORMS:
                entity = er.async_get(self.hass).async_get(entity_id)
                if entity is None or entity.platform != _MEDIA_PLATFORMS[key]:
                    errors[key] = "invalid_entity"
        cast_id = values.get(CONF_TV_CAST_ENTITY_ID)
        music_id = values.get(CONF_TV_MUSIC_ASSISTANT_ENTITY_ID)
        if cast_id and music_id and not errors:
            registry = er.async_get(self.hass)
            cast = registry.async_get(cast_id)
            music = registry.async_get(music_id)
            if cast is None or music is None or cast.unique_id != music.unique_id:
                errors[CONF_TV_MUSIC_ASSISTANT_ENTITY_ID] = "different_cast_player"
        protocol = values.get(CONF_TV_NETWORK_PROTOCOL)
        if protocol is not None and protocol not in ("current", "websocket_8001"):
            errors[CONF_TV_NETWORK_PROTOCOL] = "invalid_tv_protocol"
        if protocol == "websocket_8001" and not values.get(CONF_TV_ENTITY_ID):
            errors[CONF_TV_NETWORK_PROTOCOL] = "invalid_tv_entity"
        return errors

    async def _prepare_tv_network(self, values):
        if values.get(CONF_TV_NETWORK_PROTOCOL) != "websocket_8001":
            return None
        entity = er.async_get(self.hass).async_get(values[CONF_TV_ENTITY_ID])
        entry = self.hass.config_entries.async_get_entry(entity.config_entry_id)
        return await async_prepare_websocket(self.hass, entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create an entry from existing HA entities."""
        errors = {}
        if user_input is not None:
            user_input = self._normalize_input(user_input)
            errors = self._validate_entities(user_input)
            prepared = None
            if not errors:
                try:
                    prepared = await self._prepare_tv_network(user_input)
                except HomeAssistantError:
                    errors[CONF_TV_NETWORK_PROTOCOL] = "tv_network_unavailable"
            if not errors:
                remote_id = user_input[CONF_REMOTE_ENTITY_ID]
                await self.async_set_unique_id(remote_id)
                self._abort_if_unique_id_configured()
                self._async_abort_entries_match({CONF_REMOTE_ENTITY_ID: remote_id})
                remote = self.hass.states.get(remote_id)
                assert remote is not None
                try:
                    async_apply_websocket(self.hass, prepared)
                except HomeAssistantError:
                    return self.async_show_form(
                        step_id="user", data_schema=self._schema(user_input),
                        errors={CONF_TV_NETWORK_PROTOCOL: "tv_network_changed"},
                    )
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
            user_input = self._normalize_input(user_input)
            errors = self._validate_entities(user_input)
            prepared = None
            if not errors:
                try:
                    prepared = await self._prepare_tv_network(user_input)
                except HomeAssistantError:
                    errors[CONF_TV_NETWORK_PROTOCOL] = "tv_network_unavailable"
            if not errors:
                remote_id = user_input[CONF_REMOTE_ENTITY_ID]
                existing = await self.async_set_unique_id(remote_id)
                if existing is not None and existing.entry_id != entry.entry_id:
                    self._abort_if_unique_id_configured()
                self._async_abort_entries_match({CONF_REMOTE_ENTITY_ID: remote_id})
                data = {
                    key: value
                    for key, value in entry.data.items()
                    if key not in _ENTITY_DOMAINS and key != CONF_TV_NETWORK_PROTOCOL
                }
                data.update(user_input)
                remote = self.hass.states.get(remote_id)
                assert remote is not None
                try:
                    async_apply_websocket(self.hass, prepared)
                except HomeAssistantError:
                    return self.async_show_form(
                        step_id="reconfigure", data_schema=self._schema(user_input),
                        errors={CONF_TV_NETWORK_PROTOCOL: "tv_network_changed"},
                    )
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
