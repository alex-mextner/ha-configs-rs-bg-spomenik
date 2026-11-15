"""Small, Home Assistant independent announcement state and persistent cache."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from types import TracebackType

_LOGGER = logging.getLogger(__name__)

PHRASES = {
    "chromecast": "Хромкаст.",
    "tv": "Телевизор.",
    "kodi": "Коди.",
    "projector": "Проектор.",
    "speaker": "Колонка.",
}
VOICE_ID = "5cd1ffa5dbb4419cb5766435139c6d92"
DEFAULT_TEMPO = 1.25
CACHE_VERSION = 1
MARKER_PREFIX = "[ha-remote:"
MAX_CLIP_SECONDS = 2.0
VOLUME_TOLERANCE = 0.025


def exception_info(
    error: BaseException,
) -> tuple[type[BaseException], BaseException, TracebackType | None]:
    """Keep code locations while stripping exception messages and private chains."""
    return type(error), Exception(type(error).__name__), error.__traceback__


def speech_volume(current: float | None, minimum: float) -> float:
    """HA volume is 0..1; speaker volume 5 is 0.5."""
    try:
        level = float(current)
    except (ValueError, TypeError):
        level = minimum
    if not math.isfinite(level):
        level = minimum
    return min(1.0, max(0.0, minimum, level))


def can_restore_volume(current: float | None, applied: float, *, muted: bool) -> bool:
    """Do not undo a human's volume or mute change during an announcement."""
    return (
        not muted and current is not None and abs(current - applied) < VOLUME_TOLERANCE
    )


def can_resume(
    before_id: str | None, after_id: str | None, state: str, *, paused_by_us: bool
) -> bool:
    """Resume only our pause while the original media is still selected."""
    return bool(
        paused_by_us and before_id and before_id == after_id and state == "paused"
    )


class TargetGate:
    """No startup speech, unknown transitions, or repeated route announcements."""

    def __init__(self) -> None:
        """Start without a route so initial reconciliation remains quiet."""
        self.last: str | None = None

    def changed(self, target: str) -> bool:
        """Accept a known changed route after an initial state has been observed."""
        if target not in PHRASES:
            return False
        previous, self.last = self.last, target
        return previous is not None and previous != target


@dataclass(frozen=True)
class Clip:
    """Describe one immutable prepared label and its validated cache identity."""

    target: str
    key: str
    path: Path
    duration: float
    sha256: str

    @property
    def marker(self) -> str:
        """Return the fixed marker understood by the AISIS cache extension."""
        return f"{MARKER_PREFIX}{self.key}]"


def atomic_write(path: Path, content: bytes) -> None:
    """Replace one task-owned cache record; never leave half a ready asset."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    try:
        owner = path.stat() if path.exists() else None
        temporary.write_bytes(content)
        temporary.chmod(0o600)
        if owner is not None and os.geteuid() == 0:
            os.chown(temporary, owner.st_uid, owner.st_gid)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ClipCache:
    """Generation is confined to prepare(); get() only reads prepared metadata."""

    def __init__(
        self,
        folder: Path,
        voice: str,
        tempo: float,
        phrases: dict[str, str] | None = None,
    ) -> None:
        """Own the voice-specific cache folder and generation lock."""
        self.folder = folder
        self.voice = voice
        self.tempo = tempo
        self.phrases = phrases or PHRASES
        self.clips: dict[str, Clip] = {}
        self.lock = asyncio.Lock()

    def key(self, target: str) -> str:
        """Bind a clip key to the voice, phrase, encoding and brisk tempo."""
        spec = [
            CACHE_VERSION,
            self.voice,
            self.tempo,
            self.phrases[target],
            "ogg-opus-48000",
        ]
        return hashlib.sha256(json.dumps(spec, ensure_ascii=False).encode()).hexdigest()

    def _read(self, target: str) -> Clip | None:
        key = self.key(target)
        path = self.folder / f"{key}.ogg"
        try:
            info = json.loads((self.folder / f"{key}.json").read_text())
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            duration = float(info["duration"])
            if info["sha256"] != digest or not 0 < duration <= MAX_CLIP_SECONDS:
                return None
            return Clip(target, key, path, duration, digest)
        except (OSError, KeyError, TypeError, ValueError):
            return None

    async def prepare(
        self, generate: Callable[[str], Awaitable[tuple[bytes, float]]]
    ) -> None:
        """Reuse valid clips and generate only missing or corrupt cache entries."""
        async with self.lock:
            for target, text in self.phrases.items():
                clip = await asyncio.to_thread(self._read, target)
                if clip is None:
                    audio, duration = await generate(text)
                    if not audio or not 0 < duration <= MAX_CLIP_SECONDS:
                        message = "Generated announcement must last at most two seconds"
                        raise ValueError(message)
                    key = self.key(target)
                    digest = hashlib.sha256(audio).hexdigest()
                    path = self.folder / f"{key}.ogg"
                    await asyncio.to_thread(atomic_write, path, audio)
                    metadata = {
                        "target": target,
                        "duration": duration,
                        "sha256": digest,
                        "voice": self.voice,
                        "tempo": self.tempo,
                    }
                    await asyncio.to_thread(
                        atomic_write,
                        self.folder / f"{key}.json",
                        json.dumps(metadata).encode(),
                    )
                    clip = Clip(target, key, path, duration, digest)
                self.clips[target] = clip

    def get(self, target: str) -> Clip | None:
        """Read prepared metadata without network, synthesis or filesystem work."""
        return self.clips.get(target)


class LatestAnnouncement:
    """Coalesce pending changes while always allowing an active finally to finish."""

    def __init__(
        self,
        announce: Callable[[str], Awaitable[None]],
        debounce: float = 0.075,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Serialize active recovery while coalescing later pending targets."""
        self.announce = announce
        self.debounce = debounce
        self.on_error = on_error or (lambda _error: None)
        self.pending: str | None = None
        self.task: asyncio.Task | None = None

    def submit(self, target: str) -> None:
        """Replace the pending target without cancelling an active recovery."""
        self.pending = target
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while self.pending is not None:
            await asyncio.sleep(self.debounce)
            target, self.pending = self.pending, None
            if target is None:
                return
            try:
                await self.announce(target)
            except Exception as error:
                _LOGGER.exception(
                    "Announcement task failed", exc_info=exception_info(error)
                )
                self.on_error(error)

    async def drain(self) -> None:
        """Wait for submitted work, including the active recovery, to finish."""
        if self.task is not None:
            await self.task

    async def stop(self, grace_seconds: float | None = None) -> None:
        """Drop pending work and bound shutdown without abandoning task ownership."""
        self.pending = None
        if self.task is None:
            return
        if grace_seconds is None:
            await self.drain()
            return
        done, _ = await asyncio.wait({self.task}, timeout=grace_seconds)
        if self.task not in done:
            self.task.cancel()
