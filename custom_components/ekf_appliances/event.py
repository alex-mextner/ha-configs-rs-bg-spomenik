"""Physical universal remote events received by the EKF hub."""
from homeassistant.components.event import EventEntity
from .ir_decode import EVENT_TYPES

async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([EkfRemoteEvent(entry)])

class EkfRemoteEvent(EventEntity):
    _attr_should_poll = False
    _attr_name = "Универсальный пульт EKF"
    _attr_icon = "mdi:remote"
    _attr_event_types = EVENT_TYPES
    _attr_device_class = "button"

    def __init__(self, entry):
        self.receiver = entry.runtime_data.receiver
        self._attr_unique_id = f"{entry.entry_id}_universal_remote"
        self._attr_suggested_object_id = "ekf_universal_remote"

    @property
    def available(self):
        return self.receiver.enabled and self.receiver.last_error is None

    @property
    def extra_state_attributes(self):
        return {"receiver_enabled": self.receiver.enabled, "validated": self.receiver.validated,
                "last_received": self.receiver.last_received, "last_error": self.receiver.last_error}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        def update(name):
            if name is not None:
                self._trigger_event(name, {"receiver": "EKF"})
            self.async_write_ha_state()
        self.async_on_remove(self.receiver.add_callback(update))
