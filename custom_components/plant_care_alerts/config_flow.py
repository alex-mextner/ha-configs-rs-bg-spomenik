"""Native selection of the speaker used for watering reminders."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from . import DOMAIN
from .voice import CONF_VOICE_TARGET, configured_target


def voice_schema(value: str | None = None) -> vol.Schema:
    """Only offer entities with the speech transports this integration supports."""
    marker = vol.Required(CONF_VOICE_TARGET)
    if value:
        marker = vol.Required(CONF_VOICE_TARGET, default=value)
    return vol.Schema({
        marker: selector.EntitySelector(selector.EntitySelectorConfig(
            filter=[
                {"domain": "notify", "integration": "aisis_speaker"},
                {"domain": "media_player", "integration": "yandex_station"},
            ],
        )),
    })


def valid_target(hass: Any, target: str) -> bool:
    """Reject unrelated player/notify entities submitted outside the UI."""
    item = er.async_get(hass).async_get(target)
    if item is None:
        return False
    return (target.startswith("notify.") and item.platform == "aisis_speaker") or (
        target.startswith("media_player.") and item.platform == "yandex_station"
    )


class PlantCareConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create a single household-wide notification setting."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        await self.async_set_unique_id("plant_care_voice")
        self._abort_if_unique_id_configured()
        errors = {}
        if user_input is not None:
            if valid_target(self.hass, user_input[CONF_VOICE_TARGET]):
                return self.async_create_entry(
                    title="Напоминания о поливе", data=user_input
                )
            errors[CONF_VOICE_TARGET] = "invalid_speaker"
        manager = self.hass.data.get(DOMAIN)
        default = manager.voice_target if manager else None
        return self.async_show_form(
            step_id="user", data_schema=voice_schema(
                (user_input or {}).get(CONF_VOICE_TARGET, default)
            ), errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return PlantCareOptionsFlow()


class PlantCareOptionsFlow(config_entries.OptionsFlow):
    """Changing the speaker applies immediately, without restarting Home Assistant."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            if valid_target(self.hass, user_input[CONF_VOICE_TARGET]):
                return self.async_create_entry(title="", data=user_input)
            errors[CONF_VOICE_TARGET] = "invalid_speaker"
        default = configured_target(self.config_entry, None)
        return self.async_show_form(
            step_id="init", data_schema=voice_schema(
                (user_input or {}).get(CONF_VOICE_TARGET, default)
            ), errors=errors,
        )
