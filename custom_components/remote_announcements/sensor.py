"""Observable readiness and dispatch timing, without claiming physical latency."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity

from . import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

    from . import Manager


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Expose the shared manager readiness and measured controller status."""
    del config, discovery_info
    async_add_entities([AnnouncementStatus(hass.data[DOMAIN])])


class AnnouncementStatus(SensorEntity):
    """Report provider readiness without claiming physical speaker latency."""

    _attr_name = "Remote announcements"
    _attr_unique_id = "remote_announcements_status"
    _attr_should_poll = False
    _attr_icon = "mdi:account-voice"

    def __init__(self, manager: Manager) -> None:
        """Store the integration manager without adding polling."""
        self.manager = manager

    @property
    def native_value(self) -> str:
        """Return the current readiness or dispatch result."""
        return self.manager.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return nonsecret cache and timing details."""
        return self.manager.public_status

    async def async_added_to_hass(self) -> None:
        """Subscribe to manager status updates."""
        self.manager.listeners.add(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Detach the status listener on entity removal."""
        self.manager.listeners.discard(self.async_write_ha_state)
