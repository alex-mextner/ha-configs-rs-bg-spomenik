"""Separate local appliances using the EKF infrared remote."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .runtime import ApplianceRuntime

PLATFORMS = [Platform.CLIMATE, Platform.MEDIA_PLAYER, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Build runtime and entities without transmitting anything."""
    entry.runtime_data = ApplianceRuntime(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove entities; unloading must not change appliance power."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
