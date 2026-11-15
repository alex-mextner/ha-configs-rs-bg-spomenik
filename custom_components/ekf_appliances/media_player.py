"""Projector media functions backed by verified keys and existing power scripts."""
import logging
from datetime import datetime

from homeassistant.exceptions import HomeAssistantError
from homeassistant.components.media_player import (
    MediaPlayerEntity, MediaPlayerEntityFeature, MediaPlayerDeviceClass,
    MediaPlayerState,
)
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_TV_ENTITY_ID
from .entity import EkfEntity
from .runtime import ApplianceRuntime
from .paired_media import async_guarded_wake, install_power_pairs
from .samsung_tv import SamsungTvMediaPlayer

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    projector = ProjectorMediaPlayer(entry.runtime_data)
    entities = [projector]
    television = None
    if entry.data.get(CONF_TV_ENTITY_ID):
        try:
            television = SamsungTvMediaPlayer(entry.runtime_data)
        except HomeAssistantError as err:
            _LOGGER.error("Cannot configure optional Samsung TV: %s", err)
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
        MediaPlayerEntityFeature.TURN_ON | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE
    )

    def __init__(self, runtime: ApplianceRuntime) -> None:
        super().__init__(runtime, "projector", "projector")

    @property
    def available(self) -> bool:
        """Keep confirmed Kodi controls usable during an IR-only outage."""
        return self.runtime.available or self._kodi_state is not None

    @property
    def state(self) -> MediaPlayerState | None:
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
        await super().async_added_to_hass()
        dependencies = [self.runtime.projector_power_entity_id]
        if self.kodi_entity_id:
            dependencies.append(self.kodi_entity_id)
        self.async_on_remove(async_track_state_change_event(
            self.hass, dependencies, self._async_dependency_changed
        ))
        self.runtime.shutdown_callbacks.append(self.async_write_ha_state)
        self.async_on_remove(lambda: self.runtime.shutdown_callbacks.remove(self.async_write_ha_state))

    async def async_will_remove_from_hass(self) -> None:
        await self.runtime.async_cancel_projector_power()
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self):
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
        observed = self.runtime.projector_power_state
        if observed is None:
            await self.runtime.async_projector_power(None)
        else:
            await self._async_pair_power(not observed)

    async def _async_pair_power(self, target: bool) -> None:
        if target and self.runtime.projector_shutting_down:
            raise HomeAssistantError("Projector is shutting down; wait for fresh HDMI OFF")
        if not target:
            self.runtime.note_projector_off_requested()
        if self.runtime.projector_power_state is None:
            # Retain the absolute-power safety contract: no Kodi start and no
            # guessed projector toggle when HDMI cannot establish direction.
            await self.runtime.async_projector_power(target)
            return
        if pair := self.runtime.projector_power_pair:
            await pair.request(target)
        else:
            await self.runtime.async_projector_power(target)

    async def async_wake_if_current(self, expected_revision: str) -> None:
        await async_guarded_wake(self.runtime, self.runtime.projector_power_pair, expected_revision)

    async def async_turn_on(self) -> None:
        await self._async_pair_power(True)

    async def async_turn_off(self) -> None:
        await self._async_pair_power(False)

    @property
    def supported_features(self):
        features = self._attr_supported_features
        kodi = self.runtime.hass.states.get(self.kodi_entity_id) if self.kodi_entity_id else None
        if kodi is not None:
            features |= MediaPlayerEntityFeature(int(kodi.attributes.get("supported_features", 0))) & (
                MediaPlayerEntityFeature.VOLUME_SET | MediaPlayerEntityFeature.VOLUME_MUTE
            )
        return features

    async def async_volume_up(self) -> None:
        if self.kodi_entity_id:
            await self._async_kodi_action("volume_up")
        else:
            await self.runtime.async_send_projector("vol_up")

    async def async_volume_down(self) -> None:
        if self.kodi_entity_id:
            await self._async_kodi_action("volume_down")
        else:
            await self.runtime.async_send_projector("vol_down")

    async def async_set_volume_level(self, volume: float) -> None:
        if not self.kodi_entity_id:
            raise HomeAssistantError("The native projector has no observed absolute volume")
        await self._async_kodi_action("volume_set", volume_level=volume)

    async def async_mute_volume(self, mute: bool) -> None:
        if not self.kodi_entity_id:
            raise HomeAssistantError("The native projector has no observed mute state")
        await self._async_kodi_action("volume_mute", is_volume_muted=mute)

    @property
    def volume_level(self) -> float | None:
        value = self._kodi_attribute("volume_level")
        return value if type(value) in (int, float) and 0 <= value <= 1 else None

    @property
    def is_volume_muted(self) -> bool | None:
        value = self._kodi_attribute("is_volume_muted")
        return value if isinstance(value, bool) else None

    @property
    def kodi_entity_id(self) -> str | None:
        """Reuse the existing Kodi player selection shared with TV routing."""
        return self.runtime.entry.data.get("tv_kodi_entity_id")

    @property
    def _kodi_state(self):
        if self.runtime.projector_power_state is not True or not self.kodi_entity_id:
            return None
        state = self.runtime.hass.states.get(self.kodi_entity_id)
        if state is None or state.state in ("unknown", "unavailable", "off"):
            return None
        return state

    def _kodi_attribute(self, name):
        state = self._kodi_state
        return state.attributes.get(name) if state is not None else None

    @property
    def media_title(self) -> str | None:
        return self._kodi_attribute("media_title")

    @property
    def media_content_type(self) -> str | None:
        return self._kodi_attribute("media_content_type")

    @property
    def media_duration(self) -> float | None:
        return self._kodi_attribute("media_duration")

    @property
    def media_position(self) -> float | None:
        return self._kodi_attribute("media_position")

    @property
    def media_position_updated_at(self) -> datetime | None:
        return self._kodi_attribute("media_position_updated_at")

    async def _async_kodi_action(self, service: str, **data) -> None:
        if self.runtime.projector_shutting_down:
            raise HomeAssistantError("Projector is shutting down; Kodi controls are suspended")
        if self.runtime.projector_power_state is not True:
            raise HomeAssistantError("Projector HDMI power is not confirmed")
        if self._kodi_state is None:
            raise HomeAssistantError("The configured Kodi player is not running")
        await self.runtime.hass.services.async_call(
            "media_player", service, {"entity_id": self.kodi_entity_id, **data}, blocking=True
        )

    async def async_media_play_pause(self) -> None:
        if self.kodi_entity_id:
            await self._async_kodi_action("media_play_pause")
        else:
            await self.runtime.async_send_projector("play_pause")

    async def async_media_play(self) -> None:
        if self.kodi_entity_id:
            await self._async_kodi_action("media_play")
        else:
            await self.async_media_play_pause()

    async def async_media_pause(self) -> None:
        if self.kodi_entity_id:
            await self._async_kodi_action("media_pause")
        else:
            await self.async_media_play_pause()
