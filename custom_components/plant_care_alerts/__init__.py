# Copyright (c) 2026 Alex Ultra
# SPDX-License-Identifier: MIT
"""Persistent watering alerts accompanying the unmodified Adaptive Plant integration."""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import UTC, datetime, time, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.input_number import InputNumber
from homeassistant.components.logbook import async_log_entry
from homeassistant.components.recorder import get_instance, history
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.const import (
    __version__ as ha_version,
)
from homeassistant.core import Context, CoreState, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import discovery
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.service import (
    async_get_config_entry,
    async_register_admin_service,
)
from homeassistant.util import dt as dt_util

from .analytics import RETENTION_SECONDS, analyze, normalize_adc_points
from .calibration_policy import ADC_MAX, canonical_digest
from .calibration_runtime import CalibrationRuntime, RuntimeHooks, collect_witnesses
from .const import (
    APPLY_CALIBRATION_SERVICE,
    CALIBRATION_STORE_KEY,
    DOMAIN,
    SUPPORTED_HA_VERSION,
)
from .persistence import PersistenceNotConfirmedError, async_save_confirmed, new_store
from .photo import persist_photo
from .reliability import UNCHANGED_SECONDS, assess
from .state_machine import evaluate, finite
from .voice import async_announce, configured_target, target_available
from .watering import WATERING_METADATA, record_report

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

MAX_SAMPLE_SKEW_SECONDS = 60
MAX_MOISTURE_INDEX = 100
VOICE_START_HOUR = 8
VOICE_LAST_HOUR = 21
RETRY_DELAYS = (60, 300, 900, 3600)
CLEAR_STATUSES = {"sensor_error", "ok", "checking", "snoozed"}
VOICE_METADATA_KEYS = (
    "forecast_voice_at",
    "voice_unavailable_reason",
    "voice_target",
    "voice_target_name",
    "voice_target_available",
    "voice_next_at",
)
_LOGGER = logging.getLogger(__name__)


def _validate_calibration_config(config: dict) -> dict:
    for source, options in config.get("automatic_calibration", {}).items():
        if options["original_dry"] >= options["original_wet"]:
            message = "Automatic calibration requires original_dry < original_wet"
            raise vol.Invalid(message, path=["automatic_calibration", source])
        if bool(options.get("reference_entity")) != bool(
            options.get("reference_protocol")
        ):
            message = (
                "reference_entity and reference_protocol must be configured together"
            )
            raise vol.Invalid(message, path=["automatic_calibration", source])
        if source not in config.get(
            "freshness_sources", {}
        ) or source not in config.get("calibration_sources", {}):
            message = "Automatic calibration requires raw-source and endpoint mappings"
            raise vol.Invalid(message, path=["automatic_calibration", source])
    return config


CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN, default={}): vol.All(
            vol.Schema(
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
                    vol.Optional("unchanged_hours", default=12): vol.All(
                        vol.Coerce(float), vol.Range(min=12, max=12)
                    ),
                    vol.Optional("freshness_sources", default={}): {
                        cv.entity_id: cv.entity_id
                    },
                    vol.Optional("user_reports", default={}): {
                        cv.entity_id: vol.Any(
                            cv.string,
                            vol.Schema(
                                {
                                    vol.Required("date"): cv.string,
                                    vol.Optional("precision", default="day"): vol.In(
                                        ["day", "approximate_day"]
                                    ),
                                    vol.Optional("note"): cv.string,
                                    vol.Optional("timezone"): cv.string,
                                }
                            ),
                        )
                    },
                    vol.Optional("calibration_sources", default={}): {
                        cv.entity_id: vol.Schema(
                            {
                                vol.Required("dry"): cv.entity_id,
                                vol.Required("wet"): cv.entity_id,
                            }
                        )
                    },
                    vol.Optional("automatic_calibration", default={}): {
                        cv.entity_id: vol.Schema(
                            {
                                vol.Optional("enabled", default=True): cv.boolean,
                                vol.Required("original_dry"): vol.All(
                                    vol.Coerce(float), vol.Range(min=0, max=ADC_MAX)
                                ),
                                vol.Required("original_wet"): vol.All(
                                    vol.Coerce(float), vol.Range(min=0, max=ADC_MAX)
                                ),
                                vol.Required("source_revision"): vol.All(
                                    cv.string, vol.Length(min=1)
                                ),
                                vol.Optional("reference_entity"): cv.entity_id,
                                vol.Optional("reference_protocol"): vol.All(
                                    cv.string, vol.Length(min=1)
                                ),
                            }
                        )
                    },
                    vol.Optional("wet_hours", default=72): vol.All(
                        vol.Coerce(float), vol.Range(min=1, max=312)
                    ),
                    vol.Optional("wet_percent", default=90): vol.All(
                        vol.Coerce(float), vol.Range(min=1, max=100)
                    ),
                    vol.Optional(
                        "voice_toggle", default="input_boolean.plant_care_voice_enabled"
                    ): cv.entity_id,
                    vol.Optional(
                        "voice_speaker",
                        default="media_player.yandex_station_m00zrw3008rajg",
                    ): cv.entity_id,
                }
            ),
            _validate_calibration_config,
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


def _clear_applies(record: dict[str, Any]) -> bool:
    return record.get("status") in CLEAR_STATUSES and not record.get(
        "calibration_confirmation_pending"
    )


def needs_clear(record: dict[str, Any]) -> bool:
    """Retry a pending phone clear while no current watering warning applies."""
    return _clear_applies(record) and bool(record.get("clear_pending"))


def acknowledge_delivery(record: dict[str, Any], status: str) -> None:
    """Record delivery, or allow a new warning after a fault cleared the phone tag."""
    # A completed clear removed the phone tag even if the sensor recovered
    # while notify awaited delivery. Re-arm that current warning for retry.
    if status == "sensor_error":
        record.pop("clear_pending", None)
        record["notified_status"] = None
        record["delivery_attempts"] = 0
    elif record["status"] == status:
        record["notified_status"] = status
        record["delivery_attempts"] = 0


