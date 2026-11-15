"""The single committed universal remote target, including temporary audio mode."""
from homeassistant.components.sensor import SensorEntity

from .remote_control import TARGET


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([UniversalRemoteTarget(entry)])


class UniversalRemoteTarget(SensorEntity):
    _attr_should_poll = False
    _attr_name = "Universal remote target"
    _attr_suggested_object_id = "universal_remote_target"
    _attr_icon = "mdi:remote"

    def __init__(self, entry):
        self.controller = entry.runtime_data.remote_controller
        self._attr_unique_id = f"{entry.entry_id}_universal_remote_target"
        self.entity_id = TARGET

    @property
    def native_value(self):
        return self.controller.mode.target

    @property
    def extra_state_attributes(self):
        mode = self.controller.mode
        return {"pair": mode.pair, "native": mode.native,
                "initialized": self.controller.initialized,
                "lease_until": mode.expires_at or None,
                "projector_shutting_down": mode.shutting_down}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.controller.subscribe(self.async_write_ha_state))
