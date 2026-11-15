"""Shared HA services and identity for the two local appliances."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event

    from .ir_decode import IrFrame

import asyncio
import time
from contextlib import suppress
from contextvars import ContextVar

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_PROJECTOR_OFF_SCRIPT,
    CONF_PROJECTOR_ON_SCRIPT,
    CONF_PROJECTOR_POWER_ENTITY_ID,
    CONF_REMOTE_ENTITY_ID,
    CONF_TEMPERATURE_ENTITY_ID,
    DEFAULT_PROJECTOR_OFF_SCRIPT,
    DEFAULT_PROJECTOR_ON_SCRIPT,
    DOMAIN,
)
from .projector_shutdown import ProjectorShutdown
from .protocol import haier_pulses, projector_pulses
from .transport import Transport

PROJECTOR_TRANSITION_TIMEOUT = 35.0
PROJECTOR_SHUTDOWN_TIMEOUT = 75.0
UNIVERSAL_PROJECTOR_COMMAND = ContextVar("universal_projector_command", default=False)


_PROJECTOR_FEEDBACK_MAX_AGE = 12
_PROJECTOR_IR_ADDRESS = 0xBD00
_PROJECTOR_POWER_FRAME = 0xFE01


class ApplianceRuntime:
    """Keep every appliance command on the same serialized transmitter."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize shared transport, paired power and the shutdown latch."""
        self.hass = hass
        self.entry = entry
        self.remote_entity_id = entry.data[CONF_REMOTE_ENTITY_ID]
        self.temperature_entity_id = entry.data[CONF_TEMPERATURE_ENTITY_ID]
        self.projector_power_entity_id = entry.data[CONF_PROJECTOR_POWER_ENTITY_ID]
        self.projector_on_script = entry.data.get(
            CONF_PROJECTOR_ON_SCRIPT, DEFAULT_PROJECTOR_ON_SCRIPT
        )
        self.projector_off_script = entry.data.get(
            CONF_PROJECTOR_OFF_SCRIPT, DEFAULT_PROJECTOR_OFF_SCRIPT
        )
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
        """Return whether projector commands are blocked pending fresh HDMI off."""
        return self.shutdown.active

    async def async_setup_shutdown(self) -> None:
        """Restore through HA's supported Store before publishing any routes."""
        self._shutdown_store = Store(
            self.hass, 1, f"ekf_projector_shutdown_{self.entry.entry_id}"
        )
        data = await self._shutdown_store.async_load()
        self.shutdown.restore(data, time.time())

        @callback
        def observed(_event: Event) -> None:
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
            self._shutdown_save_task = self.hass.async_create_task(
                self._save_shutdown()
            )
            if self.shutdown.active and self._shutdown_poll is None:
                self._shutdown_poll = self.hass.loop.call_later(
                    1, self._poll_shutdown_feedback
                )
        if not self.shutdown.active and self._shutdown_poll is not None:
            self._shutdown_poll.cancel()
            self._shutdown_poll = None
        if controller := getattr(self, "remote_controller", None):
            controller.refresh()
        for listener in tuple(self.shutdown_callbacks):
            listener()

    def _poll_shutdown_feedback(self) -> None:
        self._shutdown_poll = None
        self.check_shutdown_feedback()
        if self.shutdown.active:
            self._shutdown_poll = self.hass.loop.call_later(
                1, self._poll_shutdown_feedback
            )

    def check_shutdown_feedback(self) -> None:
        """Observe explicit HDMI reports without guessing unavailable state."""
        if not self.projector_power_entity_id.startswith("binary_sensor."):
            return
        state = self.hass.states.get(self.projector_power_entity_id)
        if state is None:
            return
        reported = state.last_reported.timestamp()
        self.shutdown.observe(
            value=state.state == "on" if state.state in {"on", "off"} else None,
            reported_at=reported,
            fresh=0 <= time.time() - reported <= _PROJECTOR_FEEDBACK_MAX_AGE,
            restored=bool(state.attributes.get("restored")),
        )

    def note_projector_off_requested(self, *, target: bool = False) -> None:
        """Record an accepted explicit off intent while projector power is known on."""
        if target is False:
            self.shutdown.request_off(
                observed=self.projector_power_state, now=time.time()
            )

    def observe_projector_ir(self, frame: IrFrame, _captured_at: float) -> None:
        """Record fresh physical OEM projector power presses as off intent."""
        if (
            frame.get("address") == _PROJECTOR_IR_ADDRESS
            and frame.get("command") == _PROJECTOR_POWER_FRAME
        ):
            self.note_projector_off_requested()

    async def async_close_shutdown(self) -> None:
        """Remove shutdown observers and finish any pending persistent save."""
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
        """Return whether the configured IR transmitter is available."""
        state = self.hass.states.get(self.remote_entity_id)
        return state is not None and state.state not in ("unknown", "unavailable")

    def device_info(self, kind: str) -> dr.DeviceInfo:
        """Return stable identifiers and the shared IR gateway relationship."""
        info = dr.DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_{kind}")},
            name="EKF Haier" if kind == "haier" else "EKF Projector",
            manufacturer="Haier" if kind == "haier" else "Toshiba",
            model="HSU-12HT303/R2" if kind == "haier" else "Projector",
        )
        remote = er.async_get(self.hass).async_get(self.remote_entity_id)
        gateway = (
            dr.async_get(self.hass).async_get(remote.device_id)
            if remote and remote.device_id
            else None
        )
        if gateway:
            info["via_device_id"] = gateway.id
        return info

    async def _async_send(self, command: str) -> None:
        if receiver := getattr(self, "receiver", None):
            await receiver.async_before_send()
        if UNIVERSAL_PROJECTOR_COMMAND.get() and self.projector_shutting_down:
            error_message = "Projector is shutting down; remote command was discarded"
            raise HomeAssistantError(error_message)
        if not self.available:
            error_message = f"IR transmitter {self.remote_entity_id} is unavailable"
            raise HomeAssistantError(error_message)
        try:
            await self.hass.services.async_call(
                "remote",
                "send_command",
                {
                    "entity_id": self.remote_entity_id,
                    "command": command,
                    "num_repeats": 1,
                    "delay_secs": 0,
                },
                blocking=True,
            )
        except Exception as err:
            error_message = f"IR transmitter command failed: {err}"
            raise HomeAssistantError(error_message) from err

    async def async_send_haier(self, state: bytes) -> None:
        """Send one complete validated Haier state through the shared transmitter."""
        await self.transport.async_send(haier_pulses(state), repeats=1)

    async def async_send_projector(self, command: str) -> None:
        """Send one verified projector key through the shared transmitter."""
        await self.transport.async_send(projector_pulses(command))

    @property
    def projector_power_state(self) -> bool | None:
        """Return observed power only; a legacy helper is not feedback."""
        if not self.projector_power_entity_id.startswith("binary_sensor."):
            return None
        state = self.hass.states.get(self.projector_power_entity_id)
        if state is None or state.state not in ("on", "off"):
            return None
        if (
            state.attributes.get("restored")
            or (dt_util.utcnow() - state.last_reported).total_seconds()
            > _PROJECTOR_FEEDBACK_MAX_AGE
        ):
            return None
        return state.state == "on"

    async def async_projector_power(self, *, turn_on: bool | None) -> None:
        """
        Coalesce duplicate requests while the physical transition is pending.

        None is an explicit POWER press: choose direction from observed state,
        or send one raw frame if that state is unknown.
        """
        if turn_on is True and self.projector_shutting_down:
            error_message = "Projector is shutting down; wait for fresh HDMI OFF"
            raise HomeAssistantError(error_message)
        await self._async_record_projector_off(turn_on=turn_on)
        retry_after = False
        async with self._projector_power_lock:
            task = self._projector_power_task
            if task is not None and not task.done():
                retry_after = (
                    turn_on is not None and turn_on != self._projector_power_target
                )
            else:
                task = self._start_projector_transition(turn_on=turn_on)
                if task is None:
                    return

        # A caller timeout must not release the pending guard and permit a
        # second POWER during the projector's measured 17-second boot.
        try:
            await asyncio.shield(task)
        except Exception:
            # A failed earlier direction does not cancel an explicitly queued
            # opposite request. Caller cancellation still propagates unchanged.
            if not retry_after:
                raise
        if retry_after:
            await self.async_projector_power(turn_on=turn_on)

    async def _async_record_projector_off(self, *, turn_on: bool | None) -> None:
        if turn_on is False or (turn_on is None and self.projector_power_state is True):
            self.note_projector_off_requested()
            if self._shutdown_save_task is not None:
                await asyncio.shield(self._shutdown_save_task)

    def _start_projector_transition(
        self, *, turn_on: bool | None
    ) -> asyncio.Task[None] | None:
        observed = self.projector_power_state
        if observed is None and turn_on is not None:
            error_message = "Projector power is unknown; use its single Power button"
            raise HomeAssistantError(error_message)
        target = (
            (not observed if observed is not None else None)
            if turn_on is None
            else turn_on
        )
        if observed is not None and target == observed:
            return None
        if not self.available:
            error_message = f"IR transmitter {self.remote_entity_id} is unavailable"
            raise HomeAssistantError(error_message)
        self._projector_power_target = target
        task = self._projector_power_task = self.hass.async_create_task(
            self._async_projector_transition(target=target, observed=observed),
            "EKF projector power transition",
        )
        return task

    async def _async_projector_transition(
        self, *, target: bool | None, observed: bool | None
    ) -> None:
        """Run the physical sequence, then wait for confirmation without guessing."""
        try:
            timeout = (
                PROJECTOR_SHUTDOWN_TIMEOUT
                if target is False
                else PROJECTOR_TRANSITION_TIMEOUT
            )
            async with asyncio.timeout(timeout):
                if observed is None:
                    # Unknown power never permits an automatic confirmation press.
                    await self.async_send_projector("power")
                else:
                    script = (
                        self.projector_on_script
                        if target
                        else self.projector_off_script
                    )
                    domain, _, service = script.partition(".")
                    if domain != "script" or not self.hass.services.has_service(
                        domain, service
                    ):
                        error_message = (
                            f"Projector power script {script} is unavailable"
                        )
                        raise HomeAssistantError(error_message)
                    await self.hass.services.async_call(
                        domain, service, {}, blocking=True
                    )
                await self._async_wait_for_projector_feedback(target=target)
        except TimeoutError as err:
            if target is not None:
                error_message = (
                    "Projector did not confirm the requested power state through HDMI"
                )
                raise HomeAssistantError(error_message) from err
            # A raw press from unknown state has no inferred target. Keep the
            # guard for the whole transition window, then allow another press.

    async def _async_wait_for_projector_feedback(self, *, target: bool | None) -> None:
        ready = asyncio.Event()

        @callback
        def changed(_event: Event | None) -> None:
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
            with suppress(asyncio.CancelledError):
                await task
