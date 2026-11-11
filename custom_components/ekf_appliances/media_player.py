"""Projector media functions backed by verified keys and existing power scripts."""
import logging

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
            return MediaPlayerState.ON
        if state is False:
            return MediaPlayerState.OFF
        return None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_track_state_change_event(
            self.hass, [self.runtime.projector_power_entity_id], self._async_dependency_changed
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

    async def async_media_play_pause(self) -> None:
        await self.runtime.async_send_projector("play_pause")

    async def async_media_play(self) -> None:
        await self.async_media_play_pause()

    async def async_media_pause(self) -> None:
        await self.async_media_play_pause()
