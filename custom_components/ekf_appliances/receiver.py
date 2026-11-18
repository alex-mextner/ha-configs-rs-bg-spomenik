"""One semantic input stream, with an optional native Odroid diagnostic observer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from homeassistant.core import Event, HomeAssistant

    from .runtime import ApplianceRuntime

import asyncio
import math
from contextlib import suppress

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .ir_decode import RemoteMapper, decode_frames
from .ir_receiver import IrReceiver
from .native_ir import NativeIrReceiver

SOURCES = ("EKF", "Odroid")


class ReceiverRouter:
    """Preserve existing event IDs while explicitly selecting one active receiver."""

    def __init__(self, runtime: ApplianceRuntime) -> None:
        """Bind both receivers and subscribe to deterministic outgoing IR intent."""
        self.runtime = runtime
        self.hass = runtime.hass
        self.remote_id = runtime.entry.data["remote_entity_id"]
        self.ekf = IrReceiver(runtime)
        self.native = NativeIrReceiver(self.hass)
        self.native.frame_observer = runtime.observe_projector_ir
        self.store = Store(
            self.hass, 1, f"universal_ir_source_{runtime.entry.entry_id}"
        )
        self.source = "EKF"
        self.enabled = False
        self.callbacks = []
        self._switching = self._closing = False
        self._lock = asyncio.Lock()
        self._intent_checks: set[asyncio.Task[None]] = set()
        self.ekf.add_callback(lambda name: self._received("EKF", name))
        self.native.add_callback(lambda name: self._received("Odroid", name))
        self._unsub_services = self.hass.bus.async_listen(
            "call_service", self._service_called
        )

    @property
    def active(self) -> IrReceiver | NativeIrReceiver:
        """Return the selected physical receiver."""
        return self.native if self.source == "Odroid" else self.ekf

    @property
    def validated(self) -> bool:
        """Return whether the selected receiver has decoded valid input."""
        return self.active.validated

    @property
    def last_error(self) -> str | None:
        """Return the latest selected-receiver diagnostic error."""
        return self.active.last_error

    @property
    def last_received(self) -> float | None:
        """Return the latest accepted physical reception time."""
        return self.active.last_received

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Combine selected-receiver status with passive native observations."""
        native = self.native.diagnostics
        return {
            **self.active.diagnostics,
            "active_receiver": self.source,
            "native_receiver_status": native.get("receiver_status"),
            "native_receiver_connected": self.native.connected,
            "native_receiver_validated": self.native.validated,
            "native_capture_count": native.get("capture_count", 0),
            "native_event_count": native.get("event_count", 0),
            "native_last_frame": native.get("last_frame"),
            "native_last_lag_ms": native.get("last_lag_ms"),
            "native_suppressed_echo_count": native.get("suppressed_echo_count", 0),
            "native_kernel_key_conflicts": native.get("kernel_key_conflicts", 0),
            "native_last_error": self.native.last_error,
        }

    def add_callback(
        self, callback: Callable[[str | None], None]
    ) -> Callable[[], None]:
        """Subscribe to receiver events and return an unsubscribe callback."""
        self.callbacks.append(callback)
        return lambda: (
            self.callbacks.remove(callback) if callback in self.callbacks else None
        )

    def _received(self, source: str, name: str | None) -> None:
        dispatch = (
            name is not None
            and source == self.source
            and self.enabled
            and not self._switching
            and not self._closing
        )
        controller = getattr(self.runtime, "remote_controller", None)
        if dispatch and controller is not None:
            controller.physical_press(name, self.last_received)
        for listener in tuple(self.callbacks):
            listener(name if dispatch else None)

    def drain_pending(self) -> None:
        """Drain ready native captures before mode deadlines are evaluated."""
        if (
            self.source == "Odroid"
            and self.enabled
            and not self._switching
            and not self._closing
        ):
            self.native.drain_pending()

    def _notify(self) -> None:
        self._received(self.source, None)

    async def async_load(self) -> None:
        """Restore receiver selection and migrate the existing opt-in state."""
        await self.ekf.async_load()
        data = await self.store.async_load()
        if isinstance(data, dict) and data.get("source") in SOURCES:
            self.source = data["source"]
            self.enabled = data.get("enabled") is True
        else:
            # Migrate the user's existing opt-in without changing receiver.
            self.enabled = self.ekf.enabled
        self.ekf.enabled = self.enabled and self.source == "EKF"
        self.native.set_dispatch_enabled(
            enabled=self.enabled and self.source == "Odroid"
        )

    async def _save(self) -> None:
        await self.store.async_save({"source": self.source, "enabled": self.enabled})

    async def async_start(self, _hass: HomeAssistant | None = None) -> None:
        """Start the selected receiver and passive native observation."""
        if self._closing:
            return
        # Native is a passive diagnostic observer until explicitly selected.
        await self.native.async_start()
        if self.source == "EKF":
            await self.ekf.async_start()
        self._notify()

    async def async_set_source(self, source: str) -> None:
        """Switch receivers atomically and restore the previous source on failure."""
        if source not in SOURCES:
            error_message = "Unknown universal remote receiver"
            raise HomeAssistantError(error_message)
        async with self._lock:
            if self._closing:
                error_message = "Universal remote receiver is unloading"
                raise HomeAssistantError(error_message)
            if source == self.source:
                return
            if source == "Odroid" and not self.native.connected:
                error_message = "Odroid IR receiver is not connected"
                raise HomeAssistantError(error_message)
            previous = self.source
            self._switching = True
            self.native.set_dispatch_enabled(enabled=False)
            self.ekf.mapper = RemoteMapper()
            try:
                await self._activate_source(source)
            except BaseException:
                await self._restore_source(previous)
                raise
            finally:
                self._switching = False
                self._notify()

    async def _activate_source(self, source: str) -> None:
        if self.ekf.enabled:
            await self.ekf.async_set_enabled(enabled=False)
        if self._closing:
            error_message = "Universal remote receiver is unloading"
            raise HomeAssistantError(error_message)
        if source == "EKF":
            await self.ekf.async_set_enabled(enabled=self.enabled)
        else:
            self.native.set_dispatch_enabled(enabled=self.enabled)
        if self._closing:
            error_message = "Universal remote receiver is unloading"
            raise HomeAssistantError(error_message)
        self.source = source
        await self._save()

    async def _restore_source(self, previous: str) -> None:
        # Disable the newly started backend before restoring the old one.
        # A source-store failure must not leave a Tuya learner running
        # behind a native dispatcher (which intentionally skips its lock).
        self.native.set_dispatch_enabled(enabled=False)
        if self.ekf.enabled:
            if self._closing:
                self.ekf.enabled = False
            else:
                with suppress(Exception):
                    await self.ekf.async_set_enabled(enabled=False)
        self.source = previous
        if self._closing:
            return
        if previous == "EKF" and self.enabled:
            await self.ekf.async_set_enabled(enabled=True)
            if self._closing:
                self.ekf.enabled = False
        elif previous == "Odroid":
            self.native.set_dispatch_enabled(enabled=self.enabled)

    async def async_set_enabled(self, *, enabled: bool) -> None:
        """Persist and apply semantic dispatch for the selected receiver."""
        async with self._lock:
            if self._closing:
                error_message = "Universal remote receiver is unloading"
                raise HomeAssistantError(error_message)
            enabled = bool(enabled)
            if self.source == "EKF":
                await self.ekf.async_set_enabled(enabled=enabled)
                self.ekf.mapper = RemoteMapper()
            else:
                self.native.set_dispatch_enabled(enabled=enabled)
            self.enabled = enabled
            await self._save()
            self._notify()

    async def async_before_send(self) -> None:
        # Native input never uses the Tuya learner or the shared IR TX lock.
        """Prepare the selected receiver before shared IR transmission."""
        if self.source == "EKF":
            await self.ekf.async_before_send()

    @callback
    def _service_called(self, event: Event) -> None:
        data = event.data
        if data.get("domain") == "script":
            self._observe_off_script(event)
        values = self._outgoing_ir(data)
        if values is None:
            return
        timing = self._transmit_timing(values)
        if timing is None:
            return
        repeats, delay = timing
        self.native.note_transmit(
            values.get("command", []), repeats=repeats, delay=delay
        )
        self._observe_authorized(
            event,
            self._entity_targets(values),
            lambda: self._observe_projector_commands(values.get("command", [])),
        )

    def _observe_off_script(self, event: Event) -> None:
        data = event.data
        off_script = self.runtime.projector_off_script
        values = data.get("service_data") or {}
        targets = self._entity_targets(values) if isinstance(values, dict) else ()
        if data.get("service") == off_script.removeprefix("script."):
            targets = (off_script,)
        elif data.get("service") != "turn_on" or off_script not in targets:
            return
        self._observe_authorized(
            event, targets, self.runtime.note_projector_off_requested
        )

    @staticmethod
    def _entity_targets(values: Mapping[str, Any]) -> tuple[str, ...]:
        targets = values.get("entity_id", [])
        if isinstance(targets, str):
            return (targets,)
        if not isinstance(targets, (list, tuple, set)):
            return ()
        return tuple(target for target in targets if isinstance(target, str))

    def _observe_authorized(
        self, event: Event, targets: tuple[str, ...], observe: Callable[[], None]
    ) -> None:
        if self._closing:
            return
        context = getattr(event, "context", None)
        if context is None or context.user_id is None:
            observe()
            return
        # HA's user lookup reads its loaded auth cache without yielding. Eager
        # execution keeps accepted intent ahead of the destination hardware write.
        task = self.hass.async_create_task(
            self._async_observe_authorized(context.user_id, targets, observe),
            "EKF projector intent permission",
            eager_start=True,
        )
        if not task.done():
            self._intent_checks.add(task)
            task.add_done_callback(self._intent_checks.discard)

    async def _async_observe_authorized(
        self, user_id: str, targets: tuple[str, ...], observe: Callable[[], None]
    ) -> None:
        user = await self.hass.auth.async_get_user(user_id)
        if self._closing or user is None:
            return
        if user.is_admin or all(
            user.permissions.check_entity(target, POLICY_CONTROL) for target in targets
        ):
            observe()

    def _outgoing_ir(self, data: Mapping[str, Any]) -> dict[str, Any] | None:
        if (
            data.get("domain") not in {"remote", "infrared"}
            or data.get("service") != "send_command"
        ):
            return None
        values = data.get("service_data") or {}
        if not isinstance(values, dict):
            return None
        targets = values.get("entity_id", [])
        targets = [targets] if isinstance(targets, str) else targets
        if not isinstance(targets, (list, tuple, set)):
            return None
        aliases = {self.remote_id, self.remote_id.replace("remote.", "infrared.", 1)}
        if not aliases.intersection(targets):
            return None
        return values

    @staticmethod
    def _transmit_timing(values: Mapping[str, Any]) -> tuple[int, float] | None:
        try:
            repeats = max(1, min(20, int(values.get("num_repeats", 1))))
            delay = float(values.get("delay_secs", 0))
            if not math.isfinite(delay):
                return None
            delay = max(0.0, min(30.0, delay))
        except (ValueError, TypeError, OverflowError):
            return None
        return repeats, delay

    def _observe_projector_commands(self, commands: object) -> None:
        commands = [commands] if isinstance(commands, str) else commands
        if isinstance(commands, (list, tuple)):
            for code in commands[:32]:
                for frame in decode_frames(code):
                    self.runtime.observe_projector_ir(frame, 0)

    async def async_probe(self, seconds: float = 30) -> dict[str, Any]:
        """Run the selected receiver diagnostic probe."""
        return await self.active.async_probe(seconds)

    async def async_close(self) -> None:
        """Stop both receivers without changing appliance power."""
        if self._closing:
            return
        self._closing = True
        self.enabled = False
        self._unsub_services()
        checks = tuple(self._intent_checks)
        for task in checks:
            task.cancel()
        if checks:
            await asyncio.gather(*checks, return_exceptions=True)
        await self.native.async_close()
        await self.ekf.async_close()
        self.ekf.enabled = False
