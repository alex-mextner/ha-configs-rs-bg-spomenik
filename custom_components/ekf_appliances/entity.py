"""Stable identities and transmitter availability shared by the platforms."""
from homeassistant.core import callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_state_change_event

from .runtime import ApplianceRuntime


class EkfEntity(Entity):
    """An appliance entity whose transport is a configured HA remote."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_assumed_state = True

    def __init__(self, runtime: ApplianceRuntime, kind: str, suffix: str) -> None:
        self.runtime = runtime
        self._attr_unique_id = f"{runtime.entry.entry_id}_{suffix}"
        self._attr_device_info = runtime.device_info(kind)

    @property
    def available(self) -> bool:
        return self.runtime.available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_track_state_change_event(
            self.hass, [self.runtime.remote_entity_id], self._async_dependency_changed
        ))

    @callback
    def _async_dependency_changed(self, event) -> None:
        self.async_write_ha_state()
