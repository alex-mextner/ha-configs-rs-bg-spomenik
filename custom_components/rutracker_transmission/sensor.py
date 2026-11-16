"""Sensor for the last RuTracker.org search results."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DOMAIN, SIGNAL_RESULTS_UPDATED


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,  # noqa: ARG001 - Required HA platform setup signature.
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,  # noqa: ARG001 - HA signature.
) -> None:
    """Set up the RuTracker.org results sensor."""
    async_add_entities([RuTrackerResultsSensor(hass)])


class RuTrackerResultsSensor(SensorEntity):
    """Expose the last search result set."""

    _attr_name = "RuTracker.org search results"
    _attr_unique_id = "rutracker_transmission_search_results"
    _attr_icon = "mdi:movie-search"

    def __init__(self, hass: HomeAssistant) -> None:
        """Retain the Home Assistant runtime used for result updates."""
        self._hass = hass
        self._attr_native_value = 0
        self._attr_extra_state_attributes = {
            "query": "",
            "source": "",
            "status": "idle",
            "message": "",
            "progress_current": 0,
            "progress_total": 0,
            "selected_result_id": "",
            "results": [],
            "disabled": [],
            "notes": [],
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to result updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self._hass, SIGNAL_RESULTS_UPDATED, self._handle_update
            )
        )
        self._handle_update()

    @callback
    def _handle_update(self) -> None:
        runtime = self._hass.data.get(DOMAIN, {})
        payload = runtime.get("last_response", {})
        results = payload.get("results", [])
        self._attr_native_value = len(results)
        self._attr_extra_state_attributes = {
            "query": payload.get("query", ""),
            "source": payload.get("source", ""),
            "searched_at": payload.get("searched_at"),
            "status": payload.get("status", "idle"),
            "message": payload.get("message", ""),
            "progress_current": payload.get("progress_current", 0),
            "progress_total": payload.get("progress_total", 0),
            "selected_result_id": payload.get("selected_result_id", ""),
            "results": results,
            "disabled": [item for item in results if not item.get("downloadable")],
            "notes": payload.get("notes", []),
        }
        self.async_write_ha_state()
