"""One semantic input stream, with an optional native Odroid diagnostic observer."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import math

from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import callback
from homeassistant.helpers.storage import Store

from .ir_decode import RemoteMapper, decode_frames
from .ir_receiver import IrReceiver
from .native_ir import NativeIrReceiver

SOURCES = ("EKF", "Odroid")


class ReceiverRouter:
    """Preserve existing event IDs while explicitly selecting one active receiver."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.hass = runtime.hass
        self.remote_id = runtime.entry.data["remote_entity_id"]
        self.ekf = IrReceiver(runtime)
        self.native = NativeIrReceiver(self.hass)
        self.native.frame_observer = runtime.observe_projector_ir
        self.store = Store(self.hass, 1, f"universal_ir_source_{runtime.entry.entry_id}")
        self.source = "EKF"
        self.enabled = False
        self.callbacks = []
        self._switching = self._closing = False
        self._lock = asyncio.Lock()
        self.ekf.add_callback(lambda name: self._received("EKF", name))
        self.native.add_callback(lambda name: self._received("Odroid", name))
        self._unsub_services = self.hass.bus.async_listen("call_service", self._service_called)

    @property
    def active(self):
        return self.native if self.source == "Odroid" else self.ekf

    @property
    def validated(self):
        return self.active.validated

    @property
    def last_error(self):
        return self.active.last_error

    @property
    def last_received(self):
        return self.active.last_received

    @property
    def diagnostics(self):
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

    def add_callback(self, callback):
        self.callbacks.append(callback)
        return lambda: self.callbacks.remove(callback) if callback in self.callbacks else None

    def _received(self, source, name):
        dispatch = (name is not None and source == self.source and self.enabled
                    and not self._switching and not self._closing)
        controller = getattr(self.runtime, "remote_controller", None)
        if dispatch and controller is not None:
            controller.physical_press(name, self.last_received)
        for callback in tuple(self.callbacks):
            callback(name if dispatch else None)

    def drain_pending(self):
        if self.source == "Odroid" and self.enabled and not self._switching and not self._closing:
            self.native.drain_pending()

    def _notify(self):
        self._received(self.source, None)

    async def async_load(self):
        await self.ekf.async_load()
        data = await self.store.async_load()
        if isinstance(data, dict) and data.get("source") in SOURCES:
            self.source = data["source"]
            self.enabled = data.get("enabled") is True
        else:
            # Migrate the user's existing opt-in without changing receiver.
            self.enabled = self.ekf.enabled
        self.ekf.enabled = self.enabled and self.source == "EKF"
        self.native.set_dispatch_enabled(self.enabled and self.source == "Odroid")

    async def _save(self):
        await self.store.async_save({"source": self.source, "enabled": self.enabled})

    async def async_start(self, _hass=None):
        if self._closing:
            return
        # Native is a passive diagnostic observer until explicitly selected.
        await self.native.async_start()
        if self.source == "EKF":
            await self.ekf.async_start()
        self._notify()

    async def async_set_source(self, source):
        if source not in SOURCES:
            raise HomeAssistantError("Unknown universal remote receiver")
        async with self._lock:
            if self._closing:
                raise HomeAssistantError("Universal remote receiver is unloading")
            if source == self.source:
                return
            if source == "Odroid" and not self.native.connected:
                raise HomeAssistantError(
                    "Odroid IR receiver is not connected"
                )
            previous = self.source
            self._switching = True
            self.native.set_dispatch_enabled(False)
            self.ekf.mapper = RemoteMapper()
            try:
                await self._activate_source(source)
            except BaseException:
                await self._restore_source(previous)
                raise
            finally:
                self._switching = False
                self._notify()

    async def _activate_source(self, source):
        if self.ekf.enabled:
            await self.ekf.async_set_enabled(False)
        if self._closing:
            raise HomeAssistantError("Universal remote receiver is unloading")
        if source == "EKF":
            await self.ekf.async_set_enabled(self.enabled)
        else:
            self.native.set_dispatch_enabled(self.enabled)
        if self._closing:
            raise HomeAssistantError("Universal remote receiver is unloading")
        self.source = source
        await self._save()

    async def _restore_source(self, previous):
        # Disable the newly started backend before restoring the old one.
        # A source-store failure must not leave a Tuya learner running
        # behind a native dispatcher (which intentionally skips its lock).
        self.native.set_dispatch_enabled(False)
        if self.ekf.enabled:
            if self._closing:
                self.ekf.enabled = False
            else:
                with suppress(Exception):
                    await self.ekf.async_set_enabled(False)
        self.source = previous
        if self._closing:
            return
        if previous == "EKF" and self.enabled:
            await self.ekf.async_set_enabled(True)
            if self._closing:
                self.ekf.enabled = False
        elif previous == "Odroid":
            self.native.set_dispatch_enabled(self.enabled)

    async def async_set_enabled(self, enabled):
        async with self._lock:
            if self._closing:
                raise HomeAssistantError("Universal remote receiver is unloading")
            enabled = bool(enabled)
            if self.source == "EKF":
                await self.ekf.async_set_enabled(enabled)
                self.ekf.mapper = RemoteMapper()
            else:
                self.native.set_dispatch_enabled(enabled)
            self.enabled = enabled
            await self._save()
            self._notify()

    async def async_before_send(self):
        # Native input never uses the Tuya learner or the shared IR TX lock.
        if self.source == "EKF":
            await self.ekf.async_before_send()

    @callback
    def _service_called(self, event):
        data = event.data
        if data.get("domain") == "script":
            self._observe_off_script(data)
        values = self._outgoing_ir(data)
        if values is None:
            return
        timing = self._transmit_timing(values)
        if timing is None:
            return
        repeats, delay = timing
        self.native.note_transmit(values.get("command", []), repeats=repeats, delay=delay)
        self._observe_projector_commands(values.get("command", []))

    def _observe_off_script(self, data):
        off_script = self.runtime.projector_off_script
        values = data.get("service_data") or {}
        targets = values.get("entity_id", []) if isinstance(values, dict) else []
        targets = [targets] if isinstance(targets, str) else targets
        if (data.get("service") == off_script.removeprefix("script.")
                or (data.get("service") == "turn_on" and off_script in targets)):
            self.runtime.note_projector_off_requested()

    def _outgoing_ir(self, data):
        if data.get("domain") not in {"remote", "infrared"} or data.get("service") != "send_command":
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
    def _transmit_timing(values):
        try:
            repeats = max(1, min(20, int(values.get("num_repeats", 1))))
            delay = float(values.get("delay_secs", 0))
            if not math.isfinite(delay):
                return None
            delay = max(0.0, min(30.0, delay))
        except (ValueError, TypeError, OverflowError):
            return None
        return repeats, delay

    def _observe_projector_commands(self, commands):
        commands = [commands] if isinstance(commands, str) else commands
        if isinstance(commands, (list, tuple)):
            for code in commands[:32]:
                for frame in decode_frames(code):
                    self.runtime.observe_projector_ir(frame, 0)

    async def async_probe(self, seconds=30):
        return await self.active.async_probe(seconds)

    async def async_close(self):
        if self._closing:
            return
        self._closing = True
        self.enabled = False
        self._unsub_services()
        await self.native.async_close()
        await self.ekf.async_close()
        self.ekf.enabled = False
