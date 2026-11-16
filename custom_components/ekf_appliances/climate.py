"""Complete Haier YRW02 state with explicitly assumed operating feedback."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .runtime import ApplianceRuntime

import asyncio
import math
from dataclasses import dataclass, replace

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.unit_conversion import TemperatureConverter

from .entity import EkfEntity
from .protocol import FANS, MODES, PRESETS, SWINGS, haier_state


@dataclass(frozen=True)
class ClimateSettings:
    """Hold the complete Haier state encoded into each transmitted frame."""

    mode: str = "off"
    temperature: float = 24
    fan: str = "auto"
    swing: str = "off"
    preset: str = "none"
    last_mode: str = "cool"


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry[ApplianceRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the Haier climate entity for this config entry."""
    async_add_entities([HaierClimate(entry.runtime_data)])


class HaierClimate(EkfEntity, ClimateEntity, RestoreEntity):
    """Send full state rather than relying on a blind power toggle."""

    _attr_name = None
    _attr_suggested_object_id = "ekf_haier"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_min_temp = 16
    _attr_max_temp = 30
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.DRY,
        HVACMode.FAN_ONLY,
        HVACMode.AUTO,
    ]
    _attr_fan_modes: ClassVar[list[str]] = list(FANS)
    _attr_swing_modes: ClassVar[list[str]] = list(SWINGS)
    _attr_preset_modes: ClassVar[list[str]] = list(PRESETS)
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, runtime: ApplianceRuntime) -> None:
        """Initialize the serialized, restorable climate settings."""
        super().__init__(runtime, "haier", "haier")
        self._settings = ClimateSettings()
        self._state_lock = asyncio.Lock()

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the last successfully requested HVAC mode."""
        return HVACMode(self._settings.mode)

    @property
    def target_temperature(self) -> float:
        """Return the target temperature encoded in the appliance state."""
        return self._settings.temperature

    @property
    def fan_mode(self) -> str:
        """Return the requested fan setting."""
        return self._settings.fan

    @property
    def swing_mode(self) -> str:
        """Return the requested vertical swing setting."""
        return self._settings.swing

    @property
    def preset_mode(self) -> str:
        """Return the requested appliance preset."""
        return self._settings.preset

    @property
    def extra_state_attributes(self) -> dict[str, str | float]:
        """Expose the retained mode and native target temperature."""
        return {
            "last_hvac_mode": self._settings.last_mode,
            "native_target_temperature": self._settings.temperature,
        }

    @property
    def current_temperature(self) -> float | None:
        """Return a finite, converted room-temperature observation."""
        state = self.runtime.hass.states.get(self.runtime.temperature_entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
            if not math.isfinite(value):
                return None
            unit = state.attributes.get("unit_of_measurement")
            if (
                not isinstance(unit, str)
                or unit not in TemperatureConverter.VALID_UNITS
            ):
                return None
            value = TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
            return value if math.isfinite(value) else None
        except (ValueError, TypeError):
            return None

    async def async_added_to_hass(self) -> None:
        """Restore valid settings and subscribe to room-temperature updates."""
        await super().async_added_to_hass()
        if (restored := await self.async_get_last_state()) is not None:
            attrs = restored.attributes
            mode = restored.state if restored.state in ("off", *MODES) else "off"
            last_mode = attrs.get("last_hvac_mode", mode if mode != "off" else "cool")
            if last_mode not in MODES:
                last_mode = "cool"
            candidate = ClimateSettings(
                mode=mode,
                temperature=attrs.get(
                    "native_target_temperature", attrs.get("temperature", 24)
                ),
                fan=attrs.get("fan_mode", "auto"),
                swing=attrs.get("swing_mode", "off"),
                preset=attrs.get("preset_mode", "none"),
                last_mode=last_mode,
            )
            if candidate.preset == "comfort":
                candidate = replace(candidate, preset="none")
            if candidate.preset == "boost" and mode not in ("off", "cool", "heat"):
                candidate = replace(candidate, preset="none")
            try:
                self._encode(candidate)
            except (ValueError, TypeError):
                candidate = ClimateSettings()
            self._settings = candidate
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self.runtime.temperature_entity_id],
                self._async_dependency_changed,
            )
        )

    @staticmethod
    def _encode(settings: ClimateSettings) -> bytes:
        return haier_state(
            settings.mode,
            settings.temperature,
            settings.fan,
            settings.swing,
            settings.preset,
        )

    async def _async_change(
        self, *, restore_mode: bool = False, **changes: Any
    ) -> None:
        async with self._state_lock:
            if restore_mode:
                changes["mode"] = self._settings.last_mode
            candidate = replace(self._settings, **changes)
            if candidate.preset == "boost" and candidate.mode not in (
                "off",
                "cool",
                "heat",
            ):
                if changes.get("preset") == "boost":
                    error_message = "Boost is supported only in cool or heat mode"
                    raise ServiceValidationError(error_message)
                candidate = replace(candidate, preset="none")
            try:
                raw = self._encode(candidate)
            except (ValueError, TypeError) as err:
                raise ServiceValidationError(str(err)) from err
            if candidate.mode != "off":
                candidate = replace(candidate, last_mode=candidate.mode)
            # An explicit OFF must still reach a possibly running appliance
            # after restart.
            if candidate.mode != "off" or changes.get("mode") == "off":
                await self.runtime.async_send_haier(raw)
            self._settings = candidate
            self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Validate and transmit a target temperature and optional HVAC mode."""
        if "temperature" not in kwargs:
            error_message = "A target temperature is required"
            raise ServiceValidationError(error_message)
        changes = {"temperature": kwargs["temperature"]}
        if "hvac_mode" in kwargs:
            changes["mode"] = kwargs["hvac_mode"]
        await self._async_change(**changes)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Transmit the requested HVAC mode in a complete state frame."""
        await self._async_change(mode=hvac_mode)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Transmit the requested fan setting in a complete state frame."""
        await self._async_change(fan=fan_mode)

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Transmit the requested swing setting in a complete state frame."""
        await self._async_change(swing=swing_mode)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Transmit the requested preset in a complete state frame."""
        await self._async_change(preset=preset_mode)

    async def async_turn_on(self) -> None:
        """Restore the last active HVAC mode."""
        await self._async_change(restore_mode=True)

    async def async_turn_off(self) -> None:
        """Transmit an explicit appliance off state."""
        await self._async_change(mode="off")
