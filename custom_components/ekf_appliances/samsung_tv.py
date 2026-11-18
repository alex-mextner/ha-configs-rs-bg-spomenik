"""Samsung LAN media functions with discrete EKF power and passive feedback."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import Event, State

    from .runtime import ApplianceRuntime


import asyncio
from contextlib import suppress
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerState,
)
from homeassistant.components.media_player import (
    MediaPlayerEntityFeature as Feature,
)
from homeassistant.const import CONF_HOST, CONF_MODEL
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_TV_ANDROIDTV_ENTITY_ID, CONF_TV_ENTITY_ID
from .media_routing import TelevisionMediaRouting
from .paired_media import async_guarded_wake
from .protocol import samsung_pulses
from .samsung_power import SamsungPowerCoordinator

# The panel can take 51 seconds to expose its LAN endpoint after one IR ON.
# Keep duplicate requests guarded until that feedback arrives.
TV_TRANSITION_TIMEOUT = 75.0
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
    "volume_level",
    "is_volume_muted",
    "source",
    "media_content_id",
    "media_content_type",
    "media_duration",
    "media_position",
    "media_position_updated_at",
    "media_title",
    "media_artist",
    "media_album_name",
    "media_album_artist",
    "media_track",
    "media_series_title",
    "media_season",
    "media_episode",
    "media_channel",
    "media_playlist",
    "app_id",
    "app_name",
)


class SamsungTvMediaPlayer(
    CoordinatorEntity[SamsungPowerCoordinator], MediaPlayerEntity
):
    """Combine display power with the media source that owns playback."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_assumed_state = False
    _attr_should_poll = False
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_suggested_object_id = "ekf_samsung_tv"

    def __init__(self, runtime: ApplianceRuntime) -> None:
        """Bind the validated native TV and its passive power coordinator."""
        self.runtime = runtime
        hass = runtime.hass
        registry = er.async_get(hass)
        backend = registry.async_get(runtime.entry.data[CONF_TV_ENTITY_ID])
        native_entry = (
            hass.config_entries.async_get_entry(backend.config_entry_id)
            if backend and backend.config_entry_id
            else None
        )
        device = (
            dr.async_get(hass).async_get(backend.device_id)
            if backend and backend.device_id
            else None
        )
        identifiers = (
            {
                identifier
                for identifier in device.identifiers
                if identifier[0] == "samsungtv"
            }
            if device
            else set()
        )
        if (
            backend is None
            or backend.domain != "media_player"
            or backend.platform != "samsungtv"
            or native_entry is None
            or native_entry.domain != "samsungtv"
            or not identifiers
        ):
            error_message = "Choose a registered native Samsung TV backend"
            raise HomeAssistantError(error_message)
        host = native_entry.data.get(CONF_HOST)
        model = native_entry.data.get(CONF_MODEL)
        if (
            not isinstance(host, str)
            or not host.strip()
            or not isinstance(model, str)
            or not model.strip()
        ):
            error_message = "The native Samsung TV must have a host and model"
            raise HomeAssistantError(error_message)
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
        self._power_generation = 0
        self._copy_native_attributes()

    @property
    def backend_entity_id(self) -> str | None:
        """Resolve the native Samsung entity after registry renames."""
        backend = er.async_get(self.runtime.hass).async_get(self._backend_registry_id)
        return backend.entity_id if backend is not None else None

    @property
    def _backend_state(self) -> State | None:
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
        """Expose working power or media controls independently."""
        return self.runtime.available or (
            self.coordinator.data is not False
            and (self._native_available or self._media_state is not None)
        )

    @property
    def state(self) -> MediaPlayerState | None:
        """Combine confirmed display power with observed playback state."""
        if self.coordinator.data is False:
            return MediaPlayerState.OFF
        if self.coordinator.data is True:
            media = self._media_state
            if media is not None and media.state in ("playing", "paused", "buffering"):
                return MediaPlayerState(media.state)
            return MediaPlayerState.ON
        return None

    @property
    def _media_state(self) -> State | None:
        return self.media_routing.choose(self._backend_state)

    @property
    def supported_features(self) -> Feature:
        """Advertise configured source capabilities and discrete power."""
        features = Feature.TURN_ON | Feature.TURN_OFF
        for target in self.media_routing.capability_states(self._backend_state):
            features |= Feature(int(target.attributes.get("supported_features", 0))) & (
                _NATIVE_FEATURES & ~Feature.SELECT_SOURCE
            )
        backend = self._backend_state
        if backend:
            features |= (
                Feature(int(backend.attributes.get("supported_features", 0)))
                & Feature.SELECT_SOURCE
            )
        return features

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Describe playback ownership and current paired-power intent."""
        target = self._media_state
        return {
            "media_control_entity_id": target.entity_id if target else None,
            "media_control_source": self.media_routing.source
            if self.media_routing.configured
            else "tv",
            "paired_player_entity_id": self.runtime.entry.data.get(
                CONF_TV_ANDROIDTV_ENTITY_ID
            ),
            "power_sync_target": self.runtime.tv_power_pair.target
            if self.runtime.tv_power_pair
            else None,
            "power_sync_error": self.runtime.tv_power_pair.last_error
            if self.runtime.tv_power_pair
            else None,
            "power_intent_revision": self.runtime.tv_power_pair.intent_revision
            if self.runtime.tv_power_pair
            else None,
            "last_power_intent": self.runtime.tv_power_pair.last_intent
            if self.runtime.tv_power_pair
            else None,
        }

    @callback
    def _copy_native_attributes(self) -> None:
        """Populate the public media properties; HA builds the state attributes."""
        backend = self._backend_state
        target = self._media_state
        attributes = (
            target.attributes
            if (
                self.coordinator.data is True
                and target is not None
                and target.state not in ("off", "unknown", "unavailable")
            )
            else {}
        )
        for name in _NATIVE_PROPERTIES:
            setattr(self, f"_attr_{name}", attributes.get(name))
        self._attr_source = (
            backend.attributes.get("source")
            if backend and self.coordinator.data is True
            else None
        )
        self._attr_source_list = (
            backend.attributes.get("source_list") if backend else None
        )
        self._attr_media_image_url = attributes.get("entity_picture")

    @callback
    def _handle_coordinator_update(self) -> None:
        self._copy_native_attributes()
        super()._handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        """Subscribe to source and registry updates after entity setup."""
        self._copy_native_attributes()
        await super().async_added_to_hass()
        self._subscribe_backend()
        self.async_on_remove(self._unsubscribe_backend)
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self.runtime.remote_entity_id],
                self._async_dependency_changed,
            )
        )

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
    def _async_dependency_changed(self, _event: Event) -> None:
        self._copy_native_attributes()
        self.async_write_ha_state()

    @callback
    def _async_backend_registry_changed(self, _event: Event) -> None:
        self._subscribe_backend()
        data = self.runtime.entry.data
        changes = self.media_routing.renamed_data()
        if (entity_id := self.backend_entity_id) and data.get(
            CONF_TV_ENTITY_ID
        ) != entity_id:
            changes[CONF_TV_ENTITY_ID] = entity_id
        if changes:
            self.hass.config_entries.async_update_entry(
                self.runtime.entry, data={**data, **changes}
            )
        self._copy_native_attributes()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel pending power work before removing this entity instance."""
        await self.async_cancel_power()
        # Entity-ID renames remove/re-add this same instance. Listener removal
        # stops polling; ConfigEntry owns permanent coordinator shutdown.
        await super().async_will_remove_from_hass()

    async def _async_pair_power(self, *, target: bool) -> None:
        if pair := self.runtime.tv_power_pair:
            await pair.request(target=target)
        else:
            await self._async_power(target)

    async def async_wake_if_current(self, expected_revision: str) -> None:
        """Wake only while the captured paired-power intent remains current."""
        await async_guarded_wake(
            self.runtime, self.runtime.tv_power_pair, expected_revision
        )

    async def async_turn_on(self) -> None:
        """Request display and paired-player power on."""
        await self._async_pair_power(target=True)

    async def async_turn_off(self) -> None:
        """Request display and paired-player power off."""
        await self._async_pair_power(target=False)

    async def async_toggle(self) -> None:
        """Reverse pending intent, or turn off any known-on member of the pair."""
        observed = self.coordinator.data
        pair = self.runtime.tv_power_pair
        if pair is not None and pair.target is not None:
            observed = pair.target
        elif (
            self._power_task is not None
            and not self._power_task.done()
            and self._power_target is not None
        ):
            observed = self._power_target
        elif pair is not None and pair.endpoints["player"].read() is True:
            observed = True
        if observed is None:
            await self._async_power(None)
        else:
            await self._async_pair_power(target=not observed)

    async def _async_power(self, turn_on: bool | None) -> None:  # noqa: FBT001 - PowerEndpoint invokes this bool callback positionally.
        async with self._power_lock:
            task = self._power_task
            superseding = (
                task is not None
                and not task.done()
                and turn_on is not None
                and turn_on != self._power_target
            )
            if superseding:
                # Discrete OFF must not wait through an earlier ON confirmation.
                # The transport retains its frame deadline across cancellation.
                self._power_generation += 1
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
            if task is None or task.done():
                observed = self.coordinator.data
                target = self._requested_power(turn_on=turn_on, observed=observed)
                if observed is not None and target == observed and not superseding:
                    return
                if not self.runtime.available:
                    error_message = (
                        f"IR transmitter {self.runtime.remote_entity_id} is unavailable"
                    )
                    raise HomeAssistantError(error_message)
                self._power_generation += 1
                self._power_target = target
                task = self._power_task = self.runtime.hass.async_create_task(
                    self._async_transition(target=target),
                    "EKF Samsung power transition",
                )
            generation = self._power_generation
        # Caller cancellation cannot release the guard while the TV is booting.
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if generation != self._power_generation and not caller.cancelling():
                error_message = (
                    "Samsung power request was superseded by a newer request"
                )
                raise HomeAssistantError(error_message) from None
            raise

    @staticmethod
    def _requested_power(*, turn_on: bool | None, observed: bool | None) -> bool | None:
        if turn_on is not None:
            return turn_on
        return not observed if observed is not None else None

    async def _async_transition(self, *, target: bool | None) -> None:
        command = (
            "power"
            if target is None
            else (SAMSUNG_ON_COMMAND if target else SAMSUNG_OFF_COMMAND)
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
                error_message = (
                    "Samsung TV did not confirm the requested power state "
                    "through its network endpoint"
                )
                raise HomeAssistantError(error_message) from err
            # Unknown-state toggle has no inferred target; guard the full window.

    async def async_cancel_power(self) -> None:
        """Cancel and await the outstanding display transition."""
        task = self._power_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _async_native(self, service: str, **data: Any) -> None:
        backend = self._backend_state
        entity_id = self.backend_entity_id
        if entity_id is None or entity_id == self.entity_id:
            error_message = "The native Samsung TV backend is missing or recursive"
            raise HomeAssistantError(error_message)
        if (
            self.coordinator.data is False
            or not self._native_available
            or backend is None
            or backend.state == "off"
        ):
            error_message = "The native Samsung TV backend is not ready"
            raise HomeAssistantError(error_message)
        required = _NATIVE_SERVICES[service]
        if self.supported_features & required != required:
            error_message = f"The native Samsung TV does not support {service}"
            raise HomeAssistantError(error_message)
        await self.runtime.hass.services.async_call(
            "media_player",
            service,
            {**data, "entity_id": entity_id},
            blocking=True,
            context=self._context,
        )

    async def _async_media(self, service: str, **data: Any) -> None:
        if self.coordinator.data is False:
            error_message = "The television is off"
            raise HomeAssistantError(error_message)
        required = _NATIVE_SERVICES[service]
        candidates = self.media_routing.command_candidates(self._backend_state)
        if service == "play_media" and data.get("media_content_type") == "app":
            android = self.media_routing.state(CONF_TV_ANDROIDTV_ENTITY_ID)
            if self.media_routing.source == "chromecast" and self.media_routing.ready(
                android
            ):
                candidates = [android]
        target = next(
            (
                item
                for item in candidates
                if self.media_routing.ready(item)
                and item.entity_id != self.entity_id
                and Feature(int(item.attributes.get("supported_features", 0)))
                & required
                == required
            ),
            None,
        )
        if target is None:
            error_message = f"The active media source is not ready for {service}"
            raise HomeAssistantError(error_message)
        await self.runtime.hass.services.async_call(
            "media_player",
            service,
            {**data, "entity_id": target.entity_id},
            blocking=True,
            context=self._context,
        )

    async def async_select_source(self, source: str) -> None:
        """Forward source selection to the native Samsung entity."""
        await self._async_native("select_source", source=source)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume on the current media owner."""
        await self._async_media("volume_set", volume_level=volume)

    async def async_mute_volume(self, mute: bool) -> None:  # noqa: FBT001 - Home Assistant defines this positional entity callback.
        """Set mute on the current media owner."""
        await self._async_media("volume_mute", is_volume_muted=mute)

    async def async_volume_up(self) -> None:
        """Raise volume on the current media owner."""
        await self._async_media("volume_up")

    async def async_volume_down(self) -> None:
        """Lower volume on the current media owner."""
        await self._async_media("volume_down")

    async def async_media_play(self) -> None:
        """Start playback on the current media owner."""
        await self._async_media("media_play")

    async def async_media_pause(self) -> None:
        """Pause playback on the current media owner."""
        await self._async_media("media_pause")

    async def async_media_play_pause(self) -> None:
        """Toggle playback on the current media owner."""
        await self._async_media("media_play_pause")

    async def async_media_stop(self) -> None:
        """Stop playback on the current media owner."""
        await self._async_media("media_stop")

    async def async_media_next_track(self) -> None:
        """Advance the media owner to its next item."""
        await self._async_media("media_next_track")

    async def async_media_previous_track(self) -> None:
        """Return the media owner to its previous item."""
        await self._async_media("media_previous_track")

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: Any
    ) -> None:
        """Forward a media request to its selected capable source."""
        await self._async_media(
            "play_media",
            **{
                **kwargs,
                "media_content_type": media_type,
                "media_content_id": media_id,
            },
        )
