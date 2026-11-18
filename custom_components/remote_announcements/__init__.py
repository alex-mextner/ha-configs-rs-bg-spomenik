"""Cached Bracho announcements for the confirmed universal-remote target."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.tts import async_get_media_source_audio
from homeassistant.components.tts.media_source import generate_media_source_id
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import SupportsResponse, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry, discovery, entity_registry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event

from .audio import brisk_opus
from .core import (
    DEFAULT_TEMPO,
    PHRASES,
    VOICE_ID,
    ClipCache,
    LatestAnnouncement,
    TargetGate,
    can_restore_volume,
    can_resume,
    exception_info,
    speech_volume,
)
from .providers import (
    ProviderNotReadyError,
    dialogs_command,
    dialogs_tag,
    load_settings,
    prepare_dialogs,
    validate_native_response,
)
from .service_calls import ServiceCalls

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import (
        Event,
        EventStateChangedData,
        HomeAssistant,
        ServiceCall,
        State,
    )
    from homeassistant.helpers.typing import ConfigType

    from .core import Clip

DOMAIN = "remote_announcements"
ATTEMPT_BUDGET = 2.0
RECOVERY_RESERVE = 0.6
COMPLETION_MARGIN = 0.2
AISIS_MINIMUM_VOLUME = 0.1
_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN): vol.Schema(
            {
                vol.Required("target_entity"): cv.entity_id,
                vol.Required("backend_entity"): cv.entity_id,
                vol.Required("yandex_entity"): cv.entity_id,
                vol.Required("aisis_entity"): cv.entity_id,
                vol.Required("aisis_connected_entity"): cv.entity_id,
                vol.Required("area"): cv.string,
                vol.Optional("tts_entity", default="tts.bracho_bracho"): cv.entity_id,
                vol.Optional("volume_floor", default=0.5): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=1)
                ),
                vol.Optional("tempo", default=DEFAULT_TEMPO): vol.All(
                    vol.Coerce(float), vol.Range(min=1, max=1.6)
                ),
                vol.Optional("music_mode", default="native"): vol.In(
                    ("native", "pause")
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the cache, readiness sensor and effective-target listener."""
    if DOMAIN not in config:
        return True
    manager = Manager(hass, config[DOMAIN])
    hass.data[DOMAIN] = manager
    hass.http.register_view(ClipView(manager))
    hass.http.register_view(ManifestView(manager))
    await discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config)

    async def prepare_service(_call: ServiceCall) -> None:
        """Prepare missing clips and provider mappings without speaker playback."""
        await manager.prepare()

    hass.services.async_register(DOMAIN, "prepare", prepare_service)

    async def start(_event: Event | None = None) -> None:
        """Seed the initial target before enabling change announcements."""
        manager.gate.changed(manager.current_target())
        manager.unsubscribe = async_track_state_change_event(
            hass, [manager.config["target_entity"]], manager.target_changed
        )
        await manager.prepare()

    async def stop(_event: Event) -> None:
        """Stop new announcements and bound pending provider shutdown."""
        await manager.close()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    if hass.is_running:
        hass.async_create_task(start())
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, start)
    return True


class SpeechPlayback:
    """State observed during one announcement, including temporary volume changes."""

    def __init__(self, before: State, applied: float, *, pause: bool) -> None:
        """Snapshot original music and volume with one controller deadline."""
        self.entity = before.entity_id
        self.old_volume = before.attributes.get("volume_level")
        self.muted_before = bool(before.attributes.get("is_volume_muted"))
        self.old_content = before.attributes.get("media_content_id")
        self.applied, self.pause = applied, pause
        self.paused, self.raised = False, False
        self.started = time.monotonic()
        self.budget = ATTEMPT_BUDGET
        self.recovery_deadline = self.started + self.budget
        self.work_deadline = self.recovery_deadline - RECOVERY_RESERVE
        self.recovery_failed = False
        self.speaking, self.ended = asyncio.Event(), asyncio.Event()
        self.override_observed, self.user_changed_volume = False, False

    @callback
    def observe(self, event: Event[EventStateChangedData]) -> None:
        """Observe speech completion and later human volume changes."""
        state = event.data.get("new_state")
        if state is None:
            return
        if self.raised:
            at_override = can_restore_volume(
                state.attributes.get("volume_level"),
                self.applied,
                muted=state.attributes.get("is_volume_muted", False),
            )
            if at_override:
                self.override_observed = True
            elif self.override_observed:
                self.user_changed_volume = True
        alice = state.attributes.get("alice_state")
        if alice in ("SPEAKING", "NONE"):
            self.speaking.set()
        elif self.speaking.is_set() and alice == "IDLE":
            self.ended.set()

    def completion_wait(self, normal: float) -> float:
        """Keep completion waits inside the budget reserved for speech."""
        return max(0.0, min(normal, self.work_deadline - time.monotonic()))


