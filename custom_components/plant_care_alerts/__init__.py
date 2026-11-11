# Copyright (c) 2026 Alex Ultra
# SPDX-License-Identifier: MIT
"""Persistent watering alerts accompanying the unmodified Adaptive Plant integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import discovery
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.service import async_get_config_entry, async_register_admin_service
from homeassistant.util import dt as dt_util

from .analytics import analyze
from .photo import persist_photo
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
                vol.Optional("user_reports", default={}): {cv.entity_id: cv.string},
                vol.Optional("calibration_sources", default={}): {
                    cv.entity_id: vol.Schema({
                        vol.Required("dry"): cv.entity_id,
                        vol.Required("wet"): cv.entity_id,
                    })
                },
                vol.Optional("wet_hours", default=72): vol.All(vol.Coerce(float), vol.Range(min=1, max=312)),
                vol.Optional("wet_percent", default=90): vol.All(vol.Coerce(float), vol.Range(min=1, max=100)),
                vol.Optional("voice_toggle", default="input_boolean.plant_care_voice_enabled"): cv.entity_id,
                vol.Optional("voice_speaker", default="media_player.yandex_station_m00zrw3008rajg"): cv.entity_id,
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
    photo_lock = asyncio.Lock()

    async def set_photo(call: Any) -> None:
        """Save a new photo and publish the new companion picture immediately."""
        if not call.context.user_id:
            raise Unauthorized(context=call.context)
        async with photo_lock:
            entry_id = call.data["entry_id"]
            entry = async_get_config_entry(hass, "adaptive_plant", entry_id)
            plant = hass.data.get("adaptive_plant", {}).get(entry_id)
            if plant is None:
                raise HomeAssistantError("Plant entry is unavailable")
            photo_before = tuple(
                entry.options.get(key, entry.data.get(key))
                for key in ("image_path", "enable_image")
            )
            try:
                image = await hass.async_add_executor_job(
                    persist_photo, hass, call.data["file_id"]
                )
            except (ValueError, OSError) as err:
                raise HomeAssistantError("Invalid plant image upload") from err
            if (async_get_config_entry(hass, "adaptive_plant", entry_id) is not entry
                    or hass.data.get("adaptive_plant", {}).get(entry_id) is not plant):
                raise HomeAssistantError("Plant entry is unavailable")
            photo_now = tuple(
                entry.options.get(key, entry.data.get(key))
                for key in ("image_path", "enable_image")
            )
            if photo_now != photo_before:
                raise HomeAssistantError("Plant photo changed during upload; try again")
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, "enable_image": True, "image_path": image}
            )
            if entry_id in manager.details:
                manager.details[entry_id]["picture"] = image
                entity = manager.entities.get(entry_id)
                if entity is not None and getattr(entity, "hass", None) is not None:
                    entity.async_write_ha_state()

    async_register_admin_service(
        hass, DOMAIN, "set_photo", set_photo,
        schema=vol.Schema({
            vol.Required("entry_id"): vol.All(str, vol.Length(min=1)),
            vol.Required("file_id"): vol.All(str, vol.Length(min=1)),
        }),
    )
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
    async def snooze(call: Any) -> None:
        """Pause only companion reminders; do not touch upstream schedules."""
        for key, entity in manager.entities.items():
            if entity.entity_id == call.data["entity_id"]:
                now = dt_util.utcnow().timestamp()
                record = manager.records[key]
                record["snoozed_until"] = now + 86400
                record["notified_status"] = None
                await manager.async_refresh()
                manager.store.async_delay_save(lambda: manager.records, 1)
                break

    hass.services.async_register(DOMAIN, "snooze", snooze,
                                 schema=vol.Schema({vol.Required("entity_id"): cv.entity_id}))
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
        self.history_loaded = set()
        self.history_attempts = {}
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
            for suffix in ("mark_watered", "snooze_watering", "current_moisture", "last_watered"):
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

    async def _load_history(self, entry_id: str, plant: Any, now: float) -> None:
        """Read bounded recorder history once per source, retrying failed startup."""
        source = plant.moisture_sensor
        raw_id = self.config.get("freshness_sources", {}).get(source, source)
        record = self.records.setdefault(entry_id, {})
        identity = (entry_id, raw_id)
        if "adc_identity" in record and record["adc_identity"] != raw_id:
            record["adc_samples"] = []
            record.pop("adc_calibration", None)
            self.history_loaded.discard(identity)
            self.history_attempts.pop(identity, None)
        record["adc_identity"] = raw_id
        if not raw_id or source not in self.config.get("calibration_sources", {}):
            return
        if identity in self.history_loaded or now - self.history_attempts.get(identity, 0) < 900:
            return
        self.history_attempts[identity] = now
        try:
            from homeassistant.components.recorder import get_instance, history

            end = dt_util.utcnow()
            states = await get_instance(self.hass).async_add_executor_job(
                history.get_significant_states, self.hass, end - timedelta(days=14),
                end, [raw_id], None, False, False, False, False
            )
            points = []
            for state in states.get(raw_id, []):
                stamp = timestamp(state.attributes.get("sampled_at"))
                value = finite(state.state)
                if stamp is not None and value is not None:
                    points.append([stamp, value])
            record = self.records.setdefault(entry_id, {})
            record["adc_samples"] = record.get("adc_samples", []) + points
            self.history_loaded.add(identity)
        except Exception:
            _LOGGER.exception("Unable to load plant ADC recorder history for %s", raw_id)

    @staticmethod
    def _voice_hour(after: float, now: float) -> float:
        """Find the first future whole local hour in the 08:00–21:00 window."""
        candidate = dt_util.as_local(datetime.fromtimestamp(max(after, now + 0.001), UTC))
        if candidate.minute or candidate.second or candidate.microsecond:
            candidate = candidate.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        if candidate.hour < 8:
            candidate = candidate.replace(hour=8)
        elif candidate.hour > 21:
            candidate = (candidate + timedelta(days=1)).replace(hour=8)
        return candidate.timestamp()

    def _analytics(self, current: dict, plant: Any, reading: dict, now: float) -> None:
        """Update estimates without changing upstream watering or fertilizing."""
        source = plant.moisture_sensor
        raw_id = self.config.get("freshness_sources", {}).get(source, source)
        raw = self.hass.states.get(raw_id) if raw_id else None
        calibration_ids = self.config.get("calibration_sources", {}).get(source, {})
        dry_state = self.hass.states.get(calibration_ids.get("dry")) if calibration_ids else None
        wet_state = self.hass.states.get(calibration_ids.get("wet")) if calibration_ids else None
        dry = finite(dry_state.state) if dry_state else None
        wet = finite(wet_state.state) if wet_state else None
        calibration = [dry, wet] if dry is not None and wet is not None and wet > dry else None
        self.watched.update(x for x in [*calibration_ids.values(), self.config.get("voice_toggle"),
                                        self.config.get("voice_speaker")] if x)
        points = current.get("adc_samples", [])
        if "adc_identity" in current and current["adc_identity"] != raw_id:
            points = []
            current.pop("adc_calibration", None)
        previous_calibration = current.get("adc_calibration")
        if calibration is not None:
            if previous_calibration is not None and previous_calibration != calibration:
                points = []
            current["adc_calibration"] = calibration
        current["adc_identity"] = raw_id
        if raw is not None:
            stamp = timestamp(raw.attributes.get("sampled_at"))
            value = finite(raw.state)
            if stamp is not None and value is not None and stamp <= now + 60:
                points = points + [[stamp, value]]
        unique = {}
        for stamp, value in points:
            if now - 14 * 86400 <= stamp <= now + 60:
                unique.setdefault(stamp, value)
        current["adc_samples"] = [[stamp, unique[stamp]] for stamp in sorted(unique)]
        prior_at = current.get("last_watering_at")
        prior_source = current.get("last_watering_source", "unknown")
        report = self.config.get("user_reports", {}).get(source)
        # A user supplied a day, not an invented time. A later auto event wins.
        try:
            report_day = datetime.fromisoformat(report).date() if report else None
        except (ValueError, TypeError):
            report_day = None
        analysis = analyze(current, current["adc_samples"], now,
                           calibration=calibration, dry_threshold=plant.dry_threshold,
                           stale_seconds=self.settings["stale_seconds"],
                           wet_hours=self.config.get("wet_hours", 72),
                           wet_percent=self.config.get("wet_percent", 90))
        if raw is None or finite(raw.state) is None or finite(reading.get("moisture")) is None:
            analysis.update(wet_risk="unknown", wet_since=None, forecast_status="stale",
                            forecast_due_at=None, forecast_voice_at=None)
        detected = analysis.get("last_watering_at")
        def observed_day(value: Any) -> Any:
            if isinstance(value, (int, float)):
                return dt_util.as_local(datetime.fromtimestamp(value, UTC)).date()
            try:
                return datetime.fromisoformat(str(value)).date() if value else None
            except (ValueError, TypeError):
                return None

        detected_day = observed_day(detected)
        if report_day and (detected_day is None or report_day > detected_day or (
            report_day == detected_day and analysis.get("last_watering_source") == "auto"
        )):
            analysis["last_watering_at"] = report_day.isoformat()
            analysis["last_watering_source"] = "user_report"
        elif detected is None and prior_at:
            analysis["last_watering_at"], analysis["last_watering_source"] = prior_at, prior_source
        if getattr(plant, "last_watered", None):
            try:
                upstream_day = datetime.fromisoformat(str(plant.last_watered)).date()
                prior_day = observed_day(analysis.get("last_watering_at"))
                if (upstream_day <= dt_util.as_local(datetime.fromtimestamp(now, UTC)).date()
                        and (prior_day is None or upstream_day > prior_day)):
                    analysis["last_watering_at"] = upstream_day.isoformat()
                    analysis["last_watering_source"] = "upstream"
            except (ValueError, TypeError):
                pass
        current.update(analysis)
        forecast = current.get("forecast_due_at")
        if forecast is not None:
            forecast += self.settings["confirm_seconds"]
            current["forecast_due_at"] = forecast
        current["forecast_voice_at"] = self._voice_hour(forecast + self.settings["overdue_seconds"], now) if forecast else None
        due = current.get("due_since") or forecast
        toggle = self.hass.states.get(self.config.get("voice_toggle", "input_boolean.plant_care_voice_enabled"))
        speaker = self.hass.states.get(self.config.get("voice_speaker", "media_player.yandex_station_m00zrw3008rajg"))
        reason = None
        if toggle is None or toggle.state != "on":
            reason = "Голосовые напоминания выключены"
        elif speaker is None or speaker.state in ("unavailable", "unknown"):
            reason = "Колонка недоступна"
        elif current.get("status") == "sensor_error":
            reason = "Нет свежих показаний датчика"
        elif not due:
            reason = "Нет достоверной оценки времени полива"
        current["voice_unavailable_reason"] = reason
        current["voice_next_at"] = (self._voice_hour(max(due + self.settings["overdue_seconds"], current.get("snoozed_until") or 0), now)
                                    if reason is None else None)

    def _refresh_plant(self, entry_id: str, plant: Any, now: float) -> bool:
        """Update one loaded plant, registering its entity on first discovery."""
        from .sensor import PlantCareSensor  # noqa: PLC0415 - avoids platform cycle

        was_active = entry_id in self.active_plants
        self.active_plants.add(entry_id)
        old = self.records.get(entry_id, {})
        old_details = self.details.get(entry_id)
        reading = self._reading(plant, now)
        current = evaluate(old, reading, now, self.settings)
        self._analytics(current, plant, reading, now)
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
                    await self._load_history(entry_id, plant, now)
                    changed = self._refresh_plant(entry_id, plant, now) or changed
            for entry_id in set(self.entities) - set(plants):
                changed = await self._remove_missing(entry_id) or changed
            if changed:
                self.store.async_delay_save(lambda: self.records, 10)
        except Exception:
            _LOGGER.exception("Failed to update plant care alerts")
        finally:
            self.busy = False
