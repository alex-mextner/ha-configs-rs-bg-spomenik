"""Select entity for choosing a RuTracker.org search result."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DOMAIN, SIGNAL_RESULTS_UPDATED, SIGNAL_SELECTION_UPDATED

NO_RESULTS = "No results"


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,  # noqa: ARG001 - Required HA platform setup signature.
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,  # noqa: ARG001 - HA signature.
) -> None:
    """Set up the RuTracker.org result selector."""
    async_add_entities([RuTrackerResultSelect(hass)])


class RuTrackerResultSelect(SelectEntity):
    """Select one of the current RuTracker.org results."""

    _attr_name = "RuTracker.org search result"
    _attr_unique_id = "rutracker_transmission_search_result"
    _attr_icon = "mdi:playlist-check"

    def __init__(self, hass: HomeAssistant) -> None:
        """Retain the Home Assistant runtime used for result updates."""
        self._hass = hass

    @property
    def options(self) -> list[str]:
        """Return current result labels or the established empty label."""
        results = _results(self._hass)
        if not results:
            return [NO_RESULTS]
        return [_option_for(item) for item in results]

    @property
    def current_option(self) -> str | None:
        """Resolve the selected result, falling back to the first available label."""
        options = self.options
        selected = str(self._hass.data.get(DOMAIN, {}).get("selected_result_id") or "")
        if selected:
            for option in options:
                if option.startswith(f"{selected} |"):
                    return option
        return options[0] if options else None

    async def async_select_option(self, option: str) -> None:
        """Select a search result by its option label."""
        if option == NO_RESULTS:
            return
        match = re.match(r"^(rt-\d+)\s+\|", option)
        if not match:
            return
        runtime = self._hass.data[DOMAIN]
        runtime["selected_result_id"] = match.group(1)
        runtime.setdefault("last_response", {})["selected_result_id"] = match.group(1)
        async_dispatcher_send(self._hass, SIGNAL_SELECTION_UPDATED)
        async_dispatcher_send(self._hass, SIGNAL_RESULTS_UPDATED)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to result updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self._hass, SIGNAL_RESULTS_UPDATED, self._handle_update
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self._hass, SIGNAL_SELECTION_UPDATED, self._handle_update
            )
        )
        self._handle_update()

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


def _results(hass: HomeAssistant) -> list[dict[str, Any]]:
    runtime = hass.data.get(DOMAIN, {})
    payload = runtime.get("last_response", {})
    return list(payload.get("results") or [])


def _option_for(item: dict[str, Any]) -> str:
    status = "Ready" if item.get("downloadable") else "Disabled"
    seeders = item.get("seeders")
    seeders_text = "?" if seeders is None else str(seeders)
    size = str(item.get("size") or "?")
    title = _compact(str(item.get("title") or ""), 82)
    return f"{item.get('id')} | {status} | {seeders_text} seeds | {size} | {title}"


def _compact(value: str, limit: int) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
