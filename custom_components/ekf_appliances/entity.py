"""Stable identities and transmitter availability shared by the platforms."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import Event

    from .runtime import ApplianceRuntime

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_state_change_event


class EkfEntity(Entity):
    """An appliance entity whose transport is a configured HA remote."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_assumed_state = True

    def __init__(self, runtime: ApplianceRuntime, kind: str, suffix: str) -> None:
        """Bind appliance identity and availability to the shared runtime."""
        self.runtime = runtime
        self._attr_unique_id = f"{runtime.entry.entry_id}_{suffix}"
        self._attr_device_info = runtime.device_info(kind)

    @property
    def available(self) -> bool:
        """Return the availability of the configured IR transmitter."""
        return self.runtime.available

    async def async_added_to_hass(self) -> None:
        """Subscribe to transmitter availability changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self.runtime.remote_entity_id],
                self._async_dependency_changed,
            )
        )

    @callback
    def _async_dependency_changed(self, _event: Event) -> None:
        self.async_write_ha_state()
