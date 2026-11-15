"""Yandex Dialogs preparation, with no synthesis or uploads on route changes."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re

from aiohttp import ClientTimeout, FormData

from .core import atomic_write

API = "https://dialogs.yandex.net/api/v1"
UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class ProviderNotReady(Exception):
    """A public reason code; never includes a credential or remote response body."""


def load_settings(path: Path) -> dict:
    try:
        settings = json.loads(path.read_text())
        return settings if isinstance(settings, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def dialogs_tag(settings: dict, key: str) -> str:
    config = settings.get("dialogs", {})
    skill = config.get("skill_id", "")
    sound = config.get("sounds", {}).get(key, "")
    if not UUID.fullmatch(skill) or not UUID.fullmatch(sound):
        raise ProviderNotReady("needs_dialogs_credentials")
    return f'<speaker audio="dialogs-upload/{skill}/{sound}.opus">'


async def processed_sound(request, skill, sound):
    for _ in range(30):
        if sound.get("error"):
            raise ProviderNotReady("dialogs_audio_rejected")
        if sound.get("isProcessed"):
            break
        await asyncio.sleep(1)
        sound = (await request("GET", f"/skills/{skill}/sounds/{sound['id']}"))["sound"]
    else:
        raise ProviderNotReady("dialogs_audio_processing")
    if not UUID.fullmatch(sound.get("id", "")):
        raise ProviderNotReady("dialogs_invalid_response")
    return sound["id"]


async def prepare_dialogs(session, path: Path, clips: dict, settings: dict) -> dict:
    config = settings.get("dialogs", {})
    token, skill = config.get("oauth_token"), config.get("skill_id", "")
    if not token or not UUID.fullmatch(skill):
        raise ProviderNotReady("needs_dialogs_credentials")
    headers = {"Authorization": "OAuth " + token}

    async def request(method, route, **kwargs):
        async with session.request(method, API + route, headers=headers,
                timeout=ClientTimeout(total=20), allow_redirects=False, **kwargs) as response:
            if response.status in (401, 403):
                raise ProviderNotReady("dialogs_permission_denied")
            if response.status >= 400:
                raise ProviderNotReady("dialogs_upload_failed")
            return await response.json()

    await request("GET", "/status")
    listing = await request("GET", f"/skills/{skill}/sounds")
    existing = {item.get("originalName"): item for item in listing.get("sounds", [])}
    sounds = config.setdefault("sounds", {})
    for clip in clips.values():
        name = clip.key + ".ogg"
        sound = existing.get(name)
        if sound is None:
            data = FormData()
            data.add_field("file", await asyncio.to_thread(clip.path.read_bytes),
                filename=name, content_type="audio/ogg")
            sound = (await request("POST", f"/skills/{skill}/sounds", data=data))["sound"]
        sounds[clip.key] = await processed_sound(request, skill, sound)
        # Persist each ready mapping so another prepare does not upload duplicates.
        await asyncio.to_thread(atomic_write, path, json.dumps(settings).encode())
    return settings
