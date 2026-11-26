"""Select the player that owns playback on a television display."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, State


from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_TV_ANDROIDTV_ENTITY_ID,
    CONF_TV_CAST_ENTITY_ID,
    CONF_TV_KODI_ENTITY_ID,
    CONF_TV_MUSIC_ASSISTANT_ENTITY_ID,
    CONF_TV_SOURCE_ENTITY_ID,
)

TV_PLAYBACK_CONFIG_KEYS = (
    CONF_TV_CAST_ENTITY_ID,
    CONF_TV_ANDROIDTV_ENTITY_ID,
    CONF_TV_MUSIC_ASSISTANT_ENTITY_ID,
)
MEDIA_CONFIG_KEYS = (
    *TV_PLAYBACK_CONFIG_KEYS,
    CONF_TV_KODI_ENTITY_ID,
    CONF_TV_SOURCE_ENTITY_ID,
)
_ACTIVE = {"playing", "paused", "buffering"}
_CAST_SESSION_SERVICES = {
    "media_play",
    "media_pause",
    "media_play_pause",
    "media_stop",
    "media_next_track",
    "media_previous_track",
}
_UNAVAILABLE = {"off", "unknown", "unavailable"}
_MA_CAST_APP = "C35B0678"


class TelevisionMediaRouting:
    """Use Cast session ownership, never a stale MA playing state alone."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Record registry identities for the configured media sources."""
        self.hass = hass
        self.entry = entry
        registry = er.async_get(hass)
        self._registry_ids = {}
        for key in MEDIA_CONFIG_KEYS:
            entity_id = entry.data.get(key)
            if entity_id and (entity := registry.async_get(entity_id)):
                self._registry_ids[key] = entity.id

    def entity_id(self, key: str) -> str | None:
        """Resolve an entity ID from its stable registry identity."""
        if registry_id := self._registry_ids.get(key):
            entity = er.async_get(self.hass).async_get(registry_id)
            return entity.entity_id if entity else None
        return self.entry.data.get(key)

    @property
    def entity_ids(self) -> list[str]:
        """Return the currently configured media entity IDs."""
        return [
            entity_id for key in MEDIA_CONFIG_KEYS if (entity_id := self.entity_id(key))
        ]

    def state(self, key: str) -> State | None:
        """Read current state for a configured source."""
        entity_id = self.entity_id(key)
        return self.hass.states.get(entity_id) if entity_id else None

    @staticmethod
    def ready(state: State | None) -> bool:
        """Accept only an available, powered media source."""
        return state is not None and state.state not in _UNAVAILABLE

    @property
    def configured(self) -> bool:
        """Report whether a TV playback adapter is configured."""
        return any(self.entity_id(key) for key in TV_PLAYBACK_CONFIG_KEYS)

    def same_cast_player(self) -> bool:
        """Match Cast and Music Assistant by registry identity."""
        registry = er.async_get(self.hass)
        cast_id = self.entity_id(CONF_TV_CAST_ENTITY_ID)
        music_id = self.entity_id(CONF_TV_MUSIC_ASSISTANT_ENTITY_ID)
        cast = registry.async_get(cast_id) if cast_id else None
        music = registry.async_get(music_id) if music_id else None
        return (
            cast is not None
            and music is not None
            and cast.platform == "cast"
            and music.platform == "music_assistant"
            and cast.unique_id == music.unique_id
        )

    @property
    def source(self) -> str:
        # This physical TV is permanently connected to Chromecast. Remote
        # targeting (TV/projector/native menus) is not a media-input selector.
        """Return the physical display source independently of remote mode."""
        return "chromecast"

    def choose(self, native: State | None) -> State | None:
        """Select the playback owner from actual source state."""
        if not self.configured:
            return native
        cast = self.state(CONF_TV_CAST_ENTITY_ID)
        music = self.state(CONF_TV_MUSIC_ASSISTANT_ENTITY_ID)
        android = self.state(CONF_TV_ANDROIDTV_ENTITY_ID)
        cast_active = self.ready(cast) and (
            cast.state in _ACTIVE
            or (
                cast.state == "idle"
                and cast.attributes.get("app_id")
                and cast.attributes.get("app_id") != "E8C28D3C"
            )
        )
        if cast_active:
            owns_session = (
                cast.attributes.get("app_id") == _MA_CAST_APP
                or cast.attributes.get("app_name") == "Music Assistant"
            )
            if (
                owns_session
                and self.same_cast_player()
                and self.ready(music)
                and music.attributes.get("app_id") == "music_assistant"
                and music.attributes.get("source") == "Music Assistant Queue"
                and music.attributes.get("active_queue")
            ):
                return music
            return cast
        if self.ready(android):
            return android
        return cast if self.ready(cast) else None

    def capability_states(self, native: State | None) -> list[State]:
        """Advertise configured controls even when every source is in standby."""
        if not self.configured:
            return [native] if native is not None else []
        states = [
            state
            for key in TV_PLAYBACK_CONFIG_KEYS
            if (state := self.state(key)) is not None
        ]
        return states or ([native] if native is not None else [])

    def command_candidates(self, native: State | None, service: str) -> list[State]:
        """List the selected source and its supported Android fallback."""
        primary = self.choose(native)
        if primary is None:
            return []
        if primary.entity_id != self.entity_id(CONF_TV_CAST_ENTITY_ID):
            return [primary]

        # An idle receiver app can advertise PLAY even without a Cast media
        # session. Android still accepts physical transport keys on this device.
        # Cast only publishes a position for an explicit media status. A
        # finished item can report IDLE at 0 while its queue accepts track keys.
        position = primary.attributes.get("media_position")
        idle_queue = (
            primary.state == "idle"
            and service in {"media_next_track", "media_previous_track"}
            and type(position) in (float, int)
            and 0 <= position < float("inf")
        )
        result = (
            [primary]
            if service not in _CAST_SESSION_SERVICES
            or primary.state in _ACTIVE
            or idle_queue
            else []
        )
        android = self.state(CONF_TV_ANDROIDTV_ENTITY_ID)
        if android is not None and self.ready(android):
            result.append(android)
        return result

    def renamed_data(self) -> dict[str, str]:
        """Collect configured entity IDs changed in the registry."""
        return {
            key: entity_id
            for key in MEDIA_CONFIG_KEYS
            if (entity_id := self.entity_id(key))
            and entity_id != self.entry.data.get(key)
        }
