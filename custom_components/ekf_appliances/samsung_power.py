"""Observe Samsung power through its passive local identity endpoint."""

from __future__ import annotations

import errno
import logging
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

from aiohttp import ClientConnectorError, ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)
_NEGATIVE_CONFIRMATIONS = 3


class SamsungPowerCoordinator(DataUpdateCoordinator[bool | None]):
    """Report a matching TV, an unreachable endpoint, or an ambiguous observation."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        host: str,
        model: str,
    ) -> None:
        """Bind passive identity polling to the selected native Samsung device."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="Samsung TV power",
            update_interval=timedelta(seconds=5),
            always_update=False,
        )
        self._url = f"http://{host}:8001/api/v2/"
        self._model = model
        self._negative_observations = 0

    def _missing_endpoint(self) -> bool | None:
        self._negative_observations += 1
        # A single lost Wi-Fi response must not switch off the paired player.
        return (
            False
            if self._negative_observations >= _NEGATIVE_CONFIRMATIONS
            else self.data
        )

    def _ambiguous(self) -> None:
        self._negative_observations = 0

    def _connection_observation(
        self,
        error: TimeoutError | ConnectionRefusedError | ClientConnectorError,
        *,
        connected: bool,
    ) -> bool | None:
        """Distinguish a missing endpoint from an ambiguous connected response."""
        if isinstance(error, TimeoutError):
            return self._ambiguous() if connected else self._missing_endpoint()
        if isinstance(error, ConnectionRefusedError):
            return self._missing_endpoint()
        # This TV also returns EHOSTUNREACH while in deep standby.
        if error.os_error.errno in (errno.ECONNREFUSED, errno.EHOSTUNREACH):
            return self._missing_endpoint()
        return self._ambiguous()

    async def _async_update_data(self) -> bool | None:
        connected = False
        try:
            async with async_get_clientsession(self.hass).get(
                self._url,
                timeout=ClientTimeout(total=2),
                allow_redirects=False,
            ) as response:
                connected = True
                if response.status != HTTPStatus.OK:
                    return self._ambiguous()
                payload = await response.json(content_type=None)
        except (TimeoutError, ConnectionRefusedError, ClientConnectorError) as err:
            return self._connection_observation(err, connected=connected)
        except Exception:  # noqa: BLE001 - Poll errors remain unknown; cancellation propagates.
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