async def _require_entity_control(
    hass: Any, context: Context, entity_id: str | None
) -> None:
    """Apply native entity control permissions without restricting automations."""
    if not context.user_id:
        return
    user = await hass.auth.async_get_user(context.user_id)
    if user is None:
        raise UnknownUser(context=context)
    if entity_id is None or not user.permissions.check_entity(
        entity_id, POLICY_CONTROL
    ):
        raise Unauthorized(
            context=context, entity_id=entity_id, permission=POLICY_CONTROL
        )


def _register_delivery_services(hass: Any, manager: CareManager) -> None:
    """Register report and delivery callbacks with their caller context."""

    async def refresh(_call: Any) -> None:
        """Finish any pending refresh before a service caller reads its report."""
        await manager.async_refresh(wait=True)

    hass.services.async_register(DOMAIN, "refresh", refresh)

    async def acknowledge(call: Any) -> None:
        """Handle an integration lifecycle event."""
        await _require_entity_control(hass, call.context, call.data["entity_id"])
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
        await _require_entity_control(hass, call.context, call.data["entity_id"])
        for key, entity in manager.entities.items():
            if entity.entity_id == call.data["entity_id"]:
                now = dt_util.utcnow().timestamp()
                record = manager.records[key]
                record["snoozed_until"] = now + 86400
                record["notified_status"] = None
                await manager.async_refresh(wait=True)
                manager.store.async_delay_save(lambda: manager.records, 1)
                break

    hass.services.async_register(
        DOMAIN,
        "snooze",
        snooze,
        schema=vol.Schema({vol.Required("entity_id"): cv.entity_id}),
    )

    async def announce(call: Any) -> None:
        target = manager.voice_target
        await _require_entity_control(hass, call.context, target)
        await async_announce(hass, target, call.data["message"], context=call.context)

    hass.services.async_register(
        DOMAIN,
        "announce",
        announce,
        schema=vol.Schema({vol.Required("message"): cv.string}),
    )


async def async_setup(hass: Any, config: Any) -> bool:
    """Register the companion sensor, persistence and internal delivery services."""
    manager = CareManager(hass, config[DOMAIN])
    hass.data[DOMAIN] = manager
    await manager.load()
    _register_calibration_service(hass, manager)
    await discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config)
    manager.unsub_timer = async_track_time_interval(
        hass, manager.async_background_refresh, timedelta(minutes=1)
    )

    @callback
    def capture_calibration_change(event: Any) -> None:
        """Retain exact helper events before asynchronous refresh coalescing."""
        manager.calibration.note_helper_event(event)

    manager.unsub_calibration = hass.bus.async_listen(
        EVENT_STATE_CHANGED, capture_calibration_change
    )

    async def source_changed(event: Any) -> None:
        """Handle an integration lifecycle event."""
        manager.note_source_event(event)
        entity_id = event.data.get("entity_id")
        if entity_id in manager.voice_entities:
            manager.refresh_voice_metadata()
        elif entity_id in manager.watched_sources:
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
        manager.calibration.stop_writes()
        manager.unsub_timer()
        manager.unsub_state()
        manager.unsub_registry()
        manager.unsub_calibration()
        if manager.unsub_calibration_service:
            manager.unsub_calibration_service()
        await manager.store.async_save(manager.records)
        await manager.calibration.async_save_final()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    _register_photo_service(hass, manager)

    _register_delivery_services(hass, manager)
    _register_reference_service(hass, manager)
    return True


def _register_calibration_service(hass: Any, manager: CareManager) -> None:
    """Register an owned admin action with explicit control of the wet helper."""
    name = APPLY_CALIBRATION_SERVICE
    if ha_version != SUPPORTED_HA_VERSION:
        manager.calibration.inhibit_writes("unsupported_runtime")
        return
    if hass.services.has_service(DOMAIN, name):
        manager.calibration.inhibit_writes("service_conflict")
        _LOGGER.error("Calibration write service already registered; writes disabled")
        return

    async def apply(call: Any) -> dict:
        entity_id = call.data["entity_id"]
        # The public admin wrapper runs first. Preserve the caller's context and
        # finish every authorization await before the final compare and effect.
        await _require_entity_control(hass, call.context, entity_id)
        if hass.data.get(DOMAIN) is not manager or hass.state is not CoreState.running:
            return {entity_id: {"applied": False}}
        if not manager.calibration.has_armed_target(
            entity_id, call.data["transaction_id"], call.context.id
        ):
            return {entity_id: {"applied": False}}
        component = hass.data.get(DATA_INSTANCES, {}).get("input_number")
        entity = component.get_entity(entity_id) if component else None
        if entity is None:
            return {entity_id: {"applied": False}}
        if (
            type(entity) is not InputNumber
            or getattr(entity.async_set_native_value, "__func__", None)
            is not InputNumber.async_set_native_value
        ):
            manager.calibration.inhibit_writes("unsupported_runtime")
            return {entity_id: {"applied": False}}
        entity.async_set_context(call.context)
        result = await manager.calibration.async_guarded_apply(entity, call)
        return {entity_id: result}

    async_register_admin_service(
        hass,
        DOMAIN,
        name,
        apply,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_id,
                vol.Required("transaction_id"): cv.string,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    registered = hass.services.async_services()[DOMAIN][name]

    @callback
    def remove() -> None:
        if hass.services.async_services().get(DOMAIN, {}).get(name) is registered:
            hass.services.async_remove(DOMAIN, name)

    manager.unsub_calibration_service = remove


def _register_reference_service(hass: Any, manager: CareManager) -> None:
    """Read an allowlisted reference; never accept arbitrary reference values."""

    async def record_reference(call: Any) -> None:
        for entry_id, entity in manager.entities.items():
            if entity.entity_id == call.data["entity_id"]:
                await manager.calibration.async_record_reference(
                    entry_id, dt_util.utcnow().timestamp()
                )
                await manager.async_refresh(wait=True)
                break

    async_register_admin_service(
        hass,
        DOMAIN,
        "record_reference",
        record_reference,
        schema=vol.Schema({vol.Required("entity_id"): cv.entity_id}),
    )


def _register_photo_service(hass: Any, manager: CareManager) -> None:
    """Register guarded uploads with an independent service lifetime lock."""
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
                error = "Plant entry is unavailable"
                raise HomeAssistantError(error)
            photo_before = tuple(
                entry.options.get(key, entry.data.get(key))
                for key in ("image_path", "enable_image")
            )
            try:
                image = await hass.async_add_executor_job(
                    persist_photo, hass, call.data["file_id"]
                )
            except (ValueError, OSError) as err:
                error = "Invalid plant image upload"
                raise HomeAssistantError(error) from err
            if (
                async_get_config_entry(hass, "adaptive_plant", entry_id) is not entry
                or hass.data.get("adaptive_plant", {}).get(entry_id) is not plant
            ):
                error = "Plant entry is unavailable"
                raise HomeAssistantError(error)
            photo_now = tuple(
                entry.options.get(key, entry.data.get(key))
                for key in ("image_path", "enable_image")
            )
            if photo_now != photo_before:
                error = "Plant photo changed during upload; try again"
                raise HomeAssistantError(error)
            hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, "enable_image": True, "image_path": image},
            )
            if entry_id in manager.details:
                manager.details[entry_id]["picture"] = image
                entity = manager.entities.get(entry_id)
                if entity is not None and getattr(entity, "hass", None) is not None:
                    entity.async_write_ha_state()

    async_register_admin_service(
        hass,
        DOMAIN,
        "set_photo",
        set_photo,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): vol.All(str, vol.Length(min=1)),
                vol.Required("file_id"): vol.All(str, vol.Length(min=1)),
            }
        ),
    )


