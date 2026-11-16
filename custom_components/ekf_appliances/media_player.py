"""Projector media functions backed by verified keys and existing power scripts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, State
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .runtime import ApplianceRuntime

import logging

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_TV_ENTITY_ID
from .entity import EkfEntity
from .paired_media import async_guarded_wake, install_power_pairs
from .samsung_tv import SamsungTvMediaPlayer

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry[ApplianceRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the projector and optional television entities for this config entry."""
    projector = ProjectorMediaPlayer(entry.runtime_data)
    entities = [projector]
    television = None
    if entry.data.get(CONF_TV_ENTITY_ID):
        try:
            television = SamsungTvMediaPlayer(entry.runtime_data)
        except HomeAssistantError:
            _LOGGER.exception("Cannot configure optional Samsung TV")
        else:
            await television.coordinator.async_config_entry_first_refresh()
            entities.append(television)
    async_add_entities(entities)
    install_power_pairs(entry.runtime_data, projector, television)


class ProjectorMediaPlayer(EkfEntity, MediaPlayerEntity):
    """Report observed HDMI power and serialize the physical POWER key."""

    _attr_name = None
    _attr_assumed_state = False
    _attr_suggested_object_id = "ekf_projector"
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
    )

    def __init__(self, runtime: ApplianceRuntime) -> None:
        """Bind projector controls to the shared appliance runtime."""
        super().__init__(runtime, "projector", "projector")

    @property
    def available(self) -> bool:
        """Keep confirmed Kodi controls usable during an IR-only outage."""
        return self.runtime.available or self._kodi_state is not None

    @property
    def state(self) -> MediaPlayerState | None:
        """Return fresh HDMI power and the running Kodi playback state."""
        state = self.runtime.projector_power_state
        if state is True:
            kodi = self._kodi_state
            if kodi is not None and kodi.state in ("playing", "paused", "idle"):
                return MediaPlayerState(kodi.state)
            return MediaPlayerState.ON
        if state is False:
            return MediaPlayerState.OFF
        return None

    async def async_added_to_hass(self) -> None:
        """Subscribe to projector, Kodi and shutdown-state changes."""
        await super().async_added_to_hass()
        dependencies = [self.runtime.projector_power_entity_id]
        if self.kodi_entity_id:
            dependencies.append(self.kodi_entity_id)
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, dependencies, self._async_dependency_changed
            )
        )
        self.runtime.shutdown_callbacks.append(self.async_write_ha_state)
        self.async_on_remove(
            lambda: self.runtime.shutdown_callbacks.remove(self.async_write_ha_state)
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending projector lifecycle transition on removal."""
        await self.runtime.async_cancel_projector_power()
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose paired-player identity, power intent and shutdown state."""
        pair = self.runtime.projector_power_pair
        return {
            "paired_player_entity_id": self.kodi_entity_id,
            "power_sync_target": pair.target if pair else None,
            "power_sync_error": pair.last_error if pair else None,
            "power_intent_revision": pair.intent_revision if pair else None,
            "last_power_intent": pair.last_intent if pair else None,
            "shutting_down": self.runtime.projector_shutting_down,
        }

    async def async_toggle(self) -> None:
        """Choose one power direction from current observed HDMI feedback."""
        observed = self.runtime.projector_power_state
        if observed is None:
            await self.runtime.async_projector_power(turn_on=None)
        else:
            await self._async_pair_power(target=not observed)

    async def _async_pair_power(self, *, target: bool) -> None:
        if target and self.runtime.projector_shutting_down:
            error_message = "Projector is shutting down; wait for fresh HDMI OFF"
            raise HomeAssistantError(error_message)
        if not target:
            self.runtime.note_projector_off_requested()
        if self.runtime.projector_power_state is None:
            # Retain the absolute-power safety contract: no Kodi start and no
            # guessed projector toggle when HDMI cannot establish direction.
            await self.runtime.async_projector_power(turn_on=target)
            return
        if pair := self.runtime.projector_power_pair:
            await pair.request(target=target)
        else:
            await self.runtime.async_projector_power(turn_on=target)

    async def async_wake_if_current(self, expected_revision: str) -> None:
        """Accept a queued wake only while its power intent is current."""
        await async_guarded_wake(
            self.runtime, self.runtime.projector_power_pair, expected_revision
        )

    async def async_turn_on(self) -> None:
        """Request confirmed power-on for the projector pair."""
        await self._async_pair_power(target=True)

    async def async_turn_off(self) -> None:
        """Request confirmed power-off for the projector pair."""
        await self._async_pair_power(target=False)

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Include native Kodi volume features when a player is configured."""
        features = self._attr_supported_features
        kodi = (
            self.runtime.hass.states.get(self.kodi_entity_id)
            if self.kodi_entity_id
            else None
        )
        if kodi is not None:
            features |= MediaPlayerEntityFeature(
                int(kodi.attributes.get("supported_features", 0))
            ) & (
                MediaPlayerEntityFeature.VOLUME_SET
                | MediaPlayerEntityFeature.VOLUME_MUTE
            )
        return features

    async def async_volume_up(self) -> None:
        """Increase Kodi volume or send the native projector step command."""
        if self.kodi_entity_id:
            await self._async_kodi_action("volume_up")
        else:
            await self.runtime.async_send_projector("vol_up")

    async def async_volume_down(self) -> None:
        """Decrease Kodi volume or send the native projector step command."""
        if self.kodi_entity_id:
            await self._async_kodi_action("volume_down")
        else:
            await self.runtime.async_send_projector("vol_down")

    async def async_set_volume_level(self, volume: float) -> None:
        """Set an observed absolute volume through the configured Kodi player."""
        if not self.kodi_entity_id:
            error_message = "The native projector has no observed absolute volume"
            raise HomeAssistantError(error_message)
        await self._async_kodi_action("volume_set", volume_level=volume)

    # HA defines mute as a positional argument; preserve the inherited contract.
    async def async_mute_volume(self, mute: bool) -> None:  # noqa: FBT001
        """Set the configured Kodi player mute state."""
        if not self.kodi_entity_id:
            error_message = "The native projector has no observed mute state"
            raise HomeAssistantError(error_message)
        await self._async_kodi_action("volume_mute", is_volume_muted=mute)

    @property
    def volume_level(self) -> float | None:
        """Return the observed Kodi volume fraction when valid."""
        value = self._kodi_attribute("volume_level")
        return value if type(value) in (int, float) and 0 <= value <= 1 else None

    @property
    def is_volume_muted(self) -> bool | None:
        """Return the observed Kodi mute state when known."""
        value = self._kodi_attribute("is_volume_muted")
        return value if isinstance(value, bool) else None

    @property
    def kodi_entity_id(self) -> str | None:
        """Reuse the existing Kodi player selection shared with TV routing."""
        return self.runtime.entry.data.get("tv_kodi_entity_id")

    @property
    def _kodi_state(self) -> State | None:
        if self.runtime.projector_power_state is not True or not self.kodi_entity_id:
            return None
        state = self.runtime.hass.states.get(self.kodi_entity_id)
        if state is None or state.state in ("unknown", "unavailable", "off"):
            return None
        return state

    def _kodi_attribute(self, name: str) -> Any:
        state = self._kodi_state
        return state.attributes.get(name) if state is not None else None

    @property
    def media_title(self) -> str | None:
        """Return the configured Kodi player media title."""
        return self._kodi_attribute("media_title")

    @property
    def media_content_type(self) -> str | None:
        """Return the configured Kodi player content type."""
        return self._kodi_attribute("media_content_type")

    @property
    def media_duration(self) -> float | None:
        """Return the configured Kodi player media duration."""
        return self._kodi_attribute("media_duration")

    @property
    def media_position(self) -> float | None:
        """Return the configured Kodi player media position."""
        return self._kodi_attribute("media_position")

    @property
    def media_position_updated_at(self) -> datetime | None:
        """Return the last Kodi media-position observation time."""
        return self._kodi_attribute("media_position_updated_at")

    async def _async_kodi_action(self, service: str, **data: Any) -> None:
        if self.runtime.projector_shutting_down:
            error_message = "Projector is shutting down; Kodi controls are suspended"
            raise HomeAssistantError(error_message)
        if self.runtime.projector_power_state is not True:
            error_message = "Projector HDMI power is not confirmed"
            raise HomeAssistantError(error_message)
        if self._kodi_state is None:
            error_message = "The configured Kodi player is not running"
            raise HomeAssistantError(error_message)
        await self.runtime.hass.services.async_call(
            "media_player",
            service,
            {"entity_id": self.kodi_entity_id, **data},
            blocking=True,
        )

    async def async_media_play_pause(self) -> None:
        """Toggle playback through Kodi or the verified native key."""
        if self.kodi_entity_id:
            await self._async_kodi_action("media_play_pause")
        else:
            await self.runtime.async_send_projector("play_pause")

    async def async_media_play(self) -> None:
        """Start Kodi playback or send the native playback toggle."""
        if self.kodi_entity_id:
            await self._async_kodi_action("media_play")
        else:
            await self.async_media_play_pause()

    async def async_media_pause(self) -> None:
        """Pause Kodi playback or send the native playback toggle."""
        if self.kodi_entity_id:
            await self._async_kodi_action("media_pause")
        else:
            await self.async_media_play_pause()
