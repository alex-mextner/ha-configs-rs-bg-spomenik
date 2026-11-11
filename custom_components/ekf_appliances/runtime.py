"""Shared HA services and identity for the two local appliances."""
import asyncio

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    DOMAIN, CONF_REMOTE_ENTITY_ID, CONF_TEMPERATURE_ENTITY_ID,
    CONF_PROJECTOR_POWER_ENTITY_ID, CONF_PROJECTOR_ON_SCRIPT,
    CONF_PROJECTOR_OFF_SCRIPT, DEFAULT_PROJECTOR_ON_SCRIPT,
    DEFAULT_PROJECTOR_OFF_SCRIPT,
)
from .protocol import haier_pulses, projector_pulses
from .transport import Transport


class ApplianceRuntime:
    """Keep every appliance command on the same serialized transmitter."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.remote_entity_id = entry.data[CONF_REMOTE_ENTITY_ID]
        self.temperature_entity_id = entry.data[CONF_TEMPERATURE_ENTITY_ID]
        self.projector_power_entity_id = entry.data[CONF_PROJECTOR_POWER_ENTITY_ID]
        self.projector_on_script = entry.data.get(CONF_PROJECTOR_ON_SCRIPT, DEFAULT_PROJECTOR_ON_SCRIPT)
        self.projector_off_script = entry.data.get(CONF_PROJECTOR_OFF_SCRIPT, DEFAULT_PROJECTOR_OFF_SCRIPT)
        self.transport = Transport(self._async_send)
        self._projector_power_lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        state = self.hass.states.get(self.remote_entity_id)
        return state is not None and state.state not in ("unknown", "unavailable")

    def device_info(self, kind: str) -> dr.DeviceInfo:
        info = dr.DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_{kind}")},
            name="EKF Haier" if kind == "haier" else "EKF Projector",
            manufacturer="Haier" if kind == "haier" else "Toshiba",
            model="HSU-12HT303/R2" if kind == "haier" else "Projector",
        )
        remote = er.async_get(self.hass).async_get(self.remote_entity_id)
        gateway = dr.async_get(self.hass).async_get(remote.device_id) if remote and remote.device_id else None
        if gateway and gateway.identifiers:
            info["via_device"] = sorted(gateway.identifiers)[0]
        return info

    async def _async_send(self, command: str) -> None:
        if not self.available:
            raise HomeAssistantError(f"IR transmitter {self.remote_entity_id} is unavailable")
        try:
            await self.hass.services.async_call(
                "remote", "send_command",
                {"entity_id":self.remote_entity_id, "command":command, "num_repeats":1, "delay_secs":0},
                blocking=True,
            )
        except Exception as err:
            raise HomeAssistantError(f"IR transmitter command failed: {err}") from err

    async def async_send_haier(self, state: bytes) -> None:
        await self.transport.async_send(haier_pulses(state), repeats=6, gap=0.15)

    async def async_send_projector(self, command: str) -> None:
        await self.transport.async_send(projector_pulses(command))

    async def async_projector_power(self, turn_on: bool) -> None:
        # Named scripts call our raw buttons, so never hold the transmitter lock here.
        async with self._projector_power_lock:
            state = self.hass.states.get(self.projector_power_entity_id)
            if state is None or state.state not in ("on", "off"):
                raise HomeAssistantError("Projector power helper is unavailable")
            desired = "on" if turn_on else "off"
            if state.state == desired:
                return
            if not self.available:
                raise HomeAssistantError(f"IR transmitter {self.remote_entity_id} is unavailable")
            script = self.projector_on_script if turn_on else self.projector_off_script
            domain, _, service = script.partition(".")
            if domain != "script" or not self.hass.services.has_service(domain, service):
                raise HomeAssistantError(f"Projector power script {script} is unavailable")
            await self.hass.services.async_call(domain, service, {}, blocking=True)
