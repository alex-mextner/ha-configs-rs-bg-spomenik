"""One-time brisk Opus preparation; never used by the announcement hot path."""

from __future__ import annotations

import asyncio
import struct

OGG_UNKNOWN_GRANULE = 0xFFFFFFFFFFFFFFFF


def opus_duration(audio: bytes) -> float:
    """Read Ogg granules, including Opus pre-skip, without a second process."""
    if not audio.startswith(b"OggS") or b"OpusHead" not in audio[:256]:
        message = "Expected Ogg Opus audio"
        raise ValueError(message)
    head = audio.index(b"OpusHead")
    pre_skip = struct.unpack_from("<H", audio, head + 10)[0]
    offset, last = 0, 0
    while offset + 27 <= len(audio):
        if audio[offset : offset + 4] != b"OggS":
            message = "Malformed Ogg page"
            raise ValueError(message)
        granule = struct.unpack_from("<Q", audio, offset + 6)[0]
        if granule != OGG_UNKNOWN_GRANULE:
            last = max(last, granule)
        segments = audio[offset + 26]
        offset += 27 + segments + sum(audio[offset + 27 : offset + 27 + segments])
    if offset != len(audio):
        message = "Truncated Ogg audio"
        raise ValueError(message)
    return (last - pre_skip) / 48000


async def brisk_opus(
    raw: bytes, tempo: float, command: list[str] | None = None
) -> tuple[bytes, float]:
    """Bounded ffmpeg process with no shell or files containing raw generated speech."""
    proc = await asyncio.create_subprocess_exec(
        *(command or ["ffmpeg"]),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-af",
        f"silenceremove=start_periods=1:start_duration=0.01:start_threshold=-45dB,atempo={tempo}",
        "-c:a",
        "libopus",
        "-b:a",
        "48k",
        "-f",
        "ogg",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        audio, _ = await asyncio.wait_for(proc.communicate(raw), 30)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    if proc.returncode:
        message = "Announcement encoding failed"
        raise RuntimeError(message)
    return audio, opus_duration(audio)
