# Copyright (c) 2026 Alex Ultra
# SPDX-License-Identifier: MIT
"""Persistent watering alerts accompanying the unmodified Adaptive Plant integration."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import discovery
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .state_machine import evaluate, finite

DOMAIN = "plant_care_alerts"
RETRY_DELAYS = (60, 300, 900, 3600)
_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN, default={}): vol.Schema(
            {
                vol.Optional("confirmation_hours", default=6): vol.All(
                    vol.Coerce(float), vol.Range(min=0.1, max=48)
                ),
                vol.Optional("overdue_hours", default=24): vol.All(
                    vol.Coerce(float), vol.Range(min=1, max=168)
                ),
                vol.Optional("stale_minutes", default=20): vol.All(
                    vol.Coerce(float), vol.Range(min=5, max=1440)
                ),
                vol.Optional("freshness_sources", default={}): {
                    cv.entity_id: cv.entity_id
                },
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def timestamp(value: Any) -> float | None:
    """Parse a Home Assistant date-time without guessing missing values."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    parsed = dt_util.parse_datetime(str(value))
    return parsed.timestamp() if parsed else None


def needs_notification(record: Any) -> bool:
    """Check whether this severity still needs a delivery acknowledgement."""
    last = record.get("notified_status")
    return (record["status"] == "due" and last is None) or (
        record["status"] == "overdue" and last != "overdue"
    )


def acknowledge_delivery(record: dict[str, Any], status: str) -> None:
    """Record delivery, or allow a new warning after a fault cleared the phone tag."""
    # A completed clear removed the phone tag even if the sensor recovered
    # while notify awaited delivery. Re-arm that current warning for retry.
    if status == "sensor_error":
        record["notified_status"] = None
        record["delivery_attempts"] = 0
    elif record["status"] == status:
        record["notified_status"] = status
        record["delivery_attempts"] = 0


async def async_setup(hass: Any, config: Any) -> bool:
    """Register the companion sensor, persistence and internal delivery services."""
    manager = CareManager(hass, config[DOMAIN])
    hass.data[DOMAIN] = manager
    await manager.load()
    await discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config)
    manager.unsub_timer = async_track_time_interval(
        hass, manager.async_refresh, timedelta(minutes=1)
    )

    async def source_changed(event: Any) -> None:
        """Handle an integration lifecycle event."""
        if event.data.get("entity_id") in manager.watched:
            await manager.async_refresh()

    manager.unsub_state = hass.bus.async_listen(EVENT_STATE_CHANGED, source_changed)

    async def registry_changed(_event: Any) -> None:
        """Handle an integration lifecycle event."""
        manager.button_cache.clear()

    manager.unsub_registry = hass.bus.async_listen(
        "entity_registry_updated", registry_changed
    )

    async def stop(_event: Any) -> None:
        """Handle an integration lifecycle event."""
        manager.unsub_timer()
        manager.unsub_state()
        manager.unsub_registry()
        await manager.store.async_save(manager.records)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    hass.services.async_register(DOMAIN, "refresh", manager.async_refresh)

    async def acknowledge(call: Any) -> None:
        """Handle an integration lifecycle event."""
        for key, entity in manager.entities.items():
            if entity.entity_id == call.data["entity_id"]:
                record = manager.records[key]
                acknowledge_delivery(record, call.data["status"])
                manager.store.async_delay_save(lambda: manager.records, 1)
                break

    hass.services.async_register(
        DOMAIN,
        "acknowledge",
        acknowledge,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_id,
                vol.Required("status"): vol.In(["due", "overdue", "sensor_error"]),
            }
        ),
    )
    return True


