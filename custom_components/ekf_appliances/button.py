"""Every verified projector key, including navigation and source cycling."""
from homeassistant.components.button import ButtonEntity

from .entity import EkfEntity
from .protocol import PROJECTOR_COMMANDS
from .runtime import ApplianceRuntime

NAMES = {
    "power":"Power", "play_pause":"Play pause", "source":"Source",
    "up":"Up", "down":"Down", "left":"Left", "right":"Right",
    "menu":"Menu", "back":"Back", "vol_down":"Volume down",
    "mute":"Mute", "vol_up":"Volume up",
}


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([ProjectorButton(entry.runtime_data, key) for key in PROJECTOR_COMMANDS])


class ProjectorButton(EkfEntity, ButtonEntity):
    """One frame; power double-press handling belongs to the existing script."""

    def __init__(self, runtime: ApplianceRuntime, command: str) -> None:
        if command not in PROJECTOR_COMMANDS:
            raise ValueError(f"Unknown projector key: {command}")
        super().__init__(runtime, "projector", f"projector_{command}")
        self.command = command
        self._attr_name = NAMES[command]
        self._attr_suggested_object_id = f"ekf_projector_{command}"

    async def async_press(self) -> None:
        await self.runtime.async_send_projector(self.command)
