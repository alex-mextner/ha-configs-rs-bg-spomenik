#!/usr/bin/env python3
"""Prepare the five Bracho clips through HA TTS without playing any audio."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


def token_from(path):
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "HA_TOKEN":
            return value.strip().strip("\"'")
    raise RuntimeError("HA_TOKEN missing from configured local environment file")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home() / "homeassistant")
    parser.add_argument("--token-file", type=Path, default=Path.home() / ".env")
    parser.add_argument("--base-url", default="http://localhost:8123")
    parser.add_argument("--container", default="homeassistant-homeassistant-1")
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    log_fd, log_name = tempfile.mkstemp(prefix="remote-announcements-", suffix=".log")
    os.close(log_fd)
    print(f"Log: {log_name}", flush=True)
    root = Path(__file__).resolve().parent.parent
    core = module("remote_announcements_core", root / "custom_components/remote_announcements/core.py")
    audio = module("remote_announcements_audio", root / "custom_components/remote_announcements/audio.py")
    token = token_from(args.token_file)
    base = args.base_url.rstrip("/")
    cache = core.ClipCache(args.cache_dir or args.config / ".tts/remote_announcements", core.VOICE_ID, core.DEFAULT_TEMPO)
    started = time.monotonic()

    def fetch(text):
        body = json.dumps({"engine_id": "tts.bracho_bracho", "message": text,
            "language": "ru", "cache": True, "options": {"voice_id": core.VOICE_ID,
                "backend": "s2.1-pro-free", "latency": "balanced"}}).encode()
        request = Request(base + "/api/tts_get_url", data=body,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urlopen(request, timeout=90) as response:
            result = json.load(response)
        audio_path = urlsplit(result["url"]).path
        if not audio_path.startswith("/api/tts_proxy/"):
            raise RuntimeError("Unexpected HA TTS URL")
        with urlopen(Request(base + audio_path,
                headers={"Authorization": "Bearer " + token}), timeout=20) as response:
            return response.read(5 * 1024 * 1024 + 1)

    async def generate(text):
        with Path(log_name).open("a") as log:
            log.write(f"{time.time():.3f} generate Bracho phrase {text}\n")
        raw = await asyncio.to_thread(fetch, text)
        if len(raw) > 5 * 1024 * 1024:
            raise RuntimeError("TTS audio exceeds bound")
        return await audio.brisk_opus(raw, core.DEFAULT_TEMPO,
            ["docker", "exec", "-i", args.container, "ffmpeg"] if args.container else None)

    try:
        await cache.prepare(generate)
        report = {target: {"duration_seconds": round(clip.duration, 3), "key": clip.key,
            "sha256": clip.sha256} for target, clip in cache.clips.items()}
        output = json.dumps({"voice": "Брачо", "tempo": core.DEFAULT_TEMPO,
            "clips": report, "playback": False}, ensure_ascii=False, indent=2)
        with Path(log_name).open("a") as log:
            log.write(output + f"\nFinished in {time.monotonic() - started:.3f}s\n")
        print(output)
        print(f"Log: {log_name}")
    except Exception as error:
        # Requests may contain authorization internally: print only the exception type.
        safe = "Preparation failed: " + type(error).__name__
        if isinstance(error, OSError) and error.filename:
            safe += " file=" + str(error.filename)
        with Path(log_name).open("a") as log:
            log.write(safe + f"\nElapsed {time.monotonic() - started:.3f}s\n")
        print(safe, file=sys.stderr)
        print(f"Log: {log_name}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    asyncio.run(main())
