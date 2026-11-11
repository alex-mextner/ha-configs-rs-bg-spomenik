"""Opt-in IR reception over the existing Tuya Local connection."""
from __future__ import annotations
import asyncio
from contextlib import suppress
import json
import logging
import time
from types import SimpleNamespace

import voluptuous as vol
from homeassistant.components.remote import DATA_COMPONENT as REMOTE_COMPONENT
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .ir_decode import RemoteMapper, decode_frames, pulses_from_code

_LOGGER = logging.getLogger(__name__)
DOMAIN = "ekf_appliances"


class IrReceiver:
    """The hub has one IR channel: pause listening around outgoing commands."""

    def __init__(self, runtime):
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
        self._config = SimpleNamespace(
            dps=lambda: [],
            unique_id=lambda device_id: f"ekf_ir_observer_{runtime.entry.entry_id}_{device_id}",
        )
        self.entity_id = "event.ekf_universal_remote"
        self.unsub_services = self.hass.bus.async_listen("call_service", self._service_called)

    async def async_load(self):
        data = await self.store.async_load() or {}
        self.validated = data.get("validated") is True
        self.validated_hub = data.get("hub")
        self.enabled = self.validated and data.get("enabled") is True

    async def _save(self):
        await self.store.async_save({"validated": self.validated, "hub": self.validated_hub,
                                     "enabled": self.enabled})

    def add_callback(self, callback):
        self.callbacks.append(callback)
        return lambda: self.callbacks.remove(callback) if callback in self.callbacks else None

    def _notify(self, event=None):
        for callback in tuple(self.callbacks):
            callback(event)

    def async_schedule_update_ha_state(self, *args):
        self._notify()

    def schedule_update_ha_state(self, *args):
        self._notify()

    def _service_called(self, event):
        data = event.data
        domain, service = data.get("domain"), data.get("service")
        if domain not in {"remote", "infrared"} or service not in {"send_command", "learn_command"}:
            return
        values = data.get("service_data") or {}
        targets = values.get("entity_id", [])
        targets = [targets] if isinstance(targets, str) else targets
        if targets and self.remote_id not in targets and self.remote_id.replace("remote.", "infrared.", 1) not in targets:
            return
        self._generation += 1
        now = time.monotonic()
        if service == "learn_command":
            commands = values.get("command", [])
            count = len(commands) if isinstance(commands, list) else 1
            self.manual_until = max(self.manual_until, now + 35 * max(1, count))
            self.ignore_until = max(self.ignore_until, self.manual_until)
        else:
            commands = values.get("command", [])
            commands = [commands] if isinstance(commands, str) else commands
            duration = sum(sum(pulses_from_code(c)) / 1_000_000 for c in commands if isinstance(c, str))
            repeats = max(1, int(values.get("num_repeats", 1)))
            delay = max(0.0, float(values.get("delay_secs", 0)))
            self.ignore_until = max(self.ignore_until, now + max(1.2, repeats * (duration + len(commands) * delay) + 0.8))
        self.armed = False

    async def _attach(self):
        component = self.hass.data.get(REMOTE_COMPONENT)
        remote = component.get_entity(self.remote_id) if component else None
        if remote is None:
            raise HomeAssistantError("EKF remote is not loaded")
        if remote is self.remote:
            return
        device = getattr(remote, "_device", None)
        send = getattr(remote, "_send_dp", None)
        receive = getattr(remote, "_receive_dp", None)
        if (device is None or str(getattr(send, "id", "")) != "201"
                or str(getattr(receive, "id", "")) != "202"
                or not hasattr(device, "register_entity")
                or not hasattr(remote, "_lock")
                or not hasattr(device, "_api_lock")
                or not hasattr(device, "_retry_on_failed_connection")
                or not hasattr(getattr(device, "_api", None), "set_multiple_values")):
            raise HomeAssistantError("This Tuya Local remote does not expose DP201/DP202")
        if self.device is not None:
            with suppress(ValueError):
                await self.device.async_unregister_entity(self)
        self.remote, self.device = remote, device
        device.register_entity(self)
        if self.validated_hub and self.validated_hub != device.unique_id:
            self.enabled = self.validated = False
            await self._save()
            raise HomeAssistantError("The EKF hub changed; verify reception again")

    def _pending_send(self):
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
                return time.time() - pending.get("updated_at", 0) < 1.2
        return False

    async def _mode(self, command):
        """Use the native API lock without ever replacing queued DP201 writes.

        The caller holds the appliance and remote learning locks. Tuya Local's
        retry helper acquires its own API lock around the executor callback, so
        the last guard runs under the same lock as native sends and polling.
        """
        if command not in {"study", "study_exit"}:
            raise ValueError("Unsupported receiver mode")
        device = self.device
        generation = self._generation

        def write():
            now = time.monotonic()
            if self._pending_send() or now < self.manual_until:
                return {"mode_written": False}
            if command == "study" and (
                self._closing or now < self.ignore_until
                or generation != self._generation
            ):
                return {"mode_written": False}
            result = device._api.set_multiple_values(
                {"201": json.dumps({"control": command})}, nowait=True
            )
            if isinstance(result, dict) and "Error" in result:
                return result
            device._last_connection = time.time()
            return {"mode_written": True}

        operation = asyncio.create_task(device._retry_on_failed_connection(
            write, "Failed to update EKF IR receiver mode"
        ))
        cancelled = False
        try:
            result = await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Do not release the native API lock while its executor still writes.
            cancelled = True
            result = await operation
        written = isinstance(result, dict) and result.get("mode_written", False)
        self.armed = bool(written and command == "study"
                          and generation == self._generation)
        if self.armed:
            self.last_arm = time.monotonic()
        if cancelled:
            raise asyncio.CancelledError
        if result is None:
            raise HomeAssistantError("EKF receiver mode write failed")
        return written

    def on_receive(self, poll, full_poll=False):
        if "201" in poll:
            try:
                control = json.loads(poll["201"]).get("control")
            except (ValueError, TypeError, AttributeError):
                control = None
            if control in {"send_ir", "study_key", "rfstudy_send"}:
                self._generation += 1
                self.ignore_until = max(self.ignore_until, time.monotonic() + 1.2)
                self.armed = False
        # A full status refresh may contain an old learned code.
        code = poll.get("202")
        if self._closing or full_poll or not code:
            return
        frames = decode_frames(code)
        if not frames:
            return
        now = time.monotonic()
        if now < self.ignore_until or now < self.manual_until:
            return
        if self.probe_waiter is not None and not self.probe_waiter.done():
            names = RemoteMapper().feed(frames, now)
            self.probe_waiter.set_result({"received": True, "frames": frames, "events": names})
            return
        if not self.enabled or now < self.ignore_until or now < self.manual_until:
            return
        self.last_received = time.time()
        self.armed = False
        for name in self.mapper.feed(frames, now):
            self._notify(name)

    async def async_before_send(self):
        """Called while the shared appliance transmission lock is held."""
        if not self.enabled or self.device is None:
            return
        self.ignore_until = max(self.ignore_until, time.monotonic() + 1.2)
        deadline = time.monotonic() + 6
        while self._pending_send() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if self._pending_send():
            raise HomeAssistantError("EKF still has a queued IR command")
        async with self.remote._lock:
            if self.armed:
                await self._mode("study_exit")
        self.armed = False

    async def _loop(self):
        while self.enabled and not self._closing:
            try:
                await self._attach()
                async with self.runtime.transport._lock, self.remote._lock:
                    now = time.monotonic()
                    if (self.probe_waiter is None and now >= self.ignore_until
                            and now >= self.manual_until and not self._pending_send()
                            and (not self.armed or now - self.last_arm >= 15)):
                        await self._mode("study")
                self.last_error = None
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.last_error = str(err)
                self._notify()
                _LOGGER.warning("EKF IR listener paused: %s", err)
                await asyncio.sleep(5)

    async def async_start(self, _hass=None):
        if not self._closing and self.enabled and self.validated and (self.task is None or self.task.done()):
            self.task = self.hass.async_create_background_task(self._loop(), "ekf_ir_receiver")
        self._notify()

    async def async_set_enabled(self, enabled):
        if enabled:
            await self._attach()
            if not self.validated or self.validated_hub != self.device.unique_id:
                raise HomeAssistantError("First verify a physical universal remote button with probe_remote_receive")
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
                async with self.runtime.transport._lock, self.remote._lock:
                    if not self._pending_send():
                        await self._mode("study_exit")
            await self._save()
            self._notify()

    async def async_probe(self, seconds=30):
        if self._closing:
            raise HomeAssistantError("EKF receiver is unloading")
        if self._probe_task is not None:
            raise HomeAssistantError("An EKF receive probe is already running")
        self._probe_task = asyncio.current_task()
        try:
            await self._attach()
            async with self.runtime.transport._lock, self.remote._lock:
                deadline = time.monotonic() + 6
                while (self._pending_send() or time.monotonic() < self.ignore_until) and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                if self._pending_send() or time.monotonic() < self.manual_until:
                    raise HomeAssistantError("EKF is busy sending or learning an IR command")
                waiter = self.hass.loop.create_future()
                self.probe_waiter = waiter
                try:
                    if not await self._mode("study"):
                        raise HomeAssistantError("EKF became busy before the receive probe")
                    try:
                        result = await asyncio.wait_for(waiter, seconds)
                    except TimeoutError:
                        result = {"received": False, "frames": [], "events": []}
                    if result["events"]:
                        self.validated = True
                        self.validated_hub = self.device.unique_id
                        await self._save()
                    return {**result, "validated": self.validated,
                            "hub": self.device.unique_id}
                finally:
                    self.probe_waiter = None
                    await self._mode("study_exit")
                    self.ignore_until = max(self.ignore_until, time.monotonic() + 0.8)
                    self._notify()
        finally:
            self._probe_task = None

    async def async_close(self):
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
            if self.armed:
                with suppress(Exception):
                    async with self.runtime.transport._lock, self.remote._lock:
                        await self._mode("study_exit")
            with suppress(ValueError):
                await self.device.async_unregister_entity(self)
        self.unsub_services()


def register_probe_service(hass):
    if hass.services.has_service(DOMAIN, "probe_remote_receive"):
        return

    async def probe(call):
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.data.get("remote_entity_id") == call.data["remote_entity_id"]:
                runtime = getattr(entry, "runtime_data", None)
                if runtime and hasattr(runtime, "receiver"):
                    return await runtime.receiver.async_probe(call.data["seconds"])
        raise HomeAssistantError("EKF appliance integration is not loaded for this remote")

    hass.services.async_register(
        DOMAIN, "probe_remote_receive", probe,
        schema=vol.Schema({vol.Required("remote_entity_id"): str,
                           vol.Optional("seconds", default=30): vol.All(vol.Coerce(float), vol.Range(min=1, max=30))}),
        supports_response=SupportsResponse.ONLY,
    )