class Manager:
    """Coordinate cached speech, speaker choice and bounded playback recovery."""

    def __init__(self, hass: HomeAssistant, config: ConfigType) -> None:
        """Own provider state, prepared clips and serialized announcement work."""
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
        self.generation = 0
        self.closing = False
        self.services = ServiceCalls(hass)

    async def close(self) -> None:
        """Stop target listening while retaining unfinished provider ownership."""
        self.closing = True
        if self.unsubscribe:
            self.unsubscribe()
            self.unsubscribe = None
        await self.queue.stop(grace_seconds=ATTEMPT_BUDGET)
        await self.services.close(0.05)

    @callback
    def update(self, status: str, **values: Any) -> None:
        """Publish nonsecret status and notify the readiness sensor."""
        self.status = status
        self.public_status.update(values)
        for listener in self.listeners:
            listener()

    @callback
    def failed(self, error: Exception) -> None:
        """Report fixed provider reasons or a sanitized exception type."""
        reason = (
            str(error)
            if isinstance(error, ProviderNotReadyError)
            else type(error).__name__
        )
        self.update(
            reason if isinstance(error, ProviderNotReadyError) else "error",
            reason=reason,
        )
        _LOGGER.info("Announcement not delivered: %s", reason)

    def current_target(self) -> str:
        """Read the current effective route from Home Assistant."""
        state = self.hass.states.get(self.config["target_entity"])
        return state.state if state else "unknown"

    @staticmethod
    def lease_ended(old: State | None, state: State) -> bool:
        """Recognize an initialized manual-to-automatic control transition."""
        return bool(
            old
            and old.state in PHRASES
            and state.state in PHRASES
            and state.state != "speaker"
            and old.attributes.get("initialized") is True
            and state.attributes.get("initialized") is True
            and old.attributes.get("lease_until")
            and state.attributes.get("lease_until") is None
        )

    @callback
    def target_changed(self, event: Event[EventStateChangedData]) -> None:
        """Queue confirmed route changes or lease exits; invalidate older attempts."""
        state = event.data.get("new_state")
        old = event.data.get("old_state")
        if self.closing or state is None:
            return
        lease_ended = self.lease_ended(old, state)
        if old is None or state.state != old.state or lease_ended:
            self.generation += 1
        if (
            old is None
            or state.attributes.get("initialized") is False
            or old.attributes.get("initialized") is False
        ):
            self.gate.changed(state.state)
            return
        if self.gate.changed(state.state) or lease_ended:
            self.queue.submit(state.state)

    async def prepare(self) -> None:
        """Generate missing clips once and prepare configured providers."""
        async with self.prepare_lock:
            self.update("preparing")
            try:

                async def generate(text: str) -> tuple[bytes, float]:
                    """Synthesize one fixed label and encode its brisk cached clip."""
                    source = generate_media_source_id(
                        self.hass,
                        text,
                        engine=self.config["tts_entity"],
                        language="ru",
                        cache=True,
                        options={
                            "voice_id": VOICE_ID,
                            "backend": "s2.1-pro-free",
                            "latency": "balanced",
                        },
                    )
                    _, audio = await async_get_media_source_audio(self.hass, source)
                    return await brisk_opus(audio, self.config["tempo"])

                await self.cache.prepare(generate)
                self.settings = await self.hass.async_add_executor_job(
                    load_settings, self.settings_path
                )
                yandex_status = "needs_dialogs_credentials"
                try:
                    try:
                        for clip in self.cache.clips.values():
                            dialogs_tag(self.settings, clip.key)
                    except ProviderNotReadyError:
                        if not self.settings.get("dialogs", {}).get("oauth_token"):
                            message = "needs_dialogs_credentials"
                            raise ProviderNotReadyError(message) from None
                        self.settings = await prepare_dialogs(
                            async_get_clientsession(self.hass),
                            self.settings_path,
                            self.cache.clips,
                            self.settings,
                        )
                    yandex_status = "ready"
                except ProviderNotReadyError as error:
                    yandex_status = str(error)
                self.update(
                    "prepared",
                    clips_ready=len(self.cache.clips),
                    duration_seconds={
                        key: round(clip.duration, 3)
                        for key, clip in self.cache.clips.items()
                    },
                    yandex_status=yandex_status,
                    aisis_status="ready"
                    if self.aisis_ready()
                    else "needs_aisis_cache_install",
                )
                backend, _ = self.speaker()
                self.update(
                    yandex_status
                    if backend == "yandex"
                    else self.public_status["aisis_status"]
                )
            except Exception as error:
                _LOGGER.exception(
                    "Announcement preparation failed", exc_info=exception_info(error)
                )
                self.failed(error)

    def aisis_ready(self) -> bool:
        """Require an authenticated receipt matching all prepared clip keys."""
        aisis = self.settings.get("aisis", {})
        return (
            aisis.get("ready") is True
            and set(aisis.get("keys", []))
            == {clip.key for clip in self.cache.clips.values()}
            and len(self.cache.clips) == len(PHRASES)
        )

    def in_area(self, entity_id: str) -> bool:
        """Resolve entity or device area for automatic speaker choice."""
        registry = entity_registry.async_get(self.hass)
        entry = registry.async_get(entity_id)
        if not entry:
            return False
        area = entry.area_id
        if not area and entry.device_id:
            device = device_registry.async_get(self.hass).async_get(entry.device_id)
            area = device.area_id if device else None
        return area == self.config["area"]

    def speaker_candidates(self, *, explicit: bool) -> dict[str, str]:
        """List available speakers, applying room rules only to auto choice."""
        candidates = {}
        for backend in ("yandex", "aisis"):
            entity = self.config[backend + "_entity"]
            state = self.hass.states.get(entity)
            if (
                not state
                or state.state in ("unknown", "unavailable")
                or (not explicit and not self.in_area(entity))
            ):
                continue
            if state.attributes.get("restored"):
                continue
            if backend == "aisis" and not self.hass.states.is_state(
                self.config["aisis_connected_entity"], "on"
            ):
                continue
            candidates[backend] = entity
        return candidates

    def speaker(self) -> tuple[str, str]:
        """Honor the dedicated selector before considering automatic choice."""
        choice = self.hass.states.get(self.config["backend_entity"])
        choice = choice.state if choice else "Автоматически"
        candidates = self.speaker_candidates(explicit=choice in ("Яндекс", "AISIS"))
        if choice in ("Яндекс", "AISIS"):
            backend = "yandex" if choice == "Яндекс" else "aisis"
            if backend in candidates:
                return backend, candidates[backend]
            raise ProviderNotReadyError("selected_" + backend + "_speaker_unavailable")
        for backend in ("aisis", "yandex"):
            if backend in candidates and self.hass.states.is_state(
                candidates[backend], "playing"
            ):
                return backend, candidates[backend]
        for backend in ("yandex", "aisis"):
            if backend in candidates:
                return backend, candidates[backend]
        message = "living_room_speaker_unavailable"
        raise ProviderNotReadyError(message)

    async def call(
        self,
        service: str,
        entity: str,
        *,
        deadline: float,
        guard: Callable[[], bool] | None = None,
        **data: Any,
    ) -> bool:
        """Call a media service within the remaining acknowledgement budget."""
        return await self.services.call(
            ("media_player", service),
            {"entity_id": entity, **data},
            deadline=deadline,
            guard=guard,
        )

    def playback(self, backend: str, entity: str) -> SpeechPlayback:
        """Validate speaker capabilities and snapshot recoverable playback."""
        before = self.hass.states.get(entity)
        if before is None:
            message = "speaker_unavailable"
            raise ProviderNotReadyError(message)
        muted_before = bool(before.attributes.get("is_volume_muted"))
        if backend == "yandex" and "alice_state" not in before.attributes:
            message = "yandex_local_speech_connection_unavailable"
            raise ProviderNotReadyError(message)
        old_volume = before.attributes.get("volume_level")
        if old_volume is None:
            message = "speaker_volume_unknown"
            raise ProviderNotReadyError(message)
        if backend == "aisis" and (muted_before or old_volume < AISIS_MINIMUM_VOLUME):
            message = "aisis_cannot_restore_muted_or_zero_volume"
            raise ProviderNotReadyError(message)
        applied = speech_volume(old_volume, self.config["volume_floor"])
        pause = self.config["music_mode"] == "pause" and before.state == "playing"
        if pause and not before.attributes.get("media_content_id"):
            message = "cannot_restore_unknown_music"
            raise ProviderNotReadyError(message)
        return SpeechPlayback(before, applied, pause=pause)

    async def prepare_playback(self, playback: SpeechPlayback) -> None:
        """Apply the optional pause and minimum speech volume."""
        if playback.pause:
            # The device may pause before its service acknowledgement times out.
            # Recovery still checks the actual state and original content.
            playback.paused = True
            await self.call(
                "media_pause", playback.entity, deadline=playback.work_deadline
            )
        if playback.applied > playback.old_volume or playback.muted_before:
            # Yandex volume_set lifts volume-zero mute and keeps the remembered
            # music level; restore both that level and mute after speech.
            playback.raised = True
            await self.call(
                "volume_set",
                playback.entity,
                volume_level=playback.applied,
                deadline=playback.work_deadline,
            )

    async def wait_for_completion(
        self,
        backend: str,
        playback: SpeechPlayback,
        clip: Clip,
        target: str,
        dispatch_ms: int,
    ) -> None:
        """Observe native completion within the cached clip time budget."""
        status = {
            "target": target,
            "speaker": playback.entity,
            "dispatch_ms": dispatch_ms,
        }
        if backend != "yandex":
            # Quasar accepts the request before its native TTS buffer finishes.
            await asyncio.sleep(
                playback.completion_wait(clip.duration + COMPLETION_MARGIN)
            )
            self.update(
                "sent_unconfirmed",
                **status,
                reason="aisis_has_no_speech_completion_event",
            )
            return
        try:
            await asyncio.wait_for(
                playback.ended.wait(),
                playback.completion_wait(clip.duration + COMPLETION_MARGIN),
            )
        except TimeoutError:
            self.update(
                "sent_unconfirmed", **status, reason="speaker_completion_not_observed"
            )
        else:
            self.update("ready", **status)

    def volume_unchanged(self, playback: SpeechPlayback, expected: float) -> bool:
        """Require our temporary volume and preserve later human changes."""
        now = self.hass.states.get(playback.entity)
        return bool(
            now
            and playback.raised
            and not playback.user_changed_volume
            and can_restore_volume(
                now.attributes.get("volume_level"),
                expected,
                muted=now.attributes.get("is_volume_muted", False),
            )
        )

    async def restore_volume(self, playback: SpeechPlayback) -> None:
        """Restore volume and mute only while the original guards hold."""
        if not self.volume_unchanged(playback, playback.applied):
            return
        try:
            await self.call(
                "volume_set",
                playback.entity,
                volume_level=playback.old_volume,
                deadline=playback.recovery_deadline,
                guard=lambda: self.volume_unchanged(playback, playback.applied),
            )
            # A human can change volume/mute while the restore service is awaited.
            # Check again immediately before restoring mute.
            restored = self.hass.states.get(playback.entity)
            if (
                playback.muted_before
                and restored
                and can_restore_volume(
                    restored.attributes.get("volume_level"),
                    playback.old_volume,
                    muted=restored.attributes.get("is_volume_muted", False),
                )
            ):
                await self.call(
                    "volume_mute",
                    playback.entity,
                    is_volume_muted=True,
                    deadline=playback.recovery_deadline,
                    guard=lambda: self.volume_unchanged(playback, playback.old_volume),
                )
        except Exception as error:
            _LOGGER.exception(
                "Announcement volume recovery failed", exc_info=exception_info(error)
            )
            playback.recovery_failed = True
            self.failed(error)

    def music_unchanged(self, playback: SpeechPlayback) -> bool:
        """Resume only our pause of the original selected content."""
        now = self.hass.states.get(playback.entity)
        return bool(
            now
            and can_resume(
                playback.old_content,
                now.attributes.get("media_content_id"),
                now.state,
                paused_by_us=playback.paused,
            )
        )

    async def resume_music(self, playback: SpeechPlayback) -> None:
        """Recover music without waiting for volume acknowledgement."""
        if self.music_unchanged(playback):
            try:
                await self.call(
                    "media_play",
                    playback.entity,
                    deadline=playback.recovery_deadline,
                    guard=lambda: self.music_unchanged(playback),
                )
            except Exception as error:
                _LOGGER.exception(
                    "Announcement music recovery failed", exc_info=exception_info(error)
                )
                playback.recovery_failed = True
                self.failed(error)

    async def restore_playback(self, playback: SpeechPlayback) -> None:
        """Recover independent controls and report controller elapsed time."""
        # A lost volume acknowledgement must not postpone the original music.
        await asyncio.gather(self.restore_volume(playback), self.resume_music(playback))
        elapsed = time.monotonic() - playback.started
        self.update(
            "recovery_unconfirmed"
            if playback.recovery_failed or self.services.pending
            else self.status,
            controller_elapsed_ms=round(elapsed * 1000),
            controller_budget_seconds=playback.budget,
            controller_deadline_missed=elapsed > playback.budget,
            pending_service_count=len(self.services.pending),
        )

    def is_current(self, target: str, generation: int) -> bool:
        """Reject closed or superseded announcement attempts."""
        return (
            not self.closing
            and target == self.current_target()
            and generation == self.generation
        )

    def yandex_command(self, clip: Clip) -> dict[str, Any]:
        """Require native response support before changing music or volume."""
        if (
            not self.hass.services.has_service("yandex_station", "send_command")
            or self.hass.services.supports_response("yandex_station", "send_command")
            is SupportsResponse.NONE
        ):
            message = "yandex_native_response_service_unavailable"
            raise ProviderNotReadyError(message)
        return dialogs_command(self.settings, clip.key)

    async def announce(self, target: str) -> None:
        """Dispatch the latest cached label and always attempt guarded recovery."""
        # Never queue stale speech after a later confirmed target has superseded it.
        generation = self.generation
        if not self.is_current(target, generation):
            return
        if self.services.pending:
            message = "speaker_service_recovery_pending"
            raise ProviderNotReadyError(message)
        clip = self.cache.get(target)
        if clip is None:
            message = "cache_not_ready"
            raise ProviderNotReadyError(message)
        backend, entity = self.speaker()
        if backend == "yandex":
            payload = self.yandex_command(clip)
        elif not self.aisis_ready():
            message = "needs_aisis_cache_install"
            raise ProviderNotReadyError(message)
        playback = self.playback(backend, entity)
        unsubscribe = async_track_state_change_event(
            self.hass, [entity], playback.observe
        )
        try:
            await self.prepare_playback(playback)
            if not self.is_current(target, generation):
                self.update("superseded", target=target)
                return
            if backend == "yandex":
                sent = await self.services.call(
                    ("yandex_station", "send_command"),
                    {"entity_id": entity, **payload},
                    deadline=playback.work_deadline,
                    guard=lambda: self.is_current(target, generation),
                    response_validator=validate_native_response,
                )
            else:
                sent = await self.services.call(
                    ("aisis_speaker", "speak"),
                    {
                        "duid": entity.removeprefix(
                            "media_player.aisis_speaker_"
                        ).upper(),
                        "text": clip.marker,
                    },
                    deadline=playback.work_deadline,
                    guard=lambda: self.is_current(target, generation),
                )
            if not sent:
                self.update("superseded", target=target)
                return
            dispatch_ms = round((time.monotonic() - playback.started) * 1000)
            await self.wait_for_completion(backend, playback, clip, target, dispatch_ms)
        finally:
            unsubscribe()
            await self.restore_playback(playback)


