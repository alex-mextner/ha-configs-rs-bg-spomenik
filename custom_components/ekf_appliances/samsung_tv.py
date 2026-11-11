"""Samsung LAN media functions with discrete EKF power and passive feedback."""
import asyncio
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature as Feature,
    MediaPlayerState,
)
from homeassistant.const import CONF_HOST, CONF_MODEL
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import (
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_TV_ENTITY_ID, CONF_TV_ANDROIDTV_ENTITY_ID
from .media_routing import TelevisionMediaRouting
from .protocol import samsung_pulses
from .runtime import ApplianceRuntime
from .samsung_power import SamsungPowerCoordinator

TV_TRANSITION_TIMEOUT = 35.0
SAMSUNG_ON_COMMAND = "power_on"
SAMSUNG_OFF_COMMAND = "power_off"
_NATIVE_SERVICES = {
    "volume_set": Feature.VOLUME_SET,
    "volume_mute": Feature.VOLUME_MUTE,
    "volume_up": Feature.VOLUME_STEP,
    "volume_down": Feature.VOLUME_STEP,
    "select_source": Feature.SELECT_SOURCE,
    "media_play": Feature.PLAY,
    "media_pause": Feature.PAUSE,
    "media_play_pause": Feature.PLAY | Feature.PAUSE,
    "media_stop": Feature.STOP,
    "media_next_track": Feature.NEXT_TRACK,
    "media_previous_track": Feature.PREVIOUS_TRACK,
    "play_media": Feature.PLAY_MEDIA,
}
_NATIVE_FEATURES = Feature(0)
for _feature in _NATIVE_SERVICES.values():
    _NATIVE_FEATURES |= _feature
_NATIVE_PROPERTIES = (
    "volume_level", "is_volume_muted", "source", "media_content_id",
    "media_content_type", "media_duration", "media_position",
    "media_position_updated_at", "media_title", "media_artist",
    "media_album_name", "media_album_artist", "media_track", "media_series_title",
    "media_season", "media_episode", "media_channel", "media_playlist",
    "app_id", "app_name",
)


class SamsungTvMediaPlayer(CoordinatorEntity[SamsungPowerCoordinator], MediaPlayerEntity):
    """Combine display power with the media source that owns playback."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_assumed_state = False
    _attr_should_poll = False
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_suggested_object_id = "ekf_samsung_tv"

    def __init__(self, runtime: ApplianceRuntime) -> None:
        self.runtime = runtime
        hass = runtime.hass
        registry = er.async_get(hass)
        backend = registry.async_get(runtime.entry.data[CONF_TV_ENTITY_ID])
        native_entry = (
            hass.config_entries.async_get_entry(backend.config_entry_id)
            if backend and backend.config_entry_id else None
        )
        device = (
            dr.async_get(hass).async_get(backend.device_id)
            if backend and backend.device_id else None
        )
        identifiers = {
            identifier for identifier in device.identifiers
            if identifier[0] == "samsungtv"
        } if device else set()
        if (
            backend is None or backend.domain != "media_player"
            or backend.platform != "samsungtv"
            or native_entry is None or native_entry.domain != "samsungtv"
            or not identifiers
        ):
            raise HomeAssistantError("Choose a registered native Samsung TV backend")
        host = native_entry.data.get(CONF_HOST)
        model = native_entry.data.get(CONF_MODEL)
        if not isinstance(host, str) or not host.strip() or not isinstance(model, str) or not model.strip():
            raise HomeAssistantError("The native Samsung TV must have a host and model")
        super().__init__(SamsungPowerCoordinator(hass, runtime.entry, host, model))
        self.media_routing = TelevisionMediaRouting(hass, runtime.entry)
        self._backend_registry_id = backend.id
        self._attr_unique_id = f"{runtime.entry.entry_id}_samsung_{backend.unique_id}"
        # DeviceInfo lookup is scoped to its config entry in HA 2026. Reuse the
        # validated public device entry directly instead of creating a duplicate
        # or moving the native device into this integration.
        self._attr_device_info = None
        self.device_entry = device
        self._backend_unsubscribers = []
        self._power_lock = asyncio.Lock()
        self._power_task: asyncio.Task | None = None
        self._power_target: bool | None = None
        self._copy_native_attributes()

    @property
    def backend_entity_id(self) -> str | None:
        backend = er.async_get(self.runtime.hass).async_get(self._backend_registry_id)
        return backend.entity_id if backend is not None else None

    @property
    def _backend_state(self):
        entity_id = self.backend_entity_id
        return self.runtime.hass.states.get(entity_id) if entity_id else None

    @property
    def _native_available(self) -> bool:
        state = self._backend_state
        return state is not None and state.state not in ("unavailable", "unknown")

    @property
    def available(self) -> bool:
        # The native entity may be unavailable in standby, when IR ON is needed.
        # An IR outage must not hide working LAN volume/source controls.
        return self.runtime.available or (
            self.coordinator.data is not False
            and (self._native_available or self._media_state is not None)
        )

    @property
    def state(self) -> MediaPlayerState | None:
        if self.coordinator.data is False:
            return MediaPlayerState.OFF
        if self.coordinator.data is True:
            media = self._media_state
            if media is not None and media.state in ("playing", "paused", "buffering"):
                return MediaPlayerState(media.state)
            return MediaPlayerState.ON
        return None

    @property
    def _media_state(self):
        return self.media_routing.choose(self._backend_state)

    @property
    def supported_features(self) -> Feature:
        features = Feature.TURN_ON | Feature.TURN_OFF
        for target in self.media_routing.command_candidates(self._backend_state):
            features |= Feature(int(target.attributes.get("supported_features", 0))) & (
                _NATIVE_FEATURES & ~Feature.SELECT_SOURCE
            )
        backend = self._backend_state
        if backend:
            features |= Feature(int(backend.attributes.get("supported_features", 0))) & Feature.SELECT_SOURCE
        return features

    @property
    def extra_state_attributes(self):
        target = self._media_state
        return {
            "media_control_entity_id": target.entity_id if target else None,
            "media_control_source": self.media_routing.source if self.media_routing.configured else "tv",
        }

    @callback
    def _copy_native_attributes(self) -> None:
        """Populate the public media properties; HA builds the state attributes."""
        backend = self._backend_state
        target = self._media_state
        attributes = target.attributes if (
            self.coordinator.data is True and target is not None
            and target.state not in ("off", "unknown", "unavailable")
        ) else {}
        for name in _NATIVE_PROPERTIES:
            setattr(self, f"_attr_{name}", attributes.get(name))
        self._attr_source = backend.attributes.get("source") if backend and self.coordinator.data is True else None
        self._attr_source_list = backend.attributes.get("source_list") if backend else None
        self._attr_media_image_url = attributes.get("entity_picture")

    @callback
    def _handle_coordinator_update(self) -> None:
        self._copy_native_attributes()
        super()._handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        self._copy_native_attributes()
        await super().async_added_to_hass()
        self._subscribe_backend()
        self.async_on_remove(self._unsubscribe_backend)
        self.async_on_remove(async_track_state_change_event(
            self.hass, [self.runtime.remote_entity_id], self._async_dependency_changed
        ))

    @callback
    def _subscribe_backend(self) -> None:
        self._unsubscribe_backend()
        entity_ids = self.media_routing.entity_ids
        if entity_id := self.backend_entity_id:
            entity_ids.append(entity_id)
        if entity_ids:
            self._backend_unsubscribers = [
                async_track_state_change_event(
                    self.hass, entity_ids, self._async_dependency_changed
                ),
                async_track_entity_registry_updated_event(
                    self.hass, entity_ids, self._async_backend_registry_changed
                ),
            ]

    @callback
    def _unsubscribe_backend(self) -> None:
        for unsubscribe in self._backend_unsubscribers:
            unsubscribe()
        self._backend_unsubscribers = []

    @callback
    def _async_dependency_changed(self, _event) -> None:
        self._copy_native_attributes()
        self.async_write_ha_state()

    @callback
    def _async_backend_registry_changed(self, _event) -> None:
        self._subscribe_backend()
        data = self.runtime.entry.data
        changes = self.media_routing.renamed_data()
        if (entity_id := self.backend_entity_id) and data.get(CONF_TV_ENTITY_ID) != entity_id:
            changes[CONF_TV_ENTITY_ID] = entity_id
        if changes:
            self.hass.config_entries.async_update_entry(
                self.runtime.entry, data={**data, **changes}
            )
        self._copy_native_attributes()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        await self.async_cancel_power()
        # Entity-ID renames remove/re-add this same instance. Listener removal
        # stops polling; ConfigEntry owns permanent coordinator shutdown.
        await super().async_will_remove_from_hass()

    async def async_turn_on(self) -> None:
        await self._async_power(True)

    async def async_turn_off(self) -> None:
        await self._async_power(False)

    async def async_toggle(self) -> None:
        await self._async_power(None)

    async def _async_power(self, turn_on: bool | None) -> None:
        retry_after = False
        async with self._power_lock:
            task = self._power_task
            if task is not None and not task.done():
                retry_after = turn_on is not None and turn_on != self._power_target
            else:
                observed = self.coordinator.data
                target = (not observed if observed is not None else None) if turn_on is None else turn_on
                if observed is not None and target == observed:
                    return
                if not self.runtime.available:
                    raise HomeAssistantError(f"IR transmitter {self.runtime.remote_entity_id} is unavailable")
                self._power_target = target
                task = self._power_task = self.runtime.hass.async_create_task(
                    self._async_transition(target), "EKF Samsung power transition"
                )
        # Caller cancellation cannot release the guard while the TV is booting.
        await asyncio.shield(task)
        if retry_after:
            await self._async_power(turn_on)

    async def _async_transition(self, target: bool | None) -> None:
        command = "power" if target is None else (
            SAMSUNG_ON_COMMAND if target else SAMSUNG_OFF_COMMAND
        )
        try:
            async with asyncio.timeout(TV_TRANSITION_TIMEOUT):
                await self.runtime.transport.async_send(samsung_pulses(command))
                ready = asyncio.Event()

                @callback
                def updated() -> None:
                    if target is not None and self.coordinator.data is target:
                        ready.set()

                unsubscribe = self.coordinator.async_add_listener(updated)
                try:
                    updated()
                    await ready.wait()
                finally:
                    unsubscribe()
        except TimeoutError as err:
            if target is not None:
                raise HomeAssistantError(
                    "Samsung TV did not confirm the requested power state through its network endpoint"
                ) from err
            # Unknown-state toggle has no inferred target; guard the full window.

    async def async_cancel_power(self) -> None:
        task = self._power_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _async_native(self, service: str, **data: Any) -> None:
        backend = self._backend_state
        entity_id = self.backend_entity_id
        if entity_id is None or entity_id == self.entity_id:
            raise HomeAssistantError("The native Samsung TV backend is missing or recursive")
        if (
            self.coordinator.data is False or not self._native_available
            or backend is None or backend.state == "off"
        ):
            raise HomeAssistantError("The native Samsung TV backend is not ready")
        required = _NATIVE_SERVICES[service]
        if self.supported_features & required != required:
            raise HomeAssistantError(f"The native Samsung TV does not support {service}")
        await self.runtime.hass.services.async_call(
            "media_player", service, {**data, "entity_id": entity_id},
            blocking=True, context=self._context,
        )

    async def _async_media(self, service: str, **data: Any) -> None:
        if self.coordinator.data is False:
            raise HomeAssistantError("The television is off")
        required = _NATIVE_SERVICES[service]
        candidates = self.media_routing.command_candidates(self._backend_state)
        if service == "play_media" and data.get("media_content_type") == "app":
            android = self.media_routing.state(CONF_TV_ANDROIDTV_ENTITY_ID)
            if self.media_routing.source == "chromecast" and self.media_routing.ready(android):
                candidates = [android]
        target = next((
            item for item in candidates
            if self.media_routing.ready(item)
            and item.entity_id != self.entity_id
            and Feature(int(item.attributes.get("supported_features", 0))) & required == required
        ), None)
        if target is None:
            raise HomeAssistantError(f"The active media source is not ready for {service}")
        await self.runtime.hass.services.async_call(
            "media_player", service, {**data, "entity_id": target.entity_id},
            blocking=True, context=self._context,
        )

    async def async_select_source(self, source: str) -> None:
        await self._async_native("select_source", source=source)

    async def async_set_volume_level(self, volume: float) -> None:
        await self._async_media("volume_set", volume_level=volume)

    async def async_mute_volume(self, mute: bool) -> None:
        await self._async_media("volume_mute", is_volume_muted=mute)

    async def async_volume_up(self) -> None:
        await self._async_media("volume_up")

    async def async_volume_down(self) -> None:
        await self._async_media("volume_down")

    async def async_media_play(self) -> None:
        await self._async_media("media_play")

    async def async_media_pause(self) -> None:
        await self._async_media("media_pause")

    async def async_media_play_pause(self) -> None:
        await self._async_media("media_play_pause")

    async def async_media_stop(self) -> None:
        await self._async_media("media_stop")

    async def async_media_next_track(self) -> None:
        await self._async_media("media_next_track")

    async def async_media_previous_track(self) -> None:
        await self._async_media("media_previous_track")

    async def async_play_media(self, media_type: str, media_id: str, **kwargs: Any) -> None:
        await self._async_media(
            "play_media", **{**kwargs, "media_content_type": media_type, "media_content_id": media_id}
        )
