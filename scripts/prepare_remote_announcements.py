#!/usr/bin/env python3
"""Prepare the five Bracho clips through HA TTS without playing any audio."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
MAX_TTS_BYTES = 5 * 1024 * 1024


def http_base(value: str) -> str:
    """Require an HTTP(S) HA endpoint without URL credentials or query data."""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        message = "Use an HTTP(S) Home Assistant base URL without credentials or query"
        raise ValueError(message)
    return value.rstrip("/")


def append_log(path: Path, message: str) -> None:
    """Append nonsecret progress from a worker thread."""
    with path.open("a") as log:
        log.write(message)


def module(name: str, path: Path) -> ModuleType:
    """Load the standalone cache helper without importing Home Assistant."""
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


def token_from(path: Path) -> str:
    """Read only the local HA token entry without exposing its contents."""
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "HA_TOKEN":
            return value.strip().strip("\"'")
    message = "HA_TOKEN missing from configured local environment file"
    raise RuntimeError(message)


def parse_args() -> argparse.Namespace:
    """Parse local HA and cache paths before starting asynchronous work."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home() / "homeassistant")
    parser.add_argument("--token-file", type=Path, default=Path.home() / ".env")
    parser.add_argument("--base-url", default="http://localhost:8123")
    parser.add_argument("--container", default="homeassistant-homeassistant-1")
    parser.add_argument("--cache-dir", type=Path)
    return parser.parse_args()


async def main() -> None:
    """Prepare fixed clips through HA TTS without sending speaker commands."""
    args = parse_args()
    os.umask(0o077)
    log_fd, log_name = tempfile.mkstemp(prefix="remote-announcements-", suffix=".log")
    os.close(log_fd)
    sys.stdout.write(f"Log: {log_name}\n")
    sys.stdout.flush()
    core = module(
        "remote_announcements_core",
        ROOT / "custom_components/remote_announcements/core.py",
    )
    audio = module(
        "remote_announcements_audio",
        ROOT / "custom_components/remote_announcements/audio.py",
    )
    token = await asyncio.to_thread(token_from, args.token_file)
    base = http_base(args.base_url)
    cache = core.ClipCache(
        args.cache_dir or args.config / ".tts/remote_announcements",
        core.VOICE_ID,
        core.DEFAULT_TEMPO,
    )
    started = time.monotonic()

    def fetch(text: str) -> bytes:
        """Fetch one fixed phrase and restrict its download to the HA TTS proxy."""
        body = json.dumps(
            {
                "engine_id": "tts.bracho_bracho",
                "message": text,
                "language": "ru",
                "cache": True,
                "options": {
                    "voice_id": core.VOICE_ID,
                    "backend": "s2.1-pro-free",
                    "latency": "balanced",
                },
            }
        ).encode()
        request = Request(  # noqa: S310 -- base is restricted to HTTP(S) by http_base.
            base + "/api/tts_get_url",
            data=body,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=90) as response:  # noqa: S310 -- validated HA HTTP(S) request.
            result = json.load(response)
        audio_path = urlsplit(result["url"]).path
        if not audio_path.startswith("/api/tts_proxy/"):
            message = "Unexpected HA TTS URL"
            raise RuntimeError(message)
        with urlopen(  # noqa: S310 -- validated base plus the checked HA TTS proxy path.
            Request(base + audio_path, headers={"Authorization": "Bearer " + token}),  # noqa: S310 -- no remote host from the TTS response is used.
            timeout=20,
        ) as response:
            return response.read(MAX_TTS_BYTES + 1)

    async def generate(text: str) -> tuple[bytes, float]:
        """Encode one missing phrase and record only its fixed public label."""
        await asyncio.to_thread(
            append_log,
            Path(log_name),
            f"{time.time():.3f} generate Bracho phrase {text}\n",
        )
        raw = await asyncio.to_thread(fetch, text)
        if len(raw) > MAX_TTS_BYTES:
            message = "TTS audio exceeds bound"
            raise RuntimeError(message)
        return await audio.brisk_opus(
            raw,
            core.DEFAULT_TEMPO,
            ["docker", "exec", "-i", args.container, "ffmpeg"]
            if args.container
            else None,
        )

    try:
        await cache.prepare(generate)
        report = {
            target: {
                "duration_seconds": round(clip.duration, 3),
                "key": clip.key,
                "sha256": clip.sha256,
            }
            for target, clip in cache.clips.items()
        }
        output = json.dumps(
            {
                "voice": "Брачо",
                "tempo": core.DEFAULT_TEMPO,
                "clips": report,
                "playback": False,
            },
            ensure_ascii=False,
            indent=2,
        )
        await asyncio.to_thread(
            append_log,
            Path(log_name),
            output + f"\nFinished in {time.monotonic() - started:.3f}s\n",
        )
        sys.stdout.write(output + f"\nLog: {log_name}\n")
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        # Requests may contain authorization internally: print only the exception type.
        safe = "Preparation failed: " + type(error).__name__
        if isinstance(error, OSError) and error.filename:
            safe += " file=" + str(error.filename)
        await asyncio.to_thread(
            append_log,
            Path(log_name),
            safe + f"\nElapsed {time.monotonic() - started:.3f}s\n",
        )
        sys.stderr.write(safe + f"\nLog: {log_name}\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    asyncio.run(main())
