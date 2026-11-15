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
        self._negative_observations = 0

    def _missing_endpoint(self) -> bool | None:
        self._negative_observations += 1
        # A single lost Wi-Fi response must not switch off the paired player.
        return False if self._negative_observations >= 3 else self.data

    def _ambiguous(self) -> None:
        self._negative_observations = 0
        return None

    async def _async_update_data(self) -> bool | None:
        connected = False
        try:
            async with async_get_clientsession(self.hass).get(
                self._url, timeout=ClientTimeout(total=2), allow_redirects=False,
            ) as response:
                connected = True
                if response.status != 200:
                    return self._ambiguous()
                payload = await response.json(content_type=None)
        except TimeoutError:
            return self._ambiguous() if connected else self._missing_endpoint()
        except ConnectionRefusedError:
            return self._missing_endpoint()
        except ClientConnectorError as err:
            # This TV also returns EHOSTUNREACH while in deep standby.
            if err.os_error.errno in (errno.ECONNREFUSED, errno.EHOSTUNREACH):
                return self._missing_endpoint()
            return self._ambiguous()
        except Exception:
            # A broken response, DNS failure, or other unexpected error is not OFF.
            # CancelledError remains uncaught so HA can stop an in-flight refresh.
            return self._ambiguous()
        if not isinstance(payload, dict):
            return self._ambiguous()
        device = payload.get("device")
        if not isinstance(device, dict) or device.get("modelName") != self._model:
            return self._ambiguous()
        self._negative_observations = 0
        return True
