"""Select the player that owns playback on a television display."""
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_TV_CAST_ENTITY_ID, CONF_TV_ANDROIDTV_ENTITY_ID,
    CONF_TV_MUSIC_ASSISTANT_ENTITY_ID, CONF_TV_KODI_ENTITY_ID,
    CONF_TV_SOURCE_ENTITY_ID,
)

MEDIA_CONFIG_KEYS = (
    CONF_TV_CAST_ENTITY_ID, CONF_TV_ANDROIDTV_ENTITY_ID,
    CONF_TV_MUSIC_ASSISTANT_ENTITY_ID, CONF_TV_KODI_ENTITY_ID,
    CONF_TV_SOURCE_ENTITY_ID,
)
_ACTIVE = {"playing", "paused", "buffering"}
_UNAVAILABLE = {"off", "unknown", "unavailable"}
_MA_CAST_APP = "C35B0678"


class TelevisionMediaRouting:
    """Use Cast session ownership, never a stale MA playing state alone."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        registry = er.async_get(hass)
        self._registry_ids = {}
        for key in MEDIA_CONFIG_KEYS:
            entity_id = entry.data.get(key)
            if entity_id and (entity := registry.async_get(entity_id)):
                self._registry_ids[key] = entity.id

    def entity_id(self, key):
        if registry_id := self._registry_ids.get(key):
            entity = er.async_get(self.hass).async_get(registry_id)
            return entity.entity_id if entity else None
        return self.entry.data.get(key)

    @property
    def entity_ids(self):
        return [entity_id for key in MEDIA_CONFIG_KEYS if (entity_id := self.entity_id(key))]

    def state(self, key):
        entity_id = self.entity_id(key)
        return self.hass.states.get(entity_id) if entity_id else None

    @staticmethod
    def ready(state):
        return state is not None and state.state not in _UNAVAILABLE

    @property
    def configured(self):
        return any(self.entity_id(key) for key in MEDIA_CONFIG_KEYS[:-1])

    def same_cast_player(self):
        registry = er.async_get(self.hass)
        cast_id = self.entity_id(CONF_TV_CAST_ENTITY_ID)
        music_id = self.entity_id(CONF_TV_MUSIC_ASSISTANT_ENTITY_ID)
        cast = registry.async_get(cast_id) if cast_id else None
        music = registry.async_get(music_id) if music_id else None
        return (
            cast is not None and music is not None
            and cast.platform == "cast" and music.platform == "music_assistant"
            and cast.unique_id == music.unique_id
        )

    @property
    def source(self):
        source = self.state(CONF_TV_SOURCE_ENTITY_ID)
        if self.entity_id(CONF_TV_SOURCE_ENTITY_ID):
            return source.state.lower() if source else "unknown"
        return "chromecast"

    def choose(self, native):
        if not self.configured:
            return native
        if self.source in {"pc", "projector", "kodi"}:
            kodi = self.state(CONF_TV_KODI_ENTITY_ID)
            return kodi if self.ready(kodi) else None
        if self.source in {"tv", "hdmi"}:
            return native
        if self.source != "chromecast":
            return None
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
                owns_session and self.same_cast_player() and self.ready(music)
                and music.attributes.get("app_id") == "music_assistant"
                and music.attributes.get("source") == "Music Assistant Queue"
                and music.attributes.get("active_queue")
            ):
                return music
            return cast
        if self.ready(android):
            return android
        return cast if self.ready(cast) else None

    def command_candidates(self, native):
        primary = self.choose(native)
        result = [primary] if primary is not None else []
        # Cast has complete metadata but some Android apps lack MediaSession
        # actions. Only those unsupported actions fall back to Android Remote.
        if (
            primary is not None and self.source == "chromecast"
            and primary.entity_id == self.entity_id(CONF_TV_CAST_ENTITY_ID)
        ):
            android = self.state(CONF_TV_ANDROIDTV_ENTITY_ID)
            if self.ready(android):
                result.append(android)
        return result

    def renamed_data(self):
        return {
            key: entity_id for key in MEDIA_CONFIG_KEYS
            if (entity_id := self.entity_id(key)) and entity_id != self.entry.data.get(key)
        }
