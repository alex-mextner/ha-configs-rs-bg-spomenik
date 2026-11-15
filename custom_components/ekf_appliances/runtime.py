"""Shared HA services and identity for the two local appliances."""
import asyncio
from contextvars import ContextVar
import time

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN, CONF_REMOTE_ENTITY_ID, CONF_TEMPERATURE_ENTITY_ID,
    CONF_PROJECTOR_POWER_ENTITY_ID, CONF_PROJECTOR_ON_SCRIPT,
    CONF_PROJECTOR_OFF_SCRIPT, DEFAULT_PROJECTOR_ON_SCRIPT,
    DEFAULT_PROJECTOR_OFF_SCRIPT,
)
from .protocol import haier_pulses, projector_pulses
from .transport import Transport
from .projector_shutdown import ProjectorShutdown


PROJECTOR_TRANSITION_TIMEOUT = 35.0
PROJECTOR_SHUTDOWN_TIMEOUT = 75.0
UNIVERSAL_PROJECTOR_COMMAND = ContextVar("universal_projector_command", default=False)


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
        self.power_pairs = []
        self.projector_power_pair = None
        self.tv_power_pair = None
        self._projector_power_lock = asyncio.Lock()
        self._projector_power_task: asyncio.Task | None = None
        self._projector_power_target: bool | None = None
        self.shutdown = ProjectorShutdown(changed=self._shutdown_changed)
        self._shutdown_store = None
        self._shutdown_save_lock = asyncio.Lock()
        self._shutdown_save_task = None
        self._shutdown_poll = None
        self._shutdown_unsubscribe = None
        self.shutdown_callbacks = []

    @property
    def projector_shutting_down(self) -> bool:
        return self.shutdown.active

    async def async_setup_shutdown(self) -> None:
        """Restore through HA's supported Store before publishing any routes."""
        self._shutdown_store = Store(self.hass, 1, f"ekf_projector_shutdown_{self.entry.entry_id}")
        data = await self._shutdown_store.async_load()
        self.shutdown.restore(data, time.time())
        @callback
        def observed(_event):
            self.check_shutdown_feedback()
        self._shutdown_unsubscribe = async_track_state_change_event(
            self.hass, [self.projector_power_entity_id], observed
        )
        self.check_shutdown_feedback()

    async def _save_shutdown(self) -> None:
        async with self._shutdown_save_lock:
            await self._shutdown_store.async_save(self.shutdown.serialize())

    def _shutdown_changed(self) -> None:
        if self._shutdown_store is not None:
            self._shutdown_save_task = self.hass.async_create_task(self._save_shutdown())
            if self.shutdown.active and self._shutdown_poll is None:
                self._shutdown_poll = self.hass.loop.call_later(1, self._poll_shutdown_feedback)
        if not self.shutdown.active and self._shutdown_poll is not None:
            self._shutdown_poll.cancel()
            self._shutdown_poll = None
        if controller := getattr(self, "remote_controller", None):
            controller.refresh()
        for callback in tuple(self.shutdown_callbacks):
            callback()

    def _poll_shutdown_feedback(self) -> None:
        self._shutdown_poll = None
        self.check_shutdown_feedback()
        if self.shutdown.active:
            self._shutdown_poll = self.hass.loop.call_later(1, self._poll_shutdown_feedback)

    def check_shutdown_feedback(self) -> None:
        if not self.projector_power_entity_id.startswith("binary_sensor."):
            return
        state = self.hass.states.get(self.projector_power_entity_id)
        if state is None:
            return
        reported = state.last_reported.timestamp()
        self.shutdown.observe(
            state.state == "on" if state.state in {"on", "off"} else None,
            reported, fresh=0 <= time.time() - reported <= 12,
            restored=bool(state.attributes.get("restored")),
        )

    def note_projector_off_requested(self, target=False) -> None:
        if target is False:
            self.shutdown.request_off(self.projector_power_state, time.time())

    def observe_projector_ir(self, frame, _captured_at) -> None:
        if frame.get("address") == 0xBD00 and frame.get("command") == 0xFE01:
            self.note_projector_off_requested()

    async def async_close_shutdown(self) -> None:
        if self._shutdown_unsubscribe is not None:
            self._shutdown_unsubscribe()
            self._shutdown_unsubscribe = None
        if self._shutdown_poll is not None:
            self._shutdown_poll.cancel()
            self._shutdown_poll = None
        if self._shutdown_save_task is not None:
            await self._shutdown_save_task

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
        if gateway:
            info["via_device_id"] = gateway.id
        return info

    async def _async_send(self, command: str) -> None:
        if receiver := getattr(self, "receiver", None):
            await receiver.async_before_send()
        if UNIVERSAL_PROJECTOR_COMMAND.get() and self.projector_shutting_down:
            raise HomeAssistantError("Projector is shutting down; remote command was discarded")
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
        await self.transport.async_send(haier_pulses(state), repeats=1)

    async def async_send_projector(self, command: str) -> None:
        await self.transport.async_send(projector_pulses(command))

    @property
    def projector_power_state(self) -> bool | None:
        """Return observed power only; a legacy helper is not feedback."""
        if not self.projector_power_entity_id.startswith("binary_sensor."):
            return None
        state = self.hass.states.get(self.projector_power_entity_id)
        if state is None or state.state not in ("on", "off"):
            return None
        if state.attributes.get("restored") or (dt_util.utcnow() - state.last_reported).total_seconds() > 12:
            return None
        return state.state == "on"

    async def async_projector_power(self, turn_on: bool | None) -> None:
        """Coalesce duplicate requests while the physical transition is pending.

        None is an explicit POWER press: choose direction from observed state,
        or send one raw frame if that state is unknown.
        """
        if turn_on is True and self.projector_shutting_down:
            raise HomeAssistantError("Projector is shutting down; wait for fresh HDMI OFF")
        await self._async_record_projector_off(turn_on)
        retry_after = False
        async with self._projector_power_lock:
            task = self._projector_power_task
            if task is not None and not task.done():
                retry_after = (
                    turn_on is not None
                    and turn_on != self._projector_power_target
                )
            else:
                task = self._start_projector_transition(turn_on)
                if task is None:
                    return

        # A caller timeout must not release the pending guard and permit a
        # second POWER during the projector's measured 17-second boot.
        await asyncio.shield(task)
        if retry_after:
            await self.async_projector_power(turn_on)

    async def _async_record_projector_off(self, turn_on):
        if turn_on is False or (turn_on is None and self.projector_power_state is True):
            self.note_projector_off_requested()
            if self._shutdown_save_task is not None:
                await asyncio.shield(self._shutdown_save_task)

    def _start_projector_transition(self, turn_on):
        observed = self.projector_power_state
        if observed is None and turn_on is not None:
            raise HomeAssistantError(
                "Projector power is unknown; use its single Power button"
            )
        target = (not observed if observed is not None else None) if turn_on is None else turn_on
        if observed is not None and target == observed:
            return None
        if not self.available:
            raise HomeAssistantError(f"IR transmitter {self.remote_entity_id} is unavailable")
        self._projector_power_target = target
        task = self._projector_power_task = self.hass.async_create_task(
            self._async_projector_transition(target, observed),
            "EKF projector power transition",
        )
        return task

    async def _async_projector_transition(self, target: bool | None, observed: bool | None) -> None:
        """Run the physical sequence, then wait for confirmation without guessing."""
        try:
            timeout = (
                PROJECTOR_SHUTDOWN_TIMEOUT
                if target is False else PROJECTOR_TRANSITION_TIMEOUT
            )
            async with asyncio.timeout(timeout):
                if observed is None:
                    # Unknown power never permits an automatic confirmation press.
                    await self.async_send_projector("power")
                else:
                    script = self.projector_on_script if target else self.projector_off_script
                    domain, _, service = script.partition(".")
                    if domain != "script" or not self.hass.services.has_service(domain, service):
                        raise HomeAssistantError(f"Projector power script {script} is unavailable")
                    await self.hass.services.async_call(domain, service, {}, blocking=True)
                await self._async_wait_for_projector_feedback(target)
        except TimeoutError as err:
            if target is not None:
                raise HomeAssistantError(
                    "Projector did not confirm the requested power state through HDMI"
                ) from err
            # A raw press from unknown state has no inferred target. Keep the
            # guard for the whole transition window, then allow another press.

    async def _async_wait_for_projector_feedback(self, target: bool | None) -> None:
        ready = asyncio.Event()

        @callback
        def changed(_event) -> None:
            if target is not None and self.projector_power_state is target:
                ready.set()

        unsubscribe = async_track_state_change_event(
            self.hass, [self.projector_power_entity_id], changed
        )
        try:
            changed(None)
            await ready.wait()
        finally:
            unsubscribe()

    async def async_cancel_projector_power(self) -> None:
        """Cancel the transition when its owning entity is removed."""
        task = self._projector_power_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
