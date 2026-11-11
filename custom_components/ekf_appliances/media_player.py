"""Projector media functions backed by verified keys and existing power scripts."""
from homeassistant.components.media_player import (
    MediaPlayerEntity, MediaPlayerEntityFeature, MediaPlayerDeviceClass,
    MediaPlayerState,
)
from homeassistant.helpers.event import async_track_state_change_event

from .entity import EkfEntity
from .runtime import ApplianceRuntime


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([ProjectorMediaPlayer(entry.runtime_data)])


class ProjectorMediaPlayer(EkfEntity, MediaPlayerEntity):
    """Keep projector lifecycle in the user's existing named scripts."""

    _attr_name = None
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
        state = self.runtime.hass.states.get(self.runtime.projector_power_entity_id)
        if state and state.state == "on":
            return MediaPlayerState.ON
        if state and state.state == "off":
            return MediaPlayerState.OFF
        return None

    @property
    def available(self) -> bool:
        return super().available and self.state is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_track_state_change_event(
            self.hass, [self.runtime.projector_power_entity_id], self._async_dependency_changed
        ))

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
