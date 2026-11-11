"""Separate local appliances using the EKF infrared remote."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from homeassistant.helpers.start import async_at_started
from .runtime import ApplianceRuntime
from .ir_receiver import IrReceiver, register_probe_service

PLATFORMS = [Platform.CLIMATE, Platform.MEDIA_PLAYER, Platform.BUTTON, Platform.EVENT, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Build runtime and entities without transmitting anything."""
    entry.runtime_data = ApplianceRuntime(hass, entry)
    entry.runtime_data.receiver = IrReceiver(entry.runtime_data)
    await entry.runtime_data.receiver.async_load()
    register_probe_service(hass)
    entry.async_on_unload(async_at_started(hass, entry.runtime_data.receiver.async_start))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove entities; unloading must not change appliance power."""
    await entry.runtime_data.receiver.async_close()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
