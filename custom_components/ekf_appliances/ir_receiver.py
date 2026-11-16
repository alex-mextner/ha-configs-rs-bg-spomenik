"""Opt-in IR reception over the existing Tuya Local connection."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from homeassistant.core import Event, HomeAssistant, ServiceCall

    from .ir_decode import IrFrame
    from .runtime import ApplianceRuntime


import asyncio
import json
import logging
import time
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

from homeassistant.components.remote import DATA_COMPONENT as REMOTE_COMPONENT
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

# HA installs its schema compatibility alias before voluptuous is imported.
# isort: split
import voluptuous as vol

from .ir_decode import RemoteMapper, decode_frames, pulses_from_code

_LOGGER = logging.getLogger(__name__)
DOMAIN = "ekf_appliances"
_STUDY_RENEW_SECONDS = 15
_STUDY_SETTLE_SECONDS = 0.1
_TRANSMISSION_GUARD_SECONDS = 1.2


class IrReceiver:
    """The hub has one IR channel: pause listening around outgoing commands."""

    def __init__(self, runtime: ApplianceRuntime) -> None:
        """Bind the existing transmitter and initialize passive receiver state."""
        self.runtime = runtime
        self.hass = runtime.hass
        self.remote_id = runtime.entry.data["remote_entity_id"]
        self.store = Store(self.hass, 1, f"ekf_ir_receiver_{runtime.entry.entry_id}")
        self.enabled = self.validated = False
        self.validated_hub = None
        self.remote = self.device = None
        self.task = None
        self.armed = False
        self.last_arm = self.ignore_until = self.manual_until = 0.0
        self.probe_waiter = None
        self._probe_task = None
        self._closing = False
        self._generation = 0
        self.callbacks = []
        self.mapper = RemoteMapper()
        self.last_received = None
        self.last_error = None
        self.status = "disabled"
        self.study_sessions = self.capture_count = self.event_count = 0
        self.cached_capture_count = self.suppressed_capture_count = 0
        self.last_capture = self.last_study = self.last_packet = None
        self.last_packet_keys = []
        self.last_frame = None
        self._socket_marker = None
        self._config = SimpleNamespace(
            dps=list,
            unique_id=lambda device_id: (
                f"ekf_ir_observer_{runtime.entry.entry_id}_{device_id}"
            ),
        )
        self.entity_id = "event.ekf_universal_remote"
        self.unsub_services = self.hass.bus.async_listen(
            "call_service", self._service_called
        )

    async def async_load(self) -> None:
        """Restore the receiver opt-in and verified hub identity."""
        data = await self.store.async_load() or {}
        self.validated = data.get("validated") is True
        self.validated_hub = data.get("hub")
        self.enabled = data.get("enabled") is True

    def _storage_data(self) -> dict[str, Any]:
        return {
            "validated": self.validated,
            "hub": self.validated_hub,
            "enabled": self.enabled,
        }

    async def _save(self) -> None:
        await self.store.async_save(self._storage_data())

    def add_callback(
        self, callback: Callable[[str | None], None]
    ) -> Callable[[], None]:
        """Subscribe to decoded events and return a removal callback."""
        self.callbacks.append(callback)
        return lambda: (
            self.callbacks.remove(callback) if callback in self.callbacks else None
        )

    def _notify(self, event: str | None = None) -> None:
        for listener in tuple(self.callbacks):
            listener(event)

    def _native_connection(self) -> tuple[bool, object | None, bool]:
        """Observe the existing reader; a mode ACK alone cannot prove reception."""
        device = self.device
        reader = getattr(device, "_refresh_task", None)
        running = bool(
            getattr(device, "_running", False)
            and reader is not None
            and not reader.done()
        )
        api = getattr(device, "_api", None)
        socket = getattr(api, "socket", None)
        working = bool(getattr(device, "_api_protocol_working", False))
        return running, socket, working

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Report observed reader and capture state without claiming reception."""
        running, socket, working = self._native_connection()
        return {
            "receiver_status": self.status,
            "listener_running": self.task is not None and not self.task.done(),
            "native_reader_running": running,
            "native_socket_connected": socket is not None,
            "native_api_working": working,
            # This means a request was sent, not that hardware listening was proven.
            "study_requested": self.armed,
            "study_sessions": self.study_sessions,
            "last_study_requested": self.last_study,
            "capture_count": self.capture_count,
            "event_count": self.event_count,
            "cached_capture_count": self.cached_capture_count,
            "suppressed_capture_count": self.suppressed_capture_count,
            "last_packet": self.last_packet,
            "last_packet_keys": self.last_packet_keys,
            "last_capture": self.last_capture,
            "last_frame": self.last_frame,
        }

    def _set_status(self, status: str, error: str | None = None) -> None:
        changed = status != self.status or error != self.last_error
        self.status, self.last_error = status, error
        if changed:
            self._notify()

    def async_schedule_update_ha_state(self, *_args: Any) -> None:
        """Forward the Tuya Local observer state callback."""
        self._notify()

    def schedule_update_ha_state(self, *_args: Any) -> None:
        """Forward the Tuya Local observer state callback."""
        self._notify()

    @callback
    def _service_called(self, event: Event) -> None:
        data = event.data
        domain, service = data.get("domain"), data.get("service")
        if domain not in {"remote", "infrared"} or service not in {
            "send_command",
            "learn_command",
        }:
            return
        values = data.get("service_data") or {}
        targets = values.get("entity_id", [])
        targets = [targets] if isinstance(targets, str) else targets
        if (
            targets
            and self.remote_id not in targets
            and self.remote_id.replace("remote.", "infrared.", 1) not in targets
        ):
            return
        self._generation += 1
        now = time.monotonic()
        if service == "learn_command":
            commands = values.get("command", [])
            count = len(commands) if isinstance(commands, list) else 1
            self.manual_until = max(self.manual_until, now + 35 * max(1, count))
            self.ignore_until = max(self.ignore_until, self.manual_until)
        else:
            self._suppress_transmission(values, now)
        self.armed = False
        self._set_status(
            "paused_learning" if service == "learn_command" else "paused_transmit"
        )

    def _suppress_transmission(self, values: Mapping[str, Any], now: float) -> None:
        commands = values.get("command", [])
        commands = [commands] if isinstance(commands, str) else commands
        duration = sum(
            sum(pulses_from_code(c)) / 1_000_000 for c in commands if isinstance(c, str)
        )
        repeats = max(1, int(values.get("num_repeats", 1)))
        delay = max(0.0, float(values.get("delay_secs", 0)))
        self.ignore_until = max(
            self.ignore_until,
            now + max(1.2, repeats * (duration + len(commands) * delay) + 0.8),
        )

    async def _attach(self) -> None:
        component = self.hass.data.get(REMOTE_COMPONENT)
        remote = component.get_entity(self.remote_id) if component else None
        if remote is None:
            error_message = "EKF remote is not loaded"
            raise HomeAssistantError(error_message)
        if remote is self.remote:
            return
        device = getattr(remote, "_device", None)
        send = getattr(remote, "_send_dp", None)
        receive = getattr(remote, "_receive_dp", None)
        if (
            device is None
            or str(getattr(send, "id", "")) != "201"
            or str(getattr(receive, "id", "")) != "202"
            or not hasattr(device, "register_entity")
            or not hasattr(remote, "_lock")
            or not hasattr(device, "_api_lock")
            or not hasattr(device, "_retry_on_failed_connection")
            or not hasattr(getattr(device, "_api", None), "set_multiple_values")
        ):
            error_message = "This Tuya Local remote does not expose DP201/DP202"
            raise HomeAssistantError(error_message)
        if self.device is not None:
            with suppress(ValueError):
                await self.device.async_unregister_entity(self)
        self.remote, self.device = remote, device
        self.armed = False
        self._socket_marker = None
        device.register_entity(self)
        if self.validated_hub and self.validated_hub != device.unique_id:
            self.enabled = self.validated = False
            await self._save()
            error_message = "The EKF hub changed; verify reception again"
            raise HomeAssistantError(error_message)

    def _pending_send(self) -> bool:
        if self.device is None:
            return True
        # Native Tuya Local queues DP201. Never replace an unsent command.
        pending = getattr(self.device, "_pending_updates", {}).get("201")
        if pending and not pending.get("sent", False):
            return True
        if pending:
            try:
                value = json.loads(pending.get("value", "{}"))
            except (ValueError, TypeError):
                value = {}
            if value.get("control") in {"send_ir", "study_key", "rfstudy_send"}:
                return (
                    time.time() - pending.get("updated_at", 0)
                    < _TRANSMISSION_GUARD_SECONDS
                )
        return False

    async def _mode(self, command: str) -> bool:
        """
        Use the native API lock without ever replacing queued DP201 writes.

        The caller holds the appliance and remote learning locks. Tuya Local's
        retry helper acquires its own API lock around the executor callback, so
        the last guard runs under the same lock as native sends and polling.
        """
        if command not in {"study", "study_exit"}:
            error_message = "Unsupported receiver mode"
            raise ValueError(error_message)
        device = self.device
        generation = self._generation
        self.armed = False

        def write() -> dict[str, Any]:
            now = time.monotonic()
            if self._pending_send() or now < self.manual_until:
                return {"mode_written": False}
            if command == "study" and (
                self._closing
                or now < self.ignore_until
                or generation != self._generation
            ):
                return {"mode_written": False}
            result = device._api.set_multiple_values(  # noqa: SLF001 - Tuya Local adapter shares the existing native lock/API.
                {"201": json.dumps({"control": command})}, nowait=True
            )
            if isinstance(result, dict) and "Error" in result:
                return result
            device._last_connection = time.time()  # noqa: SLF001 - Tuya Local adapter shares the existing native lock/API.
            return {"mode_written": True}

        operation = asyncio.create_task(
            device._retry_on_failed_connection(  # noqa: SLF001 - Tuya Local adapter shares the existing native lock/API.
                write, "Failed to update EKF IR receiver mode"
            )
        )
        cancelled = False
        try:
            result = await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Do not release the native API lock while its executor still writes.
            cancelled = True
            result = await operation
        written = isinstance(result, dict) and result.get("mode_written", False)
        self.armed = bool(
            written and command == "study" and generation == self._generation
        )
        if written and command == "study":
            self.last_arm = time.monotonic()
            self.last_study = time.time()
            self.study_sessions += 1
            self._socket_marker = getattr(device._api, "socket", None)  # noqa: SLF001 - Tuya Local adapter shares the existing native lock/API.
        if cancelled:
            raise asyncio.CancelledError
        if result is None:
            error_message = "EKF receiver mode write failed"
            raise HomeAssistantError(error_message)
        return written

    async def _renew_study(self) -> bool:
        """Each single-shot learner session needs an explicit exit/start edge."""
        generation = self._generation
        self._set_status("rearming")
        if not await self._mode("study_exit"):
            return False
        # Let the hub consume EXIT; native sends can be queued during this await.
        await asyncio.sleep(_STUDY_SETTLE_SECONDS)
        if generation != self._generation or self._closing:
            return False
        written = await self._mode("study")
        self._set_status("study_requested" if self.armed else "paused_transmit")
        return written

    def on_receive(self, poll: Mapping[str, Any], full_poll: bool = False) -> None:  # noqa: FBT001, FBT002 - Tuya Local callback accepts positional full_poll.
        # Native full refreshes can include the previous learned code/control.
        # Only a new delta can consume a study session or produce a button event.
        """Consume a fresh Tuya Local delta without replaying cached captures."""
        self.last_packet = time.time()
        self.last_packet_keys = sorted(str(key) for key in poll)
        code = poll.get("202")
        if full_poll:
            if code:
                self.cached_capture_count += 1
                self._notify()
            return
        self._observe_transmit_delta(poll)
        if self._closing or not code:
            return
        self.capture_count += 1
        self.last_capture = time.time()
        self.armed = False  # Even an unknown/invalid signal consumes the learner.
        self._generation += 1
        now = time.monotonic()
        if now < self.ignore_until or now < self.manual_until:
            self.suppressed_capture_count += 1
            self._notify()
            return
        frames = self._capture_frames(code)
        self._deliver_frames(frames, now)

    def _observe_transmit_delta(self, poll: Mapping[str, Any]) -> None:
        if "201" not in poll:
            return
        try:
            control = json.loads(poll["201"]).get("control")
        except (ValueError, TypeError, AttributeError):
            control = None
        if control in {"send_ir", "study_key", "rfstudy_send"}:
            self._generation += 1
            self.ignore_until = max(self.ignore_until, time.monotonic() + 1.2)
            self.armed = False
            self._set_status("paused_transmit")

    def _capture_frames(self, code: str) -> list[IrFrame]:
        frames = decode_frames(code)
        self.last_frame = [
            {
                key: frame[key]
                for key in ("protocol", "address", "command", "repeat")
                if key in frame
            }
            for frame in frames[:8]
        ]
        return frames

    def _deliver_frames(self, frames: list[IrFrame], now: float) -> None:
        if not frames:
            self._set_status("unrecognized_capture")
            self._notify()
            return
        if self.probe_waiter is not None and not self.probe_waiter.done():
            names = RemoteMapper().feed(frames, now)
            self.probe_waiter.set_result(
                {"received": True, "frames": frames, "events": names}
            )
            self._notify()
            return
        if not self.enabled:
            self.suppressed_capture_count += 1
            self._notify()
            return
        names = self.mapper.feed(frames, now)
        if not names:
            self._set_status("unrecognized_capture")
            self._notify()
            return
        received_wall = time.time()
        self.event_count += len(names)
        self._set_status("capture_received")
        if not self.validated:
            self.validated = True
            self.validated_hub = self.device.unique_id
            self.store.async_delay_save(self._storage_data, 1)
        for name, captured_at in self.mapper.event_times:
            self.last_received = received_wall - (now - captured_at)
            self._notify(name)

    async def async_before_send(self) -> None:
        """Pause reception while the shared transmission lock is held."""
        if not self.enabled or self.device is None:
            return
        self.ignore_until = max(self.ignore_until, time.monotonic() + 1.2)
        deadline = time.monotonic() + 6
        while self._pending_send() and time.monotonic() < deadline:  # noqa: ASYNC110 - Native writes expose no event; polling is deadline-bounded.
            await asyncio.sleep(0.1)
        if self._pending_send():
            error_message = "EKF still has a queued IR command"
            raise HomeAssistantError(error_message)
        async with self.remote._lock:  # noqa: SLF001 - Tuya Local adapter shares the existing native lock/API.
            await self._mode("study_exit")
        self.armed = False
        self._set_status("paused_transmit")

    async def _loop(self) -> None:
        try:
            while self.enabled and not self._closing:
                try:
                    await self._listen_once()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - Supervise native-provider errors; cancellation is re-raised.
                    self.armed = False
                    self._set_status("error", str(err))
                    _LOGGER.warning("EKF IR listener paused: %s", err)
                    await asyncio.sleep(5)
        finally:
            self.armed = False
            self._set_status("stopped" if self.enabled else "disabled", self.last_error)

    async def _listen_once(self) -> None:
        await self._attach()
        running, socket, working = self._native_connection()
        if not running or socket is None or not working:
            self.armed = False
            self._socket_marker = None
            self._set_status(
                "disconnected", "Tuya Local receiver connection is not ready"
            )
            await asyncio.sleep(0.2)
            return
        if socket is not self._socket_marker:
            self.armed = False
        async with self.runtime.transport.lock, self.remote._lock:  # noqa: SLF001 - Tuya Local adapter shares the existing native lock/API.
            await self._refresh_study_mode()
        await asyncio.sleep(0.2)

    async def _refresh_study_mode(self) -> None:
        now = time.monotonic()
        if self.probe_waiter is not None or now < self.manual_until:
            self.armed = False
            self._set_status("paused_learning")
        elif now < self.ignore_until or self._pending_send():
            self.armed = False
            self._set_status("paused_transmit")
        elif not self.armed or now - self.last_arm >= _STUDY_RENEW_SECONDS:
            await self._renew_study()
        else:
            self._set_status("study_requested")

    async def async_start(self, _hass: HomeAssistant | None = None) -> None:
        """Start the background listener only when reception is enabled."""
        if (
            not self._closing
            and self.enabled
            and (self.task is None or self.task.done())
        ):
            self._set_status("starting")
            self.task = self.hass.async_create_background_task(
                self._loop(), "ekf_ir_receiver"
            )
            self.task.add_done_callback(lambda _: self._notify())
        self._notify()

    async def async_set_enabled(self, *, enabled: bool) -> None:
        """Persist the receiver setting and reconcile its listener."""
        if enabled:
            await self._attach()
            self.enabled = True
            await self._save()
            await self.async_start()
        else:
            self.enabled = False
            if self.task:
                self.task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.task
                self.task = None
            if self.device and time.monotonic() >= self.manual_until:
                async with self.runtime.transport.lock, self.remote._lock:  # noqa: SLF001 - Tuya Local adapter shares the existing native lock/API.
                    if not self._pending_send():
                        await self._mode("study_exit")
            self.armed = False
            self._set_status("disabled")
            await self._save()
            self._notify()

    async def async_probe(self, seconds: float = 30) -> dict[str, Any]:
        """Validate reception in one bounded learning session."""
        if self._closing:
            error_message = "EKF receiver is unloading"
            raise HomeAssistantError(error_message)
        if self._probe_task is not None:
            error_message = "An EKF receive probe is already running"
            raise HomeAssistantError(error_message)
        self._probe_task = asyncio.current_task()
        try:
            await self._attach()
            async with self.runtime.transport.lock, self.remote._lock:  # noqa: SLF001 - Tuya Local adapter shares the existing native lock/API.
                deadline = time.monotonic() + 6
                while (  # noqa: ASYNC110 - Native writes expose no event; polling is deadline-bounded.
                    self._pending_send() or time.monotonic() < self.ignore_until
                ) and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                if self._pending_send() or time.monotonic() < self.manual_until:
                    error_message = "EKF is busy sending or learning an IR command"
                    raise HomeAssistantError(error_message)
                waiter = self.hass.loop.create_future()
                self.probe_waiter = waiter
                try:
                    if not await self._renew_study():
                        error_message = "EKF became busy before the receive probe"
                        raise HomeAssistantError(error_message)
                    try:
                        result = await asyncio.wait_for(waiter, seconds)
                    except TimeoutError:
                        result = {"received": False, "frames": [], "events": []}
                    if result["events"]:
                        self.validated = True
                        self.validated_hub = self.device.unique_id
                        await self._save()
                    return {
                        **result,
                        "validated": self.validated,
                        "hub": self.device.unique_id,
                    }
                finally:
                    self.probe_waiter = None
                    await self._mode("study_exit")
                    self.ignore_until = max(self.ignore_until, time.monotonic() + 0.8)
                    self._notify()
        finally:
            self._probe_task = None

    async def async_close(self) -> None:
        """Cancel work and unregister the shared device observer."""
        if self._closing:
            return
        self._closing = True
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if self._probe_task and self._probe_task is not asyncio.current_task():
            self._probe_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._probe_task
        if self.device is not None:
            if self.enabled:
                with suppress(Exception):
                    async with self.runtime.transport.lock, self.remote._lock:  # noqa: SLF001 - Tuya Local adapter shares the existing native lock/API.
                        await self._mode("study_exit")
            with suppress(ValueError):
                await self.device.async_unregister_entity(self)
        self.unsub_services()


def register_probe_service(hass: HomeAssistant) -> None:
    """Register the bounded receive-probe service once per HA instance."""
    if hass.services.has_service(DOMAIN, "probe_remote_receive"):
        return

    async def probe(call: ServiceCall) -> dict[str, Any]:
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.data.get("remote_entity_id") == call.data["remote_entity_id"]:
                runtime = getattr(entry, "runtime_data", None)
                if runtime and hasattr(runtime, "receiver"):
                    return await runtime.receiver.async_probe(call.data["seconds"])
        error_message = "EKF appliance integration is not loaded for this remote"
        raise HomeAssistantError(error_message)

    hass.services.async_register(
        DOMAIN,
        "probe_remote_receive",
        probe,
        schema=vol.Schema(
            {
                vol.Required("remote_entity_id"): str,
                vol.Optional("seconds", default=30): vol.All(
                    vol.Coerce(float), vol.Range(min=1, max=30)
                ),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