async def async_setup_entry(hass: Any, entry: ConfigEntry) -> bool:
    """Attach persisted UI preferences to the existing YAML alert engine."""
    manager = hass.data[DOMAIN]
    manager.voice_entry = entry

    async def options_updated(_hass: Any, _entry: ConfigEntry) -> None:
        await manager.async_refresh()

    entry.async_on_unload(entry.add_update_listener(options_updated))
    await manager.async_refresh()
    return True


async def async_unload_entry(hass: Any, entry: ConfigEntry) -> bool:
    """Keep plant monitoring intact when notification preferences are unloaded."""
    manager = hass.data.get(DOMAIN)
    if manager is not None and manager.voice_entry is entry:
        manager.voice_entry = None
        await manager.async_refresh()
    return True


class CareManager:
    """Connect upstream plant configuration to persistent, guarded alerts."""

    def __init__(self, hass: Any, config: Any) -> None:
        """Initialize the adapter without changing upstream plant state."""
        self.hass, self.config = hass, config
        self.store = new_store(hass, DOMAIN)
        self.calibration = CalibrationRuntime(
            hass,
            new_store(hass, CALIBRATION_STORE_KEY),
            config,
            hooks=RuntimeHooks(
                context_factory=Context,
                log_event=self._log_calibration,
                clock=lambda: dt_util.utcnow().timestamp(),
                quality_provider=self._calibration_quality,
                identity_provider=self._calibration_source_identity,
                write_ready=lambda: self.hass.state is CoreState.running,
                read_persisted=lambda: self._read_persisted(CALIBRATION_STORE_KEY),
                confirm_witnesses=self._confirm_calibration_witnesses,
            ),
        )
        self.records, self.entities, self.details = {}, {}, {}
        self.button_cache, self.pending_initial, self.last_attempt = {}, {}, {}
        self.add_entities = None
        self.watched = set()
        self.active_plants = set()
        self.history_loaded = set()
        self.history_attempts = {}
        self.history_failures = {}
        self.busy = False
        self._refresh_pending = False
        self._calibration_write_pending = False
        self._calibration_write_active = False
        self._refresh_owner = None
        self._refresh_finished = asyncio.Event()
        self._refresh_finished.set()
        self._refresh_watched = set()
        self.voice_entry = None
        self.unsub_calibration_service = None
        self.settings = {
            "confirm_seconds": config["confirmation_hours"] * 3600,
            "overdue_seconds": config["overdue_hours"] * 3600,
            "stale_seconds": config["stale_minutes"] * 60,
            "unchanged_seconds": UNCHANGED_SECONDS,
        }

    @property
    def voice_target(self) -> str | None:
        """One setting drives the card and the delivery path."""
        return configured_target(self.voice_entry, self.config.get("voice_speaker"))

    @property
    def voice_entities(self) -> set[str]:
        """Inputs that only change voice metadata, never plant analytics."""
        return {
            entity_id
            for entity_id in (
                self.config.get(
                    "voice_toggle", "input_boolean.plant_care_voice_enabled"
                ),
                self.voice_target,
            )
            if entity_id
        }

    @property
    def watched_sources(self) -> set[str]:
        """Keep existing source subscriptions while recorder history is pending."""
        return self.watched | self._refresh_watched | self.calibration.watched_entities

    async def _read_persisted(self, key: str) -> dict | None:
        """Read through a new Store, excluding the writer's own pending memory."""
        return await new_store(self.hass, key).async_load()

    async def load(self) -> None:
        """Restore alert timing through the supported Home Assistant storage API."""
        data = await self.store.async_load()
        self.records = data if isinstance(data, dict) else {}
        await self.calibration.async_load(self._calibration_witnesses())

    def _calibration_witnesses(self) -> dict:
        """Keep actual progress and independent uncertainty fences for each source."""
        return collect_witnesses(self.records)

    async def _confirm_calibration_witnesses(
        self, entry_id: str, witnesses: dict | None
    ) -> None:
        """Confirm the independent witness before each effect, even when unchanged."""
        if witnesses is None or entry_id not in self.records:
            message = "Independent calibration witness is unavailable"
            raise PersistenceNotConfirmedError(message)
        snapshot = deepcopy(self.records)
        snapshot[entry_id]["calibration_witnesses"] = witnesses
        try:
            await async_save_confirmed(
                self.store, lambda: self._read_persisted(DOMAIN), snapshot
            )
        except Exception:
            self.calibration.inhibit_writes("witness_unavailable")
            raise
        # Other callbacks may update care fields while persistence awaits.
        # Confirm only this witness; never restore the detached care snapshot.
        self.records[entry_id]["calibration_witnesses"] = witnesses

    async def _persist_calibration_witnesses(self, entry_id: str) -> bool:
        """Synchronize new observed history; effect authorization uses confirmation."""
        witnesses = self.calibration.witnesses()
        record = self.records[entry_id]
        if witnesses is None or record.get("calibration_witnesses", {}) == witnesses:
            return False
        try:
            await self._confirm_calibration_witnesses(entry_id, witnesses)
        except Exception:
            _LOGGER.exception(
                "Calibration witness could not be persisted; writes disabled"
            )
            return False
        return True

    def _calibration_source_identity(self, source: str) -> str:
        """Bind history to the registry's raw-source identity and channel."""
        raw_id = self.config.get("freshness_sources", {}).get(source, source)
        entity = er.async_get(self.hass).async_get(raw_id) if raw_id else None
        return (
            f"{entity.platform}:{entity.unique_id}:{entity.id}"
            if entity
            else raw_id or ""
        )

    def _calibration_quality(self, entry_id: str, now: float) -> dict:
        """Recheck physical evidence and refresh source metadata after the executor."""
        plant = self.hass.data.get("adaptive_plant", {}).get(entry_id)
        if plant is None:
            return {"reliability_valid": False, "reliability_reason": "missing"}
        return assess(
            self.records.get(entry_id, {}),
            self._reading(plant, now),
            now,
            self.settings,
        )

    def _log_calibration(self, event: dict) -> None:
        """Mirror durable changes into native Logbook without a notification."""
        source = event["source"]
        for entry_id, bound in self.calibration.bindings.items():
            if bound != source or entry_id not in self.entities:
                continue
            name = self.details.get(entry_id, {}).get("plant_name", "Растение")
            kind = {
                "automatic": "Автоматическая калибровка",
                "rollback": "Откат калибровки",
                "manual": "Изменение настройки",
                "external_discovered": "Обнаружено внешнее изменение",
            }.get(event["kind"], "Калибровка")
            if event.get("reason") == "concurrent_change":
                kind = "Автоматическая запись при конфликте настроек"
            message = (
                f"{kind}: сухая точка {event['old_dry']:g} → {event['new_dry']:g}; "
                f"мокрая точка {event['old_wet']:g} → {event['new_wet']:g} ADC."
            )
            if event.get("reason") == "concurrent_change":
                message += (
                    " Одновременно изменялись настройки."
                    " Эта запись не подтверждает автоматическую калибровку."
                )
            if event["at"] is None:
                message += " Время изменения неизвестно."
            async_log_entry(
                self.hass,
                name,
                message,
                domain=DOMAIN,
                entity_id=self.entities[entry_id].entity_id,
            )

    def _buttons(self, entry_id: Any) -> dict[str, str]:
        """Resolve stable button unique IDs and cache the registry lookup."""
        if entry_id in self.button_cache:
            return self.button_cache[entry_id]
        registry = er.async_get(self.hass)
        result = {}
        for entry in er.async_entries_for_config_entry(registry, entry_id):
            for suffix in (
                "mark_watered",
                "snooze_watering",
                "current_moisture",
                "last_watered",
            ):
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
            # An explicit physical freshness source must supply its sample time.
            # A new HA report or restart cannot replace a missing/broken timestamp.
            if (
                source_id in self.config["freshness_sources"]
                or "sampled_at" in fresh.attributes
            ):
                stamp = timestamp(fresh.attributes.get("sampled_at"))
            else:
                stamp = timestamp(fresh.last_reported)
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
            "starting": self.hass.state is not CoreState.running,
            "measurement_missing": self._measurement_missing(source_id, fresh_id),
            "source_identity": source_id,
            "moisture": moisture,
            "sample_at": stamp,
            "raw_source_value": self._raw_source_value(source_id, fresh_id),
            "raw_source_identity": fresh_id if fresh_id != source_id else None,
            "dry_threshold": plant.dry_threshold,
            "next_watering_at": self._scheduled_time(plant.next_watering),
            **self.calibration.reading_fields(plant.entry_id),
        }
        for action, field in [
            ("mark_watered", "watered_at"),
            ("snooze_watering", "snoozed_at"),
        ]:
            entity_id = buttons.get(action)
            state = self.hass.states.get(entity_id) if entity_id else None
            reading[field] = timestamp(state.state) if state else None
        old = self.records.get(plant.entry_id, {})
        # A failed calibration Store must not let manual scaling erase a prior fault.
        reading.setdefault("calibration_current", self._calibration(source_id))
        reading.setdefault(
            "calibration_previous", old.get("reliability_calibration_basis")
        )
        reading["history_ready"] = self._history_ready(old, source_id)
        reading["recorder_ready"] = old.get(
            "value_history_source"
        ) == source_id and old.get("reliability_history_ready", False)
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
            "unchanged_seconds": self.settings["unchanged_seconds"],
            "picture": plant.image_path if plant.enable_image else None,
            "last_watered": plant.last_watered,
            "next_watering": plant.next_watering,
            "button_water": buttons.get("mark_watered"),
            "button_snooze": buttons.get("snooze_watering"),
            "confirmation_hours": self.config["confirmation_hours"],
            "overdue_after_hours": self.config["overdue_hours"],
        }
        return reading

    def _measurement_missing(self, source_id: str | None, fresh_id: str | None) -> bool:
        """Distinguish absent source states from invalid numeric evidence."""
        for entity_id in {source_id, fresh_id}:
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                return True
        return False

    def _raw_source_value(self, source_id: str | None, fresh_id: str | None) -> Any:
        """Read the physical source only when it differs from the derived index."""
        if not fresh_id or fresh_id == source_id:
            return None
        state = self.hass.states.get(fresh_id)
        return state.state if state else None

    @staticmethod
    def _history_ready(old: dict[str, Any], source_id: str | None) -> bool:
        """Accept Recorder or observed changes only for the current source."""
        return bool(
            old.get("value_history_source") == source_id
            and old.get("reliability_history_ready", False)
        ) or (
            old.get("source_identity") == source_id
            and old.get("last_value_change_at") is not None
        )

    def emit_transition(self, entry_id: Any, previous: Any) -> None:
        """Publish a change; delivery remains pending until acknowledged."""
        current = self.records[entry_id]
        status = current["status"]
        if _clear_applies(current) and (
            previous in {"due", "overdue"}
            or (previous == "sensor_error" and status != "sensor_error")
            or current.get("notified_status") in {"due", "overdue"}
        ):
            current["clear_pending"] = True
        notify = needs_notification(current)
        clear = needs_clear(current)
        if notify or clear:
            self.last_attempt[entry_id] = dt_util.utcnow().timestamp()
            current["delivery_attempts"] = current.get("delivery_attempts", 0) + 1
        if status == "ok" and current.get("due_since") is None:
            current["notified_status"] = None
            if not clear:
                current["delivery_attempts"] = 0
        self.hass.bus.async_fire(
            "plant_care_status_changed",
            {
                "entity_id": self.entities[entry_id].entity_id,
                "plant_name": self.details[entry_id]["plant_name"],
                "status": status,
                "previous": previous,
                "notify": notify,
                "clear": clear,
            },
        )
        self.store.async_delay_save(lambda: self.records, 10)

    def _prepare_adc_context(self, current: dict, source: str, now: float) -> bool:
        """Fence physical-source metadata independently of calibration storage."""
        raw_id = self.config.get("freshness_sources", {}).get(source, source)
        context = [
            source,
            raw_id,
            self._calibration_source_identity(source),
            self.config.get("automatic_calibration", {})
            .get(source, {})
            .get("source_revision"),
        ]
        previous = current.get("adc_source_context")
        changed = previous != context and (
            previous is not None or bool(current.get("adc_samples"))
        )
        if changed:
            self._reset_adc_samples(current)
            current.pop("raw_value_history", None)
            current["adc_source_fence"] = now
        current["adc_source_context"] = context
        return changed

    def note_source_event(self, event: Any) -> None:
        """Retain the event snapshot before a busy refresh coalesces callbacks."""
        raw = event.data.get("new_state")
        observed = timestamp(
            getattr(raw, "last_updated", None) or getattr(event, "time_fired", None)
        )
        if observed is None:
            return
        now = dt_util.utcnow().timestamp()
        plants = self.hass.data.get("adaptive_plant", {})
        if not isinstance(plants, dict):
            return
        for entry_id, plant in plants.items():
            source = getattr(plant, "moisture_sensor", None)
            raw_id = self.config.get("freshness_sources", {}).get(source, source)
            if not raw_id or event.data.get("entity_id") != raw_id:
                continue
            current = self.records.setdefault(entry_id, {})
            self._prepare_adc_context(current, source, now)
            if raw is None:
                interruptions = set(current.get("watering_interruptions", []))
                interruptions.add(observed)
                current["watering_interruptions"] = sorted(interruptions)
            else:
                self._merge_adc_states(current, [raw], now)
            self.store.async_delay_save(lambda: self.records, 10)

    async def _load_history(self, entry_id: str, plant: Any, now: float) -> None:
        """Read bounded recorder history once per source, retrying failed startup."""
        source = plant.moisture_sensor
        raw_id = self.config.get("freshness_sources", {}).get(source, source)
        record = self.records.setdefault(entry_id, {})
        identity = (entry_id, raw_id, source)
        context_changed = self._prepare_adc_context(record, source, now)
        if context_changed or (
            "adc_identity" in record and record["adc_identity"] != raw_id
        ):
            self._reset_adc_samples(record)
            record.pop("raw_value_history", None)
            self.history_loaded.discard(identity)
            self.history_attempts.pop(identity, None)
        record["adc_identity"] = raw_id
        if not raw_id:
            return
        retry_delay = min(900, 60 * 2 ** min(self.history_failures.get(identity, 0), 4))
        if (
            identity in self.history_loaded
            or now - self.history_attempts.get(identity, 0) < retry_delay
        ):
            return
        # Legacy settings lack a trustworthy boundary for pre-change inference.
        if "adc_calibration" in record:
            record.setdefault("adc_calibration_fence", now)
        self.history_attempts[identity] = now
        self.history_failures[identity] = self.history_failures.get(identity, 0) + 1
        try:
            end = dt_util.utcnow()
            states = await get_instance(self.hass).async_add_executor_job(
                partial(
                    history.get_significant_states,
                    self.hass,
                    end - timedelta(days=14),
                    end,
                    list(dict.fromkeys([raw_id, source])),
                    include_start_time_state=True,
                    significant_changes_only=False,
                    minimal_response=False,
                    no_attributes=False,
                )
            )
            self.calibration.seed_history(entry_id, states.get(raw_id, []), record, now)
            record = self.records.setdefault(entry_id, {})
            observed_now = max(now, dt_util.utcnow().timestamp())
            self._merge_adc_states(record, states.get(raw_id, []), observed_now)
            # Restarts and unavailable->same-value states are not numeric changes.
            # Recorder's bounded initial state supplies a conservative lower bound.
            record["value_history_source"] = source
            record["value_history"] = [
                [timestamp(state.last_updated), finite(state.state)]
                for state in states.get(source, [])
            ]
            record["reliability_history_ready"] = any(
                stamp is not None
                and value is not None
                and 0 <= value <= MAX_MOISTURE_INDEX
                for stamp, value in record["value_history"]
            )
            if record["reliability_history_ready"]:
                self.history_loaded.add(identity)
                self.history_failures.pop(identity, None)
        except Exception:
            _LOGGER.exception(
                "Unable to load plant ADC recorder history for %s", raw_id
            )

    @staticmethod
    def _voice_hour(after: float, now: float) -> float:
        """Find the first future whole local hour in the 08:00-21:00 window."""
        candidate = dt_util.as_local(
            datetime.fromtimestamp(max(after, now + 0.001), UTC)
        )
        if candidate.minute or candidate.second or candidate.microsecond:
            candidate = candidate.replace(
                minute=0, second=0, microsecond=0
            ) + timedelta(hours=1)
        if candidate.hour < VOICE_START_HOUR:
            candidate = candidate.replace(hour=VOICE_START_HOUR)
        elif candidate.hour > VOICE_LAST_HOUR:
            candidate = (candidate + timedelta(days=1)).replace(hour=VOICE_START_HOUR)
        return candidate.timestamp()

    def _calibration(self, source: str | None) -> list[float] | None:
        """Read calibration and watch the same analytics inputs."""
        calibration_ids = self.config.get("calibration_sources", {}).get(source, {})
        dry_state = (
            self.hass.states.get(calibration_ids.get("dry"))
            if calibration_ids
            else None
        )
        wet_state = (
            self.hass.states.get(calibration_ids.get("wet"))
            if calibration_ids
            else None
        )
        dry = finite(dry_state.state) if dry_state else None
        wet = finite(wet_state.state) if wet_state else None
        calibration = (
            [dry, wet] if dry is not None and wet is not None and wet > dry else None
        )
        self.watched.update(x for x in calibration_ids.values() if x)
        return calibration

    @staticmethod
    def _retained_raw_points(current: dict) -> list:
        """Migrate a legacy buffer only when no factual buffer has been recorded."""
        return list(current.get("raw_value_history", current.get("adc_samples", [])))

    @staticmethod
    def _reset_adc_samples(current: dict) -> None:
        for key in (
            "adc_samples",
            "adc_bursts",
            "adc_provenance",
            "watering_conflicts",
            "watering_interruptions",
        ):
            current[key] = []
        current.pop("adc_calibration", None)

    @staticmethod
    def _collect_adc_samples(
        current: dict[str, Any],
        raw_id: str | None,
        raw: Any,
        calibration: list[float] | None,
        now: float,
    ) -> None:
        """Retain physical observations and their provenance across ordinary polls."""
        previous_calibration = current.get("adc_calibration")
        source_changed = "adc_identity" in current and current["adc_identity"] != raw_id
        calibration_changed = (
            calibration is not None
            and previous_calibration is not None
            and previous_calibration != calibration
        )
        if calibration_changed:
            if not source_changed:
                current["raw_value_history"] = CareManager._retained_raw_points(current)
            # Recorder replay must retain this boundary without calibration storage.
            current["adc_calibration_fence"] = max(
                finite(current.get("adc_calibration_fence")) or 0, now
            )
        if source_changed or calibration_changed:
            CareManager._reset_adc_samples(current)
        if calibration is not None:
            current["adc_calibration"] = calibration
        current["adc_identity"] = raw_id
        CareManager._merge_adc_states(current, [raw] if raw is not None else [], now)

    @staticmethod
    def _merge_adc_states(current: dict, states: list, now: float) -> None:
        """Keep burst contradictions and actual state interruptions with raw history."""
        fence = max(
            finite(current.get("adc_source_fence")) or 0,
            finite(current.get("adc_calibration_fence")) or 0,
        )
        raw_points = CareManager._retained_raw_points(current)
        points = [
            point for point in current.get("adc_samples", []) if point[0] >= fence
        ]
        conflicts = set(current.get("watering_conflicts", []))
        bursts = dict(current.get("adc_bursts", []))
        provenance = dict(current.get("adc_provenance", []))
        interruptions = set(current.get("watering_interruptions", []))
        for state in states:
            point = CareManager._adc_observation(state, interruptions, raw_points)
            if point is None or point[0] < fence:
                continue
            stamp = point[0]
            points.append(point)
            CareManager._retain_adc_burst(bursts, conflicts, state, stamp)
            fingerprint = canonical_digest(state.attributes.get("physical_pin"))
            if provenance.setdefault(stamp, fingerprint) != fingerprint:
                conflicts.add(stamp)
        current["raw_value_history"], _ = normalize_adc_points(raw_points, now)
        current["adc_samples"], current["watering_conflicts"] = normalize_adc_points(
            points, now, conflicts=conflicts
        )
        retained = {point[0] for point in current["adc_samples"]}
        current["adc_bursts"] = [
            [stamp, bursts[stamp]] for stamp in sorted(bursts) if stamp in retained
        ]
        CareManager._retain_source_fences(current, provenance, interruptions, now)

    @staticmethod
    def _adc_observation(
        state: Any, interruptions: set, raw_points: list
    ) -> list | None:
        """Retain a physical point before inference cutoffs, or record an outage."""
        stamp = timestamp(state.attributes.get("sampled_at"))
        value = finite(state.state)
        if stamp is not None and value is not None:
            point = [stamp, value]
            raw_points.append(point)
            return point
        interrupted_at = timestamp(getattr(state, "last_updated", None))
        if interrupted_at is not None:
            interruptions.add(interrupted_at)
        return None

    @staticmethod
    def _retain_source_fences(
        current: dict, provenance: dict, interruptions: set, now: float
    ) -> None:
        """Split adjacent physical channels, including legacy unattributed points."""
        prior = None
        have_prior = False
        for stamp, _value in current["adc_samples"]:
            identity = provenance.get(stamp)
            if have_prior and identity != prior:
                interruptions.add(stamp)
            prior, have_prior = identity, True
        retained = {point[0] for point in current["adc_samples"]}
        current["adc_provenance"] = [
            [stamp, provenance[stamp]]
            for stamp in sorted(provenance)
            if stamp in retained
        ]
        current["watering_interruptions"] = sorted(
            stamp for stamp in interruptions if now - RETENTION_SECONDS <= stamp <= now
        )

    @staticmethod
    def _retain_adc_burst(
        bursts: dict, conflicts: set, state: Any, stamp: float
    ) -> None:
        burst = state.attributes.get("samples")
        if not isinstance(burst, (list, tuple)):
            return
        fingerprint = canonical_digest([finite(item) for item in burst])
        if bursts.setdefault(stamp, fingerprint) != fingerprint:
            conflicts.add(stamp)

    @staticmethod
    def _observed_day(value: Any) -> Any:
        """Read the local day of a recorded date or measured event."""
        if isinstance(value, (int, float)):
            return dt_util.as_local(datetime.fromtimestamp(value, UTC)).date()
        try:
            return datetime.fromisoformat(str(value)).date() if value else None
        except (ValueError, TypeError):
            return None

    def _configured_report(self, source: str, now: float) -> dict | None:
        """Validate the reported local day without manufacturing a time."""
        configured = self.config.get("user_reports", {}).get(source)
        report = (
            dict(configured) if isinstance(configured, dict) else {"date": configured}
        )
        try:
            day = datetime.fromisoformat(str(report["date"])).date()
        except (ValueError, TypeError):
            return None
        if day > dt_util.as_local(datetime.fromtimestamp(now, UTC)).date():
            return None
        report["date"] = day.isoformat()
        return report

    def _restore_reported_watering(
        self, analysis: dict[str, Any], prior: tuple[Any, str], plant: Any, now: float
    ) -> None:
        """Persist user uncertainty while retaining a later trustworthy estimate."""
        report = self._configured_report(plant.moisture_sensor, now)
        detected = analysis.get("last_watering_at")
        detected_day = self._observed_day(detected)
        if report:
            report_day = self._observed_day(report["date"])
            same_day_auto = (
                report_day == detected_day
                and analysis.get("last_watering_source") == "auto"
                and (
                    report.get("precision") != "approximate_day"
                    or analysis.get("last_watering_precision") != "interval"
                )
            )
            preferred = (
                detected_day is None or report_day > detected_day or same_day_auto
            )
            record_report(analysis, report, now, select=preferred)
        if (
            analysis.get("last_watering_at") is None
            and prior[0]
            and analysis.get("untrusted_last_watering_at") != prior[0]
        ):
            analysis["last_watering_at"], analysis["last_watering_source"] = prior
        self._restore_upstream_watering(analysis, plant, now)

    def _restore_upstream_watering(
        self, analysis: dict[str, Any], plant: Any, now: float
    ) -> None:
        """Keep a newer valid upstream day as a separately attributed record."""
        if not getattr(plant, "last_watered", None):
            return
        try:
            upstream_day = datetime.fromisoformat(str(plant.last_watered)).date()
            prior_day = self._observed_day(analysis.get("last_watering_at"))
            if upstream_day <= dt_util.as_local(
                datetime.fromtimestamp(now, UTC)
            ).date() and (prior_day is None or upstream_day > prior_day):
                analysis["last_watering_at"] = upstream_day.isoformat()
                analysis["last_watering_source"] = "upstream"
                for key in WATERING_METADATA:
                    analysis[f"last_watering_{key}"] = None
                analysis["last_watering_precision"] = "day"
                analysis["last_watering_note"] = None
        except (ValueError, TypeError):
            pass

    def _update_voice_estimates(self, current: dict[str, Any], now: float) -> None:
        """Apply confirmation timing once after a full plant analysis."""
        forecast = current.get("forecast_due_at")
        if forecast is not None:
            forecast += self.settings["confirm_seconds"]
            current["forecast_due_at"] = forecast
        self._update_voice_metadata(current, now)

    def _update_voice_metadata(self, current: dict[str, Any], now: float) -> None:
        """Update voice availability without changing the watering forecast."""
        forecast = current.get("forecast_due_at")
        current["forecast_voice_at"] = (
            self._voice_hour(forecast + self.settings["overdue_seconds"], now)
            if forecast
            else None
        )
        due = current.get("due_since") or forecast
        toggle = self.hass.states.get(
            self.config.get("voice_toggle", "input_boolean.plant_care_voice_enabled")
        )
        speaker = self.hass.states.get(self.voice_target) if self.voice_target else None
        reason = self._voice_unavailable_reason(current, due, toggle, speaker)
        current["voice_unavailable_reason"] = reason
        current["voice_target"] = self.voice_target
        current["voice_target_name"] = (
            speaker.attributes.get("friendly_name", self.voice_target)
            if speaker
            else None
        )
        current["voice_target_available"] = target_available(speaker)
        current["voice_next_at"] = (
            self._voice_hour(
                max(
                    due + self.settings["overdue_seconds"],
                    current.get("snoozed_until") or 0,
                ),
                now,
            )
            if reason is None
            else None
        )

    def refresh_voice_metadata(self) -> None:
        """Publish speaker or toggle changes without reading history or samples."""
        now = dt_util.utcnow().timestamp()
        changed = False
        for entry_id in self.active_plants:
            current = self.records[entry_id]
            before = tuple(current.get(key) for key in VOICE_METADATA_KEYS)
            self._update_voice_metadata(current, now)
            if before != tuple(current.get(key) for key in VOICE_METADATA_KEYS):
                changed = True
                entity = self.entities.get(entry_id)
                if entity is not None and getattr(entity, "hass", None) is not None:
                    entity.async_write_ha_state()
        if changed:
            self.store.async_delay_save(lambda: self.records, 10)

    @staticmethod
    def _voice_unavailable_reason(
        current: dict[str, Any], due: float | None, toggle: Any, speaker: Any
    ) -> str | None:
        """Describe the first condition preventing a watering announcement."""
        if toggle is None or toggle.state != "on":
            return "Голосовые напоминания выключены"
        if not target_available(speaker):
            return "Колонка недоступна"
        if current.get("status") == "sensor_error":
            return "Показания датчика недостоверны"
        if current.get("status") == "calibrating":
            return "Проверяю показания после изменения калибровки"
        if not due:
            return "Нет достоверной оценки времени полива"
        return None

    def _analytics(
        self, current: dict[str, Any], plant: Any, reading: dict[str, Any], now: float
    ) -> None:
        """Update estimates without changing upstream watering or fertilizing."""
        source = plant.moisture_sensor
        raw_id = self.config.get("freshness_sources", {}).get(source, source)
        raw = self.hass.states.get(raw_id) if raw_id else None
        calibration = self._calibration(source)
        self._prepare_adc_context(current, source, now)
        self._collect_adc_samples(current, raw_id, raw, calibration, now)
        barrier = finite(reading.get("calibration_barrier_until"))
        if barrier is not None:
            current["adc_samples"] = [
                point for point in current["adc_samples"] if point[0] > barrier
            ]
        prior = (
            current.get("last_watering_at"),
            current.get("last_watering_source", "unknown"),
        )
        analysis = analyze(
            current,
            current["adc_samples"],
            now,
            calibration=calibration,
            dry_threshold=plant.dry_threshold,
            stale_seconds=self.settings["stale_seconds"],
            wet_hours=self.config.get("wet_hours", 72),
            wet_percent=self.config.get("wet_percent", 90),
            reliable=current.get("reliability_valid", True),
            unreliable_since=(
                current.get("reliability_incident_value_changed_at")
                if current.get("reliability_incident_reason")
                in ("saturated", "lower_saturation", "unchanged", "raw_unchanged")
                else None
            ),
        )
        if (
            raw is None
            or finite(raw.state) is None
            or finite(reading.get("moisture")) is None
        ):
            analysis.update(
                wet_risk="unknown",
                wet_since=None,
                forecast_status="stale",
                forecast_due_at=None,
                forecast_voice_at=None,
            )
        self._restore_reported_watering(analysis, prior, plant, now)
        current.update(analysis)
        self._update_voice_estimates(current, now)

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
        meaningful = {
            k: v for k, v in old.items() if k not in ("updated_at", "stagnant_seconds")
        } != {
            k: v
            for k, v in current.items()
            if k not in ("updated_at", "stagnant_seconds")
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
            (needs_notification(current) or needs_clear(current)) and retry_ready
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

    async def _refresh_once(self) -> None:
        """Refresh one snapshot of loaded plants and their subscriptions."""
        plants = self.hass.data.get("adaptive_plant")
        if not isinstance(plants, dict):
            return
        now = dt_util.utcnow().timestamp()
        changed = False
        self._refresh_watched = self.watched
        self.watched = set()
        for entry_id, plant in list(plants.items()):
            if hasattr(plant, "moisture_sensor"):
                changed = (
                    await self._refresh_calibrated_plant(entry_id, plant, now)
                    or changed
                )
        for entry_id in set(self.entities) - set(plants):
            changed = await self._remove_missing(entry_id) or changed
        if changed:
            self.store.async_delay_save(lambda: self.records, 10)

    async def _refresh_calibrated_plant(
        self, entry_id: str, plant: Any, now: float
    ) -> bool:
        """Order reconciliation, reliability, and separately authorized observation."""
        before = self.calibration.attributes(entry_id)
        await self.calibration.async_prepare(entry_id, plant.moisture_sensor, now)
        await self._load_history(entry_id, plant, now)
        now = max(now, dt_util.utcnow().timestamp())
        changed = self._refresh_plant(entry_id, plant, now)
        await self.calibration.async_observe(
            entry_id,
            self.records[entry_id],
            now,
            allow_write=self._calibration_write_active,
        )
        changed = await self._persist_calibration_witnesses(entry_id) or changed
        if before != self.calibration.attributes(entry_id):
            changed = True
            entity = self.entities.get(entry_id)
            if entity is not None and getattr(entity, "hass", None) is not None:
                entity.async_write_ha_state()
        return changed

    async def async_background_refresh(self, _event: Any = None) -> None:
        """Authorize automatic writes only from the independently scheduled timer."""
        await self.async_refresh(calibration_write=True)

    async def async_refresh(
        self, _event: Any = None, *, wait: bool = False, calibration_write: bool = False
    ) -> None:
        """Coalesce refreshes and let explicit callers await the latest pass."""
        if self.add_entities is None:
            return
        if calibration_write:
            self._calibration_write_pending = True
        if self.busy:
            self._refresh_pending = True
            if wait and asyncio.current_task() is not self._refresh_owner:
                await self._refresh_finished.wait()
            return
        self.busy = True
        self._refresh_owner = asyncio.current_task()
        self._refresh_finished.clear()
        try:
            while True:
                self._refresh_pending = False
                self._calibration_write_active = self._calibration_write_pending
                self._calibration_write_pending = False
                await self._refresh_once()
                self._calibration_write_active = False
                if not self._refresh_pending:
                    break
        except Exception:
            _LOGGER.exception("Failed to update plant care alerts")
        finally:
            self._calibration_write_active = False
            self._calibration_write_pending = False
            self.busy = False
            self._refresh_owner = None
            self._refresh_watched = set()
            self._refresh_finished.set()
