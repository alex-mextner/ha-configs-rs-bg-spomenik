"""Observe Samsung power through its passive local identity endpoint."""

from datetime import timedelta
import errno
import logging

from aiohttp import ClientConnectorError, ClientTimeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class SamsungPowerCoordinator(DataUpdateCoordinator[bool | None]):
    """Report a matching TV, an unreachable endpoint, or an ambiguous observation."""

    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, host: str, model: str,
    ) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=config_entry, name="Samsung TV power",
            update_interval=timedelta(seconds=5), always_update=False,
        )
        self._url = f"http://{host}:8001/api/v2/"
        self._model = model

    async def _async_update_data(self) -> bool | None:
        try:
            async with async_get_clientsession(self.hass).get(
                self._url, timeout=ClientTimeout(total=2), allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
        except (TimeoutError, ConnectionRefusedError):
            return False
        except ClientConnectorError as err:
            # This TV also returns EHOSTUNREACH while in deep standby.
            if err.os_error.errno in (errno.ECONNREFUSED, errno.EHOSTUNREACH):
                return False
            return None
        except Exception:
            # A broken response, DNS failure, or other unexpected error is not OFF.
            # CancelledError remains uncaught so HA can stop an in-flight refresh.
            return None
        if not isinstance(payload, dict):
            return None
        device = payload.get("device")
        if not isinstance(device, dict) or device.get("modelName") != self._model:
            return None
        return True
