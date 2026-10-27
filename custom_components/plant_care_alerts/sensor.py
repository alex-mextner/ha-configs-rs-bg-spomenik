# Copyright (c) 2026 Alex Ultra
# SPDX-License-Identifier: MIT
"""One alert sensor per upstream plant, discovered without YAML duplication."""

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.start import async_at_started

from . import DOMAIN


async def async_setup_platform(
    hass: Any, config: Any, async_add_entities: Any, discovery_info: Any = None
) -> None:
    """Attach discovered plant sensors to the Home Assistant platform."""
    del config, discovery_info  # Required Home Assistant platform signature.
    manager = hass.data[DOMAIN]
    manager.add_entities = async_add_entities
    await manager.async_refresh()


class PlantCareSensor(SensorEntity):
    """Expose one plant alert with controls and photo metadata."""

    _attr_should_poll = False
    _attr_icon = "mdi:flower"

    def __init__(self, manager: Any, entry_id: Any) -> None:
        """Initialize the adapter without changing upstream plant state."""
        self.manager, self.plant_id = manager, entry_id
        self._attr_unique_id = f"{entry_id}_care_alert"
        self._attr_name = f"{manager.details[entry_id]['plant_name']} — полив"

    async def async_added_to_hass(self) -> None:
        """Publish initial state before emitting its delivery event."""
        await super().async_added_to_hass()

        async def publish_initial(_hass: Any) -> None:
            """Wait until startup automation listeners can receive the transition."""
            self.async_write_ha_state()
            previous = self.manager.pending_initial.pop(self.plant_id, None)
            self.manager.emit_transition(self.plant_id, previous)

        self.async_on_remove(async_at_started(self.hass, publish_initial))

    @property
    def available(self) -> bool:
        """Suppress reminders while the upstream entry is unloaded or disabled."""
        return self.plant_id in self.manager.active_plants

    @property
    def native_value(self) -> str:
        """Return the current watering-alert status."""
        return self.manager.records[self.plant_id]["status"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose card metadata without copying internal sample history."""
        record = self.manager.records[self.plant_id]
        return {
            **self.manager.details[self.plant_id],
            **{
                k: record.get(k)
                for k in (
                    "moisture",
                    "raw_moisture",
                    "sample_count",
                    "last_sample_at",
                    "dry_since",
                    "due_since",
                    "overdue_hours",
                    "snoozed_until",
                    "ack_until",
                    "dry_threshold",
                    "recovery_threshold",
                )
            },
        }
