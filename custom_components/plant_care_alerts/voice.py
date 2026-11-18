# Copyright (c) 2026 Alex Ultra
# SPDX-License-Identifier: MIT
"""Select one speech target and use its actual Home Assistant transport."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.exceptions import HomeAssistantError

if TYPE_CHECKING:
    from homeassistant.core import Context

CONF_VOICE_TARGET = "voice_target"


def configured_target(entry: Any, fallback: str | None) -> str | None:
    """Use persisted options first, with YAML as backward-compatible fallback."""
    if entry is None:
        return fallback
    return entry.options.get(
        CONF_VOICE_TARGET, entry.data.get(CONF_VOICE_TARGET, fallback)
    )


def target_available(state: Any) -> bool:
    """Notify entities normally have unknown state; it does not mean offline."""
    if state is None or state.state == "unavailable":
        return False
    return state.entity_id.startswith("notify.") or state.state != "unknown"


async def async_announce(
    hass: Any, target: str | None, message: str, *, context: Context | None = None
) -> None:
    """Speak exactly once through the configured target, never a guessed fallback."""
    if not message.strip():
        return
    state = hass.states.get(target) if target else None
    if not target_available(state):
        error = "Колонка для напоминаний недоступна"
        raise HomeAssistantError(error)
    if target.startswith("notify."):
        await hass.services.async_call(
            "notify",
            "send_message",
            {"entity_id": target, "message": message.strip()},
            blocking=True,
            context=context,
        )
        return
    if target.startswith("media_player.") and (
        state.attributes.get("supported_features", 0)
        & MediaPlayerEntityFeature.PLAY_MEDIA
    ):
        await hass.services.async_call(
            "media_player",
            "play_media",
            {
                "entity_id": target,
                "media_content_type": "text",
                "media_content_id": message.strip(),
            },
            blocking=True,
            context=context,
        )
        return
    error = "У выбранной колонки нет действия для озвучивания текста"  # noqa: RUF001 - Russian UI text.
    raise HomeAssistantError(error)
