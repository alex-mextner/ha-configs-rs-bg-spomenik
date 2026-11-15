"""Observable readiness and dispatch timing, without claiming physical latency."""
from homeassistant.components.sensor import SensorEntity

from . import DOMAIN


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    async_add_entities([AnnouncementStatus(hass.data[DOMAIN])])


class AnnouncementStatus(SensorEntity):
    _attr_name = "Remote announcements"
    _attr_unique_id = "remote_announcements_status"
    _attr_should_poll = False
    _attr_icon = "mdi:account-voice"

    def __init__(self, manager):
        self.manager = manager

    @property
    def native_value(self):
        return self.manager.status

    @property
    def extra_state_attributes(self):
        return self.manager.public_status

    async def async_added_to_hass(self):
        self.manager.listeners.add(self.async_write_ha_state)

    async def async_will_remove_from_hass(self):
        self.manager.listeners.discard(self.async_write_ha_state)
