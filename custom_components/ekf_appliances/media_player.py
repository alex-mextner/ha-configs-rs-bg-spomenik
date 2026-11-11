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
from .samsung_tv import SamsungTvMediaPlayer

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    entities = [ProjectorMediaPlayer(entry.runtime_data)]
    if entry.data.get(CONF_TV_ENTITY_ID):
        try:
            television = SamsungTvMediaPlayer(entry.runtime_data)
        except HomeAssistantError as err:
            _LOGGER.error("Cannot configure optional Samsung TV: %s", err)
        else:
            await television.coordinator.async_config_entry_first_refresh()
            entities.append(television)
    async_add_entities(entities)


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

    async def async_will_remove_from_hass(self) -> None:
        await self.runtime.async_cancel_projector_power()
        await super().async_will_remove_from_hass()

    async def async_toggle(self) -> None:
        await self.runtime.async_projector_power(None)

    async def async_turn_on(self) -> None:
        await self.runtime.async_projector_power(True)

    async def async_turn_off(self) -> None:
        await self.runtime.async_projector_power(False)

    async def async_volume_up(self) -> None:
        await self.runtime.async_send_projector("vol_up")

    async def async_volume_down(self) -> None:
        await self.runtime.async_send_projector("vol_down")

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

    async def _async_kodi_action(self, service: str) -> None:
        if self.runtime.projector_power_state is not True:
            raise HomeAssistantError("Projector HDMI power is not confirmed")
        if self._kodi_state is None:
            raise HomeAssistantError("The configured Kodi player is not running")
        await self.runtime.hass.services.async_call(
            "media_player", service, {"entity_id": self.kodi_entity_id}, blocking=True
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
