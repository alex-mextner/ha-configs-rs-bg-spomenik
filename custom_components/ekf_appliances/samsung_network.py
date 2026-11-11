"""Validate a working Samsung WebSocket transport before changing its entry."""
import asyncio
from dataclasses import dataclass
from typing import Any

import aiohttp
from samsungtvws.async_remote import SamsungTVWSAsyncRemote
from samsungtvws.exceptions import ConnectionFailure, UnauthorizedError, ResponseError
from websockets.exceptions import WebSocketException

from homeassistant.components.samsungtv.const import VALUE_CONF_NAME
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession


@dataclass(frozen=True)
class PreparedSamsungTransport:
    entry_id: str
    original_data: dict[str, Any]
    data: dict[str, Any]


async def _async_probe(hass, host: str):
    """Read identity and open an authorized connection; never send remote keys."""
    async with asyncio.timeout(10):
        session = async_get_clientsession(hass)
        async with session.get(
            f"http://{host}:8001/api/v2/",
            timeout=aiohttp.ClientTimeout(total=3),
            allow_redirects=False,
        ) as response:
            response.raise_for_status()
            info = await response.json(content_type=None)
        async with SamsungTVWSAsyncRemote(
            host=host, port=8001, timeout=5, name=VALUE_CONF_NAME,
        ) as remote:
            await remote.open()
            return info, remote.token


async def async_prepare_websocket(hass, entry):
    """Return a verified update without applying it or touching stored pairing."""
    if entry is None or entry.domain != "samsungtv":
        raise HomeAssistantError("The selected entry is not Samsung Smart TV")
    original = dict(entry.data)
    if original.get("method") == "websocket" and original.get("port") == 8001:
        return None
    host = original.get("host")
    if not isinstance(host, str) or not host:
        raise HomeAssistantError("The Samsung TV has no configured host")
    try:
        info, token = await _async_probe(hass, host)
    except (aiohttp.ClientError, OSError, TimeoutError, ValueError, ConnectionFailure, UnauthorizedError, ResponseError, WebSocketException):
        raise HomeAssistantError("Could not verify the Samsung WebSocket connection") from None
    if not isinstance(info, dict) or not isinstance(info.get("device"), dict):
        raise HomeAssistantError("The Samsung identity response was invalid")
    device = info["device"]
    actual_id = device.get("udn") or info.get("id")
    if isinstance(actual_id, str):
        actual_id = actual_id.removeprefix("uuid:")
    expected_mac = str(original.get("mac") or "").replace(":", "").lower()
    actual_mac = str(device.get("wifiMac") or "").replace(":", "").lower()
    if (
        device.get("modelName") != original.get("model")
        or (entry.unique_id and actual_id != entry.unique_id)
        or (expected_mac and actual_mac != expected_mac)
    ):
        raise HomeAssistantError("The network response belongs to a different Samsung TV")
    data = {
        key: value for key, value in original.items()
        if key not in {"token", "session_id"}
    }
    data.update(method="websocket", port=8001)
    if token:
        data["token"] = token
    return PreparedSamsungTransport(entry.entry_id, original, data)


@callback
def async_apply_websocket(hass, prepared):
    """Use HA's public API and preserve any concurrent configuration change."""
    if prepared is None:
        return
    entry = hass.config_entries.async_get_entry(prepared.entry_id)
    if entry is None or dict(entry.data) != prepared.original_data:
        raise HomeAssistantError("Samsung configuration changed during validation")
    hass.config_entries.async_update_entry(entry, data=prepared.data)
