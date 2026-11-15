"""Explicit opt-in for the EKF physical remote receiver."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .runtime import ApplianceRuntime

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import EntityCategory


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry[ApplianceRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the receiver enable switch for this config entry."""
    async_add_entities([EkfRemoteReceiverSwitch(entry)])


class EkfRemoteReceiverSwitch(SwitchEntity):
    """Enable or disable semantic dispatch from the selected IR receiver."""

    _attr_should_poll = False
    _attr_name = "Приём универсального пульта"
    _attr_icon = "mdi:remote"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry[ApplianceRuntime]) -> None:
        """Bind this entity to the config entry runtime."""
        self.receiver = entry.runtime_data.receiver
        self._attr_unique_id = f"{entry.entry_id}_ir_receiver_enabled"
        self._attr_suggested_object_id = "ekf_universal_remote_receiver"

    @property
    def is_on(self) -> bool:
        """Return whether the selected receiver dispatches remote presses."""
        return self.receiver.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return current receiver or routing diagnostics."""
        return {
            **self.receiver.diagnostics,
            "validated": self.receiver.validated,
            "last_error": self.receiver.last_error,
            "remote_entity_id": self.receiver.remote_id,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to receiver or controller state updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.receiver.add_callback(lambda _: self.async_write_ha_state())
        )

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Enable semantic events from the selected receiver."""
        await self.receiver.async_set_enabled(enabled=True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Disable semantic events from the selected receiver."""
        await self.receiver.async_set_enabled(enabled=False)
