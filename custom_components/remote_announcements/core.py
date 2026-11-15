"""Small, Home Assistant independent announcement state and persistent cache."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Awaitable, Callable

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


def speech_volume(current: float | None, minimum: float) -> float:
    """HA volume is 0..1; speaker volume 5 is 0.5."""
    try:
        level = float(current)
    except (ValueError, TypeError):
        level = minimum
    if not math.isfinite(level):
        level = minimum
    return min(1.0, max(0.0, minimum, level))


def can_restore_volume(current: float | None, applied: float, muted: bool) -> bool:
    """Do not undo a human's volume or mute change during an announcement."""
    return not muted and current is not None and abs(current - applied) < 0.025


def can_resume(before_id: str | None, after_id: str | None, state: str, paused_by_us: bool) -> bool:
    """Resume only our pause while the original media is still selected."""
    return bool(paused_by_us and before_id and before_id == after_id and state == "paused")


class TargetGate:
    """No startup speech, unknown transitions, or repeated route announcements."""
    def __init__(self) -> None:
        self.last: str | None = None

    def changed(self, target: str) -> bool:
        if target not in PHRASES:
            return False
        previous, self.last = self.last, target
        return previous is not None and previous != target


@dataclass(frozen=True)
class Clip:
    target: str
    key: str
    path: Path
    duration: float
    sha256: str

    @property
    def marker(self) -> str:
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
    def __init__(self, folder: Path, voice: str, tempo: float,
                 phrases: dict[str, str] | None = None) -> None:
        self.folder = folder
        self.voice = voice
        self.tempo = tempo
        self.phrases = phrases or PHRASES
        self.clips: dict[str, Clip] = {}
        self.lock = asyncio.Lock()

    def key(self, target: str) -> str:
        spec = [CACHE_VERSION, self.voice, self.tempo, self.phrases[target], "ogg-opus-48000"]
        return hashlib.sha256(json.dumps(spec, ensure_ascii=False).encode()).hexdigest()

    def _read(self, target: str) -> Clip | None:
        key = self.key(target)
        path = self.folder / f"{key}.ogg"
        try:
            info = json.loads((self.folder / f"{key}.json").read_text())
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            duration = float(info["duration"])
            if info["sha256"] != digest or not 0 < duration <= 2:
                return None
            return Clip(target, key, path, duration, digest)
        except (OSError, KeyError, TypeError, ValueError):
            return None

    async def prepare(self, generate: Callable[[str], Awaitable[tuple[bytes, float]]]) -> None:
        async with self.lock:
            for target, text in self.phrases.items():
                clip = await asyncio.to_thread(self._read, target)
                if clip is None:
                    audio, duration = await generate(text)
                    if not audio or not 0 < duration <= 2:
                        raise ValueError("Generated announcement must be nonempty and at most two seconds")
                    key = self.key(target)
                    digest = hashlib.sha256(audio).hexdigest()
                    path = self.folder / f"{key}.ogg"
                    await asyncio.to_thread(atomic_write, path, audio)
                    metadata = {"target": target, "duration": duration, "sha256": digest,
                                "voice": self.voice, "tempo": self.tempo}
                    await asyncio.to_thread(atomic_write, self.folder / f"{key}.json",
                                            json.dumps(metadata).encode())
                    clip = Clip(target, key, path, duration, digest)
                self.clips[target] = clip

    def get(self, target: str) -> Clip | None:
        return self.clips.get(target)


class LatestAnnouncement:
    """Coalesce pending changes while always allowing an active finally to finish."""
    def __init__(self, announce: Callable[[str], Awaitable[None]], debounce: float = 0.075,
                 on_error: Callable[[Exception], None] | None = None) -> None:
        self.announce = announce
        self.debounce = debounce
        self.on_error = on_error or (lambda error: None)
        self.pending: str | None = None
        self.task: asyncio.Task | None = None

    def submit(self, target: str) -> None:
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
                self.on_error(error)

    async def drain(self) -> None:
        if self.task is not None:
            await self.task

    async def stop(self) -> None:
        self.pending = None
        await self.drain()
