"""Bind private keyboard providers after the native integrations have started."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from homeassistant.helpers import entity_registry as er

from .const import CONF_TV_ANDROIDTV_ENTITY_ID, CONF_TV_KODI_ENTITY_ID, DOMAIN
from .keyboard import KeyboardCoordinator, KeyboardTarget
from .keyboard_android import AndroidKeyboardProvider, native_remote_id
from .keyboard_kodi import KodiKeyboardProvider
from .keyboard_notifications import KeyboardNotifications

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def resolved_host_config(config: dict[str, Any]) -> tuple[str, str] | None:
    """Reuse the resolved existing host bridge credentials without copying secrets."""
    try:
        command = config["rest_command"]["host_kodi_start"]
        url = urlsplit(command["url"])
        token = command["headers"]["X-HA-Host-Token"]
        if (
            url.scheme not in ("http", "https")
            or not url.netloc
            or url.username
            or url.password
            or url.path != "/kodi/start"
            or not isinstance(token, str)
            or not token
        ):
            return None
        return url.scheme + "://" + url.netloc, token
    except (KeyError, TypeError, ValueError):
        return None


async def async_start_keyboard(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bind configured native providers while allowing failed setup to retry."""
    runtime = entry.runtime_data
    if getattr(runtime, "keyboard_closing", False):
        return
    data = hass.data.setdefault(DOMAIN, {})
    if data.get("keyboard_owner") not in (None, entry.entry_id):
        return
    if getattr(runtime, "keyboard_coordinator", None) is not None:
        return
    data["keyboard_owner"] = entry.entry_id
    providers = []
    targets = {}
    configured = {**entry.data, **entry.options}
    media_id = configured.get(CONF_TV_ANDROIDTV_ENTITY_ID)
    if media_id and (remote_id := native_remote_id(hass, media_id)):
        android = AndroidKeyboardProvider(hass, media_id)
        providers.append(android)
        targets["tv"] = KeyboardTarget(remote_id, android)
    kodi_id = configured.get(CONF_TV_KODI_ENTITY_ID)
    host = data.get("keyboard_host")
    registry = er.async_get(hass)
    record = registry.async_get(kodi_id) if kodi_id else None
    if (
        host
        and record is not None
        and record.platform == "kodi"
        and record.config_entry_id
    ):
        kodi = KodiKeyboardProvider(
            hass,
            kodi_id,
            host[0],
            host[1],
            command_allowed=lambda: not runtime.projector_shutting_down,
        )
        providers.append(kodi)
        targets["projector"] = KeyboardTarget(kodi_id, kodi)
    runtime.keyboard_providers = providers
    if not targets:
        data.pop("keyboard_owner", None)
        return
    coordinator = KeyboardCoordinator(hass, targets)
    runtime.keyboard_coordinator = coordinator
    data["keyboard_coordinator"] = coordinator
    try:
        for provider in providers:
            await provider.async_start()
            if getattr(runtime, "keyboard_closing", False):
                await async_stop_keyboard(hass, entry)
                return
        await coordinator.async_start(KeyboardNotifications(hass, targets))
    except Exception as exc:  # noqa: BLE001 -- Optional native setup must not stop controls.
        _LOGGER.warning("Keyboard initialization failed (%s)", type(exc).__name__)
        await _async_close_keyboard(hass, entry)
        # A phone transport failure must not stop physical appliance controls.


async def async_stop_keyboard(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Permanently close keyboard transports when the integration unloads."""
    runtime = entry.runtime_data
    runtime.keyboard_closing = True
    await _async_close_keyboard(hass, entry)


async def _async_close_keyboard(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Release partial initialization without turning a failure into unload."""
    runtime = entry.runtime_data
    data = hass.data.setdefault(DOMAIN, {})
    coordinator = getattr(runtime, "keyboard_coordinator", None)
    if data.get("keyboard_owner") == entry.entry_id:
        data.pop("keyboard_coordinator", None)
        data.pop("keyboard_owner", None)
    if coordinator is not None:
        await coordinator.async_close()
        runtime.keyboard_coordinator = None
    for provider in getattr(runtime, "keyboard_providers", ()):
        await provider.async_shutdown()
    runtime.keyboard_providers = []
