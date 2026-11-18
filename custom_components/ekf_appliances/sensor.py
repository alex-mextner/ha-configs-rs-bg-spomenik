"""The single committed universal remote target, including temporary audio mode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .runtime import ApplianceRuntime

from homeassistant.components.sensor import SensorEntity

from .remote_control import TARGET


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry[ApplianceRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the committed target sensor for this config entry."""
    async_add_entities([UniversalRemoteTarget(entry)])


class UniversalRemoteTarget(SensorEntity):
    """Expose the complete effective target committed by the controller."""

    _attr_should_poll = False
    _attr_name = "Universal remote target"
    _attr_suggested_object_id = "universal_remote_target"
    _attr_icon = "mdi:remote"

    def __init__(self, entry: ConfigEntry[ApplianceRuntime]) -> None:
        """Bind this entity to the config entry runtime."""
        self.controller = entry.runtime_data.remote_controller
        self._attr_unique_id = f"{entry.entry_id}_universal_remote_target"
        self.entity_id = TARGET

    @property
    def native_value(self) -> str:
        """Return the atomically committed remote destination."""
        return self.controller.mode.target

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return current receiver or routing diagnostics."""
        mode = self.controller.mode
        return {
            "pair": mode.pair,
            "native": mode.native,
            "initialized": self.controller.initialized,
            "lease_until": mode.expires_at or None,
            "projector_shutting_down": mode.shutting_down,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to receiver or controller state updates."""
        await super().async_added_to_hass()
        self.async_on_remove(self.controller.subscribe(self.async_write_ha_state))