class CareManager:
    """Connect upstream plant configuration to persistent, guarded alerts."""

    def __init__(self, hass: Any, config: Any) -> None:
        """Initialize the adapter without changing upstream plant state."""
        self.hass, self.config = hass, config
        self.store = Store(hass, 1, DOMAIN)
        self.records, self.entities, self.details = {}, {}, {}
        self.button_cache, self.pending_initial, self.last_attempt = {}, {}, {}
        self.add_entities = None
        self.watched = set()
        self.active_plants = set()
        self.busy = False
        self.settings = {
            "confirm_seconds": config["confirmation_hours"] * 3600,
            "overdue_seconds": config["overdue_hours"] * 3600,
            "stale_seconds": config["stale_minutes"] * 60,
        }

    async def load(self) -> None:
        """Restore alert timing through the supported Home Assistant storage API."""
        data = await self.store.async_load()
        self.records = data if isinstance(data, dict) else {}

    def _buttons(self, entry_id: Any) -> dict[str, str]:
        """Resolve stable button unique IDs and cache the registry lookup."""
        if entry_id in self.button_cache:
            return self.button_cache[entry_id]
        registry = er.async_get(self.hass)
        result = {}
        for entry in er.async_entries_for_config_entry(registry, entry_id):
            for suffix in ("mark_watered", "snooze_watering"):
                if entry.unique_id == f"{entry_id}_{suffix}":
                    result[suffix] = entry.entity_id
        self.button_cache[entry_id] = result
        return result

    def _source_reading(self, source_id: str | None) -> tuple[Any, Any, Any]:
        """Return source value, freshness entity and physical sample timestamp."""
        source = self.hass.states.get(source_id) if source_id else None
        fresh_id = self.config["freshness_sources"].get(source_id, source_id)
        fresh = self.hass.states.get(fresh_id) if fresh_id else None
        stamp = None
        if fresh is not None:
            stamp = timestamp(fresh.attributes.get("sampled_at")) or timestamp(
                fresh.last_reported
            )
        valid = (
            source is not None and fresh is not None and finite(fresh.state) is not None
        )
        return source.state if valid else None, fresh_id, stamp

    @staticmethod
    def _scheduled_time(value: Any) -> float | None:
        """Resolve a schedule-only plant's watering day at local 08:00."""
        if not value:
            return None
        try:
            day = datetime.fromisoformat(value).date()
            return datetime.combine(day, time(8), dt_util.DEFAULT_TIME_ZONE).timestamp()
        except (ValueError, TypeError):
            return None

    def _reading(self, plant: Any, now: Any) -> dict[str, Any]:
        """Combine moisture with independently verified source freshness."""
        buttons = self._buttons(plant.entry_id)
        source_id = plant.moisture_sensor
        moisture, fresh_id, stamp = self._source_reading(source_id)
        reading = {
            "has_sensor": bool(source_id),
            "source_identity": source_id,
            "moisture": moisture,
            "sample_at": stamp,
            "dry_threshold": plant.dry_threshold,
            "next_watering_at": self._scheduled_time(plant.next_watering),
        }
        for action, field in [
            ("mark_watered", "watered_at"),
            ("snooze_watering", "snoozed_at"),
        ]:
            entity_id = buttons.get(action)
            state = self.hass.states.get(entity_id) if entity_id else None
            reading[field] = timestamp(state.state) if state else None
        old = self.records.get(plant.entry_id, {})
        if (
            not source_id
            and "watered_date" in old
            and plant.last_watered != old.get("watered_date")
        ):
            reading["watered_at"] = now
        self.watched.update(x for x in [source_id, fresh_id, *buttons.values()] if x)
        self.details[plant.entry_id] = {
            "plant_care": True,
            "plant_name": plant.plant_name,
            "entry_id": plant.entry_id,
            "source_entity": source_id,
            "freshness_entity": fresh_id,
            "stale_seconds": self.settings["stale_seconds"],
            "picture": plant.image_path if plant.enable_image else None,
            "last_watered": plant.last_watered,
            "next_watering": plant.next_watering,
            "button_water": buttons.get("mark_watered"),
            "button_snooze": buttons.get("snooze_watering"),
            "confirmation_hours": self.config["confirmation_hours"],
            "overdue_after_hours": self.config["overdue_hours"],
        }
        return reading

    def emit_transition(self, entry_id: Any, previous: Any) -> None:
        """Publish a change; delivery remains pending until acknowledged."""
        current = self.records[entry_id]
        status = current["status"]
        notify = needs_notification(current)
        if notify:
            self.last_attempt[entry_id] = dt_util.utcnow().timestamp()
            current["delivery_attempts"] = current.get("delivery_attempts", 0) + 1
        if status == "ok" and current.get("due_since") is None:
            current["notified_status"] = None
            current["delivery_attempts"] = 0
        self.hass.bus.async_fire(
            "plant_care_status_changed",
            {
                "entity_id": self.entities[entry_id].entity_id,
                "plant_name": self.details[entry_id]["plant_name"],
                "status": status,
                "previous": previous,
                "notify": notify,
            },
        )
        self.store.async_delay_save(lambda: self.records, 10)

    def _refresh_plant(self, entry_id: str, plant: Any, now: float) -> bool:
        """Update one loaded plant, registering its entity on first discovery."""
        from .sensor import PlantCareSensor  # noqa: PLC0415 - avoids platform cycle

        was_active = entry_id in self.active_plants
        self.active_plants.add(entry_id)
        old = self.records.get(entry_id, {})
        old_details = self.details.get(entry_id)
        reading = self._reading(plant, now)
        current = evaluate(old, reading, now, self.settings)
        current["watered_date"] = plant.last_watered
        self.records[entry_id] = current
        meaningful = {k: v for k, v in old.items() if k != "updated_at"} != {
            k: v for k, v in current.items() if k != "updated_at"
        } or old_details != self.details[entry_id]
        if entry_id not in self.entities:
            entity = PlantCareSensor(self, entry_id)
            self.entities[entry_id] = entity
            self.pending_initial[entry_id] = old.get("status")
            self.add_entities([entity])
        else:
            self._publish_existing(
                entry_id, old, now, write_state=meaningful or not was_active
            )
        return meaningful

    def _publish_existing(
        self, entry_id: str, old: dict[str, Any], now: float, *, write_state: bool
    ) -> None:
        """Publish state changes and retry only unacknowledged delivery."""
        entity = self.entities[entry_id]
        if entity.hass is None:
            return
        if write_state:
            entity.async_write_ha_state()
        current = self.records[entry_id]
        attempt = max(0, current.get("delivery_attempts", 0) - 1)
        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
        retry_ready = now - self.last_attempt.get(entry_id, 0) >= delay
        if old.get("status") != current["status"] or (
            needs_notification(current) and retry_ready
        ):
            self.emit_transition(entry_id, old.get("status"))

    async def _remove_missing(self, entry_id: str) -> bool:
        """Preserve an options reload, but remove a truly deleted plant."""
        was_active = entry_id in self.active_plants
        self.active_plants.discard(entry_id)
        if self.hass.config_entries.async_get_entry(entry_id) is not None:
            entity = self.entities[entry_id]
            if was_active and getattr(entity, "hass", None) is not None:
                entity.async_write_ha_state()
            return False
        entity = self.entities.pop(entry_id)
        self.hass.bus.async_fire("plant_care_removed", {"entity_id": entity.entity_id})
        await entity.async_remove()
        for records in (
            self.records,
            self.details,
            self.button_cache,
            self.last_attempt,
            self.pending_initial,
        ):
            records.pop(entry_id, None)
        return True

    async def async_refresh(self, _event: Any = None) -> None:
        """Refresh loaded plants and preserve timers across options reloads."""
        if self.busy or self.add_entities is None:
            return
        plants = self.hass.data.get("adaptive_plant")
        if not isinstance(plants, dict):
            return
        self.busy = True
        try:
            now = dt_util.utcnow().timestamp()
            changed = False
            self.watched = set()
            for entry_id, plant in list(plants.items()):
                if hasattr(plant, "moisture_sensor"):
                    changed = self._refresh_plant(entry_id, plant, now) or changed
            for entry_id in set(self.entities) - set(plants):
                changed = await self._remove_missing(entry_id) or changed
            if changed:
                self.store.async_delay_save(lambda: self.records, 10)
        except Exception:
            _LOGGER.exception("Failed to update plant care alerts")
        finally:
            self.busy = False
