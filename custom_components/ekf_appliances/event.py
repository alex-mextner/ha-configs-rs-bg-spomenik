"""Physical universal remote events from the selected receiver."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .runtime import ApplianceRuntime

from homeassistant.components.event import EventEntity

from .ir_decode import EVENT_TYPES


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry[ApplianceRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the physical remote event entity for this config entry."""
    async_add_entities([EkfRemoteEvent(entry)])


class EkfRemoteEvent(EventEntity):
    """Expose accepted physical presses from the selected receiver."""

    _attr_should_poll = False
    _attr_name = "Универсальный пульт EKF"
    _attr_icon = "mdi:remote"
    _attr_event_types = EVENT_TYPES
    _attr_device_class = "button"

    def __init__(self, entry: ConfigEntry[ApplianceRuntime]) -> None:
        """Bind this entity to the config entry runtime."""
        self.receiver = entry.runtime_data.receiver
        self._attr_unique_id = f"{entry.entry_id}_universal_remote"
        self._attr_suggested_object_id = "ekf_universal_remote"

    @property
    def available(self) -> bool:
        """Return whether remote dispatch is enabled without a receiver error."""
        return self.receiver.enabled and self.receiver.last_error is None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return current receiver or routing diagnostics."""
        return {
            **self.receiver.diagnostics,
            "receiver_enabled": self.receiver.enabled,
            "validated": self.receiver.validated,
            "last_received": self.receiver.last_received,
            "last_error": self.receiver.last_error,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to receiver or controller state updates."""
        await super().async_added_to_hass()

        def update(name: str | None) -> None:
            if name is not None:
                self._trigger_event(name, {"receiver": self.receiver.source})
            self.async_write_ha_state()

        self.async_on_remove(self.receiver.add_callback(update))