class ManifestView(HomeAssistantView):
    """Expose the five nonprivate cached-label descriptions."""

    url = "/api/remote_announcements/manifest"
    name = "api:remote_announcements:manifest"
    requires_auth = False

    def __init__(self, manager: Manager) -> None:
        """Read only the manager cache, without exposing provider settings."""
        self.manager = manager

    async def get(self, _request: web.Request) -> web.Response:
        """Publish only fixed-label clip metadata and hashes."""
        clips = self.manager.cache.clips
        if len(clips) != len(PHRASES):
            raise web.HTTPServiceUnavailable
        # Only five fixed labels and hashes are public, with no settings or tokens.
        return web.json_response(
            {
                "voice_id": VOICE_ID,
                "clips": {
                    target: {
                        "key": clip.key,
                        "sha256": clip.sha256,
                        "duration": clip.duration,
                    }
                    for target, clip in clips.items()
                },
            }
        )


class ClipView(HomeAssistantView):
    """Expose prepared fixed-label audio without provider credentials."""

    url = "/api/remote_announcements/audio/{key}.ogg"
    name = "api:remote_announcements:audio"
    requires_auth = False

    def __init__(self, manager: Manager) -> None:
        """Serve only keys present in the prepared cache."""
        self.manager = manager

    async def get(self, _request: web.Request, key: str) -> web.FileResponse:
        """Return a prepared Opus clip or fail for an unknown key."""
        clip = next(
            (clip for clip in self.manager.cache.clips.values() if clip.key == key),
            None,
        )
        if clip is None:
            raise web.HTTPNotFound
        return web.FileResponse(
            clip.path,
            headers={
                "Content-Type": "audio/ogg",
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )
