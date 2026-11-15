"""Choose the active physical IR receiver without changing the remote layout."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .runtime import ApplianceRuntime

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.entity import EntityCategory

from .receiver import SOURCES


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry[ApplianceRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the physical receiver selector for this config entry."""
    async_add_entities([RemoteReceiverSource(entry)])


class RemoteReceiverSource(SelectEntity):
    """Select the one physical receiver allowed to dispatch remote events."""

    _attr_should_poll = False
    _attr_name = "Приёмник универсального пульта"
    _attr_icon = "mdi:remote"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options: ClassVar[list[str]] = list(SOURCES)

    def __init__(self, entry: ConfigEntry[ApplianceRuntime]) -> None:
        """Bind this entity to the config entry runtime."""
        self.receiver = entry.runtime_data.receiver
        self._attr_unique_id = f"{entry.entry_id}_ir_receiver_source"
        self._attr_suggested_object_id = "universal_remote_receiver"

    @property
    def current_option(self) -> str:
        """Return the active physical receiver source."""
        return self.receiver.source

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return current receiver or routing diagnostics."""
        return self.receiver.diagnostics

    async def async_added_to_hass(self) -> None:
        """Subscribe to receiver or controller state updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.receiver.add_callback(lambda _: self.async_write_ha_state())
        )

    async def async_select_option(self, option: str) -> None:
        """Select the requested physical receiver."""
        await self.receiver.async_set_source(option)
