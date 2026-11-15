"""Yandex Dialogs preparation, with no synthesis or uploads on route changes."""

from __future__ import annotations

import asyncio
import json
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import ClientTimeout, FormData

from .core import atomic_write

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from aiohttp import ClientSession

    from .core import Clip

API = "https://dialogs.yandex.net/api/v1"
UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class ProviderNotReadyError(Exception):
    """A public reason code; never includes a credential or remote response body."""


def load_settings(path: Path) -> dict[str, Any]:
    """Read local provider settings while tolerating an absent initial file."""
    try:
        settings = json.loads(path.read_text())
        return settings if isinstance(settings, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def dialogs_tag(settings: dict[str, Any], key: str) -> str:
    """Build a native Dialogs audio tag only for verified resource IDs."""
    config = settings.get("dialogs", {})
    skill = config.get("skill_id", "")
    sound = config.get("sounds", {}).get(key, "")
    if not UUID.fullmatch(skill) or not UUID.fullmatch(sound):
        message = "needs_dialogs_credentials"
        raise ProviderNotReadyError(message)
    return f'<speaker audio="dialogs-upload/{skill}/{sound}.opus">'


async def processed_sound(
    request: Callable[..., Awaitable[dict[str, Any]]], skill: str, sound: dict[str, Any]
) -> str:
    """Wait for a newly uploaded fixed label to finish processing."""
    for _ in range(30):
        if sound.get("error"):
            message = "dialogs_audio_rejected"
            raise ProviderNotReadyError(message)
        if sound.get("isProcessed"):
            break
        await asyncio.sleep(1)
        sound = (await request("GET", f"/skills/{skill}/sounds/{sound['id']}"))["sound"]
    else:
        message = "dialogs_audio_processing"
        raise ProviderNotReadyError(message)
    if not UUID.fullmatch(sound.get("id", "")):
        message = "dialogs_invalid_response"
        raise ProviderNotReadyError(message)
    return sound["id"]


async def prepare_dialogs(
    session: ClientSession, path: Path, clips: dict[str, Clip], settings: dict[str, Any]
) -> dict[str, Any]:
    """Reuse or upload the five clips and persist each ready resource ID."""
    config = settings.get("dialogs", {})
    token, skill = config.get("oauth_token"), config.get("skill_id", "")
    if not token or not UUID.fullmatch(skill):
        message = "needs_dialogs_credentials"
        raise ProviderNotReadyError(message)
    headers = {"Authorization": "OAuth " + token}

    async def request(method: str, route: str, **kwargs: Any) -> dict[str, Any]:
        """Make one scoped Dialogs call without forwarding redirects."""
        async with session.request(
            method,
            API + route,
            headers=headers,
            timeout=ClientTimeout(total=20),
            allow_redirects=False,
            **kwargs,
        ) as response:
            if response.status in (401, 403):
                message = "dialogs_permission_denied"
                raise ProviderNotReadyError(message)
            if response.status >= HTTPStatus.BAD_REQUEST:
                message = "dialogs_upload_failed"
                raise ProviderNotReadyError(message)
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
            data.add_field(
                "file",
                await asyncio.to_thread(clip.path.read_bytes),
                filename=name,
                content_type="audio/ogg",
            )
            sound = (await request("POST", f"/skills/{skill}/sounds", data=data))[
                "sound"
            ]
        sounds[clip.key] = await processed_sound(request, skill, sound)
        # Persist each ready mapping so another prepare does not upload duplicates.
        await asyncio.to_thread(atomic_write, path, json.dumps(settings).encode())
    return settings
