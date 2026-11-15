"""Separate local appliances using the EKF infrared remote."""
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers.start import async_at_started
from .const import DOMAIN
from .runtime import ApplianceRuntime
from .ir_receiver import register_probe_service
from .receiver import ReceiverRouter
from .keyboard import async_register_commands
from .keyboard_setup import resolved_host_config, async_start_keyboard, async_stop_keyboard
from .remote_control import RemoteController, async_register_remote_services

PLATFORMS = [Platform.CLIMATE, Platform.MEDIA_PLAYER, Platform.BUTTON, Platform.EVENT, Platform.SWITCH, Platform.SELECT, Platform.SENSOR]


async def async_setup(hass: HomeAssistant, _config) -> bool:
    """Register guarded wake only for this integration's media entities."""
    service.async_register_platform_entity_service(
        hass, DOMAIN, "wake_if_current", entity_domain=Platform.MEDIA_PLAYER,
        schema={vol.Required("expected_revision"): vol.All(cv.string, vol.Length(min=1, max=80))},
        func="async_wake_if_current",
    )
    async_register_commands(hass)
    async_register_remote_services(hass)
    hass.data.setdefault(DOMAIN, {})["keyboard_host"] = resolved_host_config(_config)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Build runtime and entities without transmitting anything."""
    entry.runtime_data = ApplianceRuntime(hass, entry)
    await entry.runtime_data.async_setup_shutdown()
    entry.runtime_data.remote_controller = controller = RemoteController(entry.runtime_data)
    hass.data.setdefault(DOMAIN, {})["remote_controller"] = controller
    entry.runtime_data.receiver = ReceiverRouter(entry.runtime_data)
    await entry.runtime_data.receiver.async_load()
    register_probe_service(hass)
    entry.async_on_unload(async_at_started(hass, entry.runtime_data.receiver.async_start))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(async_at_started(hass, controller.async_start))

    async def start_keyboard(_hass):
        await async_start_keyboard(hass, entry)

    entry.async_on_unload(async_at_started(hass, start_keyboard))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove entities; unloading must not change appliance power."""
    await async_stop_keyboard(hass, entry)
    await entry.runtime_data.remote_controller.async_close()
    for pair in entry.runtime_data.power_pairs:
        await pair.close()
    await entry.runtime_data.receiver.async_close()
    await entry.runtime_data.async_close_shutdown()
    if hass.data.get(DOMAIN, {}).get("remote_controller") is entry.runtime_data.remote_controller:
        hass.data[DOMAIN].pop("remote_controller", None)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
