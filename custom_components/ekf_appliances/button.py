"""Projector keys, including navigation and source cycling."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .runtime import ApplianceRuntime

from homeassistant.components.button import ButtonEntity

from .entity import EkfEntity
from .protocol import PROJECTOR_COMMANDS

NAMES = {
    "power": "Power",
    "play_pause": "Play pause",
    "source": "Source",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "menu": "Menu",
    "back": "Back",
    "vol_down": "Volume down",
    "mute": "Mute",
    "vol_up": "Volume up",
    "ok": "OK",
}


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry[ApplianceRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create projector key buttons for this config entry."""
    async_add_entities(
        [ProjectorButton(entry.runtime_data, key) for key in PROJECTOR_COMMANDS]
    )


class ProjectorButton(EkfEntity, ButtonEntity):
    """One frame; power double-press handling belongs to the existing script."""

    def __init__(self, runtime: ApplianceRuntime, command: str) -> None:
        """Bind one verified projector command to its HA button."""
        if command not in PROJECTOR_COMMANDS:
            error_message = f"Unknown projector key: {command}"
            raise ValueError(error_message)
        super().__init__(runtime, "projector", f"projector_{command}")
        self.command = command
        self._attr_name = NAMES[command]
        self._attr_suggested_object_id = (
            "projector_ok" if command == "ok" else f"ekf_projector_{command}"
        )

    async def async_press(self) -> None:
        """Send the configured command and record explicit power-off intent."""
        if self.command == "power":
            self.runtime.note_projector_off_requested()
        await self.runtime.async_send_projector(self.command)
