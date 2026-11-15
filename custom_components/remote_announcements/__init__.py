"""Cached Bracho announcements for the confirmed universal-remote target."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import time

from aiohttp import web
import voluptuous as vol

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.tts import async_get_media_source_audio
from homeassistant.components.tts.media_source import generate_media_source_id
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv, discovery, entity_registry, device_registry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event

from .audio import brisk_opus
from .core import ClipCache, DEFAULT_TEMPO, LatestAnnouncement, PHRASES, TargetGate, VOICE_ID
from .core import can_restore_volume, can_resume, speech_volume
from .providers import ProviderNotReady, dialogs_tag, load_settings, prepare_dialogs

DOMAIN = "remote_announcements"
PAUSE_COMPLETION_BUDGET = 2.0
_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema({vol.Optional(DOMAIN): vol.Schema({
    vol.Required("target_entity"): cv.entity_id,
    vol.Required("backend_entity"): cv.entity_id,
    vol.Required("yandex_entity"): cv.entity_id,
    vol.Required("aisis_entity"): cv.entity_id,
    vol.Required("aisis_connected_entity"): cv.entity_id,
    vol.Required("area"): cv.string,
    vol.Optional("tts_entity", default="tts.bracho_bracho"): cv.entity_id,
    vol.Optional("volume_floor", default=0.5): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
    vol.Optional("tempo", default=DEFAULT_TEMPO): vol.All(vol.Coerce(float), vol.Range(min=1, max=1.6)),
    vol.Optional("music_mode", default="native"): vol.In(("native", "pause")),
})}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass, config):
    if DOMAIN not in config:
        return True
    manager = Manager(hass, config[DOMAIN])
    hass.data[DOMAIN] = manager
    hass.http.register_view(ClipView(manager))
    hass.http.register_view(ManifestView(manager))
    await discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config)

    async def prepare_service(call):
        await manager.prepare()

    hass.services.async_register(DOMAIN, "prepare", prepare_service)

    async def start(event=None):
        manager.gate.changed(manager.current_target())
        manager.unsubscribe = async_track_state_change_event(hass,
            [manager.config["target_entity"]], manager.target_changed)
        await manager.prepare()

    async def stop(event):
        if manager.unsubscribe:
            manager.unsubscribe()
        await manager.queue.stop()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    if hass.is_running:
        hass.async_create_task(start())
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, start)
    return True


class SpeechPlayback:
    """State observed during one announcement, including temporary volume changes."""

    def __init__(self, before, applied, pause):
        self.entity = before.entity_id
        self.old_volume = before.attributes.get("volume_level")
        self.muted_before = bool(before.attributes.get("is_volume_muted"))
        self.old_content = before.attributes.get("media_content_id")
        self.applied, self.pause = applied, pause
        self.paused, self.raised = False, False
        self.started = time.monotonic()
        self.recovery_deadline = self.started + PAUSE_COMPLETION_BUDGET if pause else None
        self.speaking, self.ended = asyncio.Event(), asyncio.Event()
        self.override_observed, self.user_changed_volume = False, False

    @callback
    def observe(self, event):
        state = event.data.get("new_state")
        if state is None:
            return
        if self.raised:
            at_override = can_restore_volume(state.attributes.get("volume_level"), self.applied,
                state.attributes.get("is_volume_muted", False))
            if at_override:
                self.override_observed = True
            elif self.override_observed:
                self.user_changed_volume = True
        alice = state.attributes.get("alice_state")
        if alice in ("SPEAKING", "NONE"):
            self.speaking.set()
        elif self.speaking.is_set() and alice == "IDLE":
            self.ended.set()

    def completion_wait(self, normal):
        if self.recovery_deadline is None:
            return normal
        return max(0.0, min(normal, self.recovery_deadline - time.monotonic()))


class Manager:
    def __init__(self, hass, config):
        self.hass, self.config = hass, config
        self.folder = Path(hass.config.path(".tts", DOMAIN))
        self.settings_path = self.folder / "providers.json"
        self.cache = ClipCache(self.folder, VOICE_ID, config["tempo"])
        self.settings = {}
        self.gate = TargetGate()
        self.queue = LatestAnnouncement(self.announce, on_error=self.failed)
        self.prepare_lock = asyncio.Lock()
        self.status = "starting"
        self.public_status = {"voice": "Брачо", "physical_latency_verified": False}
        self.listeners = set()
        self.unsubscribe = None

    @callback
    def update(self, status, **values):
        self.status = status
        self.public_status.update(values)
        for listener in self.listeners:
            listener()

    @callback
    def failed(self, error):
        reason = str(error) if isinstance(error, ProviderNotReady) else type(error).__name__
        self.update(reason if isinstance(error, ProviderNotReady) else "error", reason=reason)
        _LOGGER.info("Announcement not delivered: %s", reason)

    def current_target(self):
        state = self.hass.states.get(self.config["target_entity"])
        return state.state if state else "unknown"

    @callback
    def target_changed(self, event):
        state = event.data.get("new_state")
        old = event.data.get("old_state")
        if state and (state.attributes.get("initialized") is False
                or (old and old.attributes.get("initialized") is False)):
            self.gate.changed(state.state)
            return
        if state and self.gate.changed(state.state):
            self.queue.submit(state.state)

    async def prepare(self):
        async with self.prepare_lock:
            self.update("preparing")
            try:
                async def generate(text):
                    source = generate_media_source_id(self.hass, text, engine=self.config["tts_entity"],
                        language="ru", cache=True, options={"voice_id": VOICE_ID,
                            "backend": "s2.1-pro-free", "latency": "balanced"})
                    _, audio = await async_get_media_source_audio(self.hass, source)
                    return await brisk_opus(audio, self.config["tempo"])

                await self.cache.prepare(generate)
                self.settings = await self.hass.async_add_executor_job(load_settings, self.settings_path)
                yandex_status = "needs_dialogs_credentials"
                try:
                    try:
                        for clip in self.cache.clips.values():
                            dialogs_tag(self.settings, clip.key)
                    except ProviderNotReady:
                        if not self.settings.get("dialogs", {}).get("oauth_token"):
                            raise ProviderNotReady("needs_dialogs_credentials") from None
                        self.settings = await prepare_dialogs(async_get_clientsession(self.hass),
                            self.settings_path, self.cache.clips, self.settings)
                    yandex_status = "ready"
                except ProviderNotReady as error:
                    yandex_status = str(error)
                self.update("prepared", clips_ready=len(self.cache.clips),
                    duration_seconds={key: round(clip.duration, 3) for key, clip in self.cache.clips.items()},
                    yandex_status=yandex_status,
                    aisis_status="ready" if self.aisis_ready() else "needs_aisis_cache_install")
                backend, _ = self.speaker()
                self.update(yandex_status if backend == "yandex" else self.public_status["aisis_status"])
            except Exception as error:
                self.failed(error)

    def aisis_ready(self):
        aisis = self.settings.get("aisis", {})
        return aisis.get("ready") is True and set(aisis.get("keys", [])) == {
            clip.key for clip in self.cache.clips.values()} and len(self.cache.clips) == len(PHRASES)

    def in_area(self, entity_id):
        registry = entity_registry.async_get(self.hass)
        entry = registry.async_get(entity_id)
        if not entry:
            return False
        area = entry.area_id
        if not area and entry.device_id:
            device = device_registry.async_get(self.hass).async_get(entry.device_id)
            area = device.area_id if device else None
        return area == self.config["area"]

    def speaker_candidates(self, explicit):
        candidates = {}
        for backend in ("yandex", "aisis"):
            entity = self.config[backend + "_entity"]
            state = self.hass.states.get(entity)
            if (not state or state.state in ("unknown", "unavailable")
                    or (not explicit and not self.in_area(entity))):
                continue
            if state.attributes.get("restored"):
                continue
            if backend == "aisis" and not self.hass.states.is_state(self.config["aisis_connected_entity"], "on"):
                continue
            candidates[backend] = entity
        return candidates

    def speaker(self):
        choice = self.hass.states.get(self.config["backend_entity"])
        choice = choice.state if choice else "Автоматически"
        candidates = self.speaker_candidates(choice in ("Яндекс", "AISIS"))
        if choice in ("Яндекс", "AISIS"):
            backend = "yandex" if choice == "Яндекс" else "aisis"
            if backend in candidates:
                return backend, candidates[backend]
            raise ProviderNotReady("selected_" + backend + "_speaker_unavailable")
        for backend in ("aisis", "yandex"):
            if backend in candidates and self.hass.states.is_state(candidates[backend], "playing"):
                return backend, candidates[backend]
        for backend in ("yandex", "aisis"):
            if backend in candidates:
                return backend, candidates[backend]
        raise ProviderNotReady("living_room_speaker_unavailable")

    async def call(self, service, entity, **data):
        await self.hass.services.async_call("media_player", service,
            {"entity_id": entity, **data}, blocking=True)

    def playback(self, backend, entity):
        before = self.hass.states.get(entity)
        muted_before = bool(before.attributes.get("is_volume_muted"))
        if backend == "yandex" and "alice_state" not in before.attributes:
            raise ProviderNotReady("yandex_local_speech_connection_unavailable")
        old_volume = before.attributes.get("volume_level")
        if old_volume is None:
            raise ProviderNotReady("speaker_volume_unknown")
        if backend == "aisis" and (muted_before or old_volume < 0.1):
            raise ProviderNotReady("aisis_cannot_restore_muted_or_zero_volume")
        applied = speech_volume(old_volume, self.config["volume_floor"])
        pause = self.config["music_mode"] == "pause" and before.state == "playing"
        if pause and not before.attributes.get("media_content_id"):
            raise ProviderNotReady("cannot_restore_unknown_music")
        return SpeechPlayback(before, applied, pause)

    async def prepare_playback(self, playback):
        if playback.pause:
            await self.call("media_pause", playback.entity)
            playback.paused = True
        if playback.applied > playback.old_volume or playback.muted_before:
            # Yandex volume_set lifts volume-zero mute and keeps the remembered
            # music level; restore both that level and mute after speech.
            playback.raised = True
            await self.call("volume_set", playback.entity, volume_level=playback.applied)

    async def wait_for_completion(self, backend, playback, clip, target, dispatch_ms):
        status = {"target": target, "speaker": playback.entity, "dispatch_ms": dispatch_ms}
        if backend != "yandex":
            # Quasar accepts the request before the device finishes its native TTS buffer.
            await asyncio.sleep(playback.completion_wait(clip.duration + 0.2))
            self.update("sent_unconfirmed", **status, reason="aisis_has_no_speech_completion_event")
            return
        try:
            await asyncio.wait_for(playback.speaking.wait(), playback.completion_wait(3))
            await asyncio.wait_for(playback.ended.wait(), playback.completion_wait(clip.duration + 2))
        except asyncio.TimeoutError:
            self.update("sent_unconfirmed", **status, reason="speaker_completion_not_observed")
        else:
            self.update("ready", **status)

    async def restore_volume(self, playback):
        now = self.hass.states.get(playback.entity)
        if (not now or not playback.raised or playback.user_changed_volume
                or not can_restore_volume(now.attributes.get("volume_level"), playback.applied,
                    now.attributes.get("is_volume_muted", False))):
            return
        try:
            await self.call("volume_set", playback.entity, volume_level=playback.old_volume)
            # A human can change volume/mute while the restore service is awaited.
            # Check again immediately before restoring mute.
            restored = self.hass.states.get(playback.entity)
            if playback.muted_before and restored and can_restore_volume(
                    restored.attributes.get("volume_level"), playback.old_volume,
                    restored.attributes.get("is_volume_muted", False)):
                await self.call("volume_mute", playback.entity, is_volume_muted=True)
        except Exception as error:
            self.failed(error)

    async def restore_playback(self, playback):
        await self.restore_volume(playback)
        now = self.hass.states.get(playback.entity)
        if now and can_resume(playback.old_content, now.attributes.get("media_content_id"),
                now.state, playback.paused):
            await self.call("media_play", playback.entity)

    async def announce(self, target):
        # Never queue stale speech after a later confirmed target has superseded it.
        if target != self.current_target():
            return
        clip = self.cache.get(target)
        if clip is None:
            raise ProviderNotReady("cache_not_ready")
        backend, entity = self.speaker()
        if backend == "yandex":
            payload = dialogs_tag(self.settings, clip.key)
        elif not self.aisis_ready():
            raise ProviderNotReady("needs_aisis_cache_install")
        playback = self.playback(backend, entity)
        unsubscribe = async_track_state_change_event(self.hass, [entity], playback.observe)
        try:
            await self.prepare_playback(playback)
            if backend == "yandex":
                await self.call("play_media", entity, media_content_type="text", media_content_id=payload)
            else:
                await self.hass.services.async_call("aisis_speaker", "speak",
                    {"duid": entity.removeprefix("media_player.aisis_speaker_").upper(),
                     "text": clip.marker}, blocking=True)
            dispatch_ms = round((time.monotonic() - playback.started) * 1000)
            await self.wait_for_completion(backend, playback, clip, target, dispatch_ms)
        finally:
            unsubscribe()
            await self.restore_playback(playback)


class ManifestView(HomeAssistantView):
    url = "/api/remote_announcements/manifest"
    name = "api:remote_announcements:manifest"
    requires_auth = False

    def __init__(self, manager):
        self.manager = manager

    async def get(self, request):
        clips = self.manager.cache.clips
        if len(clips) != len(PHRASES):
            raise web.HTTPServiceUnavailable()
        # Only five nonprivate fixed labels and hashes are public. No settings or tokens.
        return web.json_response({"voice_id": VOICE_ID, "clips": {
            target: {"key": clip.key, "sha256": clip.sha256, "duration": clip.duration}
            for target, clip in clips.items()}})


class ClipView(HomeAssistantView):
    url = "/api/remote_announcements/audio/{key}.ogg"
    name = "api:remote_announcements:audio"
    requires_auth = False

    def __init__(self, manager):
        self.manager = manager

    async def get(self, request, key):
        clip = next((clip for clip in self.manager.cache.clips.values() if clip.key == key), None)
        if clip is None:
            raise web.HTTPNotFound()
        return web.FileResponse(clip.path, headers={"Content-Type": "audio/ogg",
            "Cache-Control": "public, max-age=31536000, immutable"})
