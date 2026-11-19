"""Odroid's kernel-decoded IR input; observing never changes kernel keymaps."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from .ir_decode import FullFrame, IrFrame

import asyncio
import fcntl
import os
import stat
import struct
import time
from contextlib import suppress
from pathlib import Path

from homeassistant.exceptions import HomeAssistantError

from .ir_decode import (
    HOLD,
    MAIN,
    REPEAT_MAX_GAP,
    UNIVERSAL_ADDRESS,
    RemoteMapper,
    decode_frames,
    pulses_from_code,
)

# Linux include/uapi/linux/lirc.h. The ioctl changes this open file only.
_LIRC_SET_REC_MODE = 0x40046912
_LIRC_MODE_SCANCODE = 8
_RECORD = struct.Struct("=QHHIQ")
_MAX_AGE_NS = 1_000_000_000
_REPEAT = 2


_RC_PROTO_NEC = 9
_MAX_NEC_SCANCODE = 0xFFFF
_RC_PROTO_NECX = 10
_MAX_NECX_SCANCODE = 0xFFFFFF
_SAMSUNG_KERNEL_ADDRESS = 0x0707
_MAX_ECHO_FRAMES = 64
_MODE_PRESS_GAP_NS = 150_000_000


def scancode_frame(protocol: int, scancode: int) -> FullFrame | None:
    """Convert Linux NEC/NECX packing to the existing ESPHome field convention."""
    if protocol == _RC_PROTO_NEC and 0 <= scancode <= _MAX_NEC_SCANCODE:  # RC_PROTO_NEC
        address = scancode >> 8
        address |= (address ^ 0xFF) << 8
    elif (
        protocol == _RC_PROTO_NECX and 0 <= scancode <= _MAX_NECX_SCANCODE
    ):  # RC_PROTO_NECX
        address = ((scancode >> 16) & 0xFF) | ((scancode >> 8) & 0xFF) << 8
    else:
        return None
    command = (scancode & 0xFF) | ((scancode & 0xFF) ^ 0xFF) << 8
    if address == _SAMSUNG_KERNEL_ADDRESS:
        # The kernel decodes Samsung's 4.5 ms leader as NECX. ESPHome's Samsung
        # protocol reports its fields in wire/MSB order, unlike the NEC profile.
        address = int(f"{address:016b}"[::-1], 2)
        command = int(f"{command:016b}"[::-1], 2)
    return {"repeat": False, "address": address, "command": command, "offset": 0}


def _require_character_device(fd: int) -> None:
    if not stat.S_ISCHR(os.fstat(fd).st_mode):
        error_message = "The Odroid IR input is not a character device"
        raise OSError(error_message)


def open_lirc_device() -> int:
    """Open only the already exposed meson-ir receiver, with no global changes."""
    device = Path("/dev/lirc0")
    sys_device = Path("/sys/class/lirc/lirc0/device")
    # Some kernels expose lirc below rc only.
    identity = sys_device / "uevent"
    if not identity.exists():
        identity = Path("/sys/class/rc/rc0/uevent")
    description = identity.read_text()
    if "DRV_NAME=meson-ir" not in description:
        error_message = "The lirc0 device is not the expected Odroid meson-ir receiver"
        raise OSError(error_message)
    fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        _require_character_device(fd)
        fcntl.ioctl(fd, _LIRC_SET_REC_MODE, struct.pack("=I", _LIRC_MODE_SCANCODE))
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_native_records(fd: int) -> bytes:
    data = os.read(fd, _RECORD.size * 32)
    if not data:
        error_message = "Native IR receiver disconnected"
        raise OSError(error_message)
    return data


class NativeIrReceiver:
    """Read bounded batches on FD readiness; active dispatch is independently gated."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the native receiver and its bounded gesture histories."""
        self.hass = hass
        self.callbacks = []
        self.mapper = RemoteMapper()
        self.enabled = False
        self.validated = False
        self.last_received = self.last_error = self.last_frame = None
        self.last_press_id = None
        self.status = "stopped"
        self.capture_count = self.event_count = self.suppressed_echo_count = 0
        self.kernel_key_conflicts = self.stale_capture_count = 0
        self.last_lag_ms = None
        self._fd = None
        self._reading = False
        self._connect_task = None
        self._retry_handle = None
        self._closing = False
        self._started = False
        self._accept_after_ns = 0
        self._last_identity = None
        self._echo_until = {}
        self.frame_observer = None
        self._mode_frames = {}
        self._press_mappers = {}

    @property
    def connected(self) -> bool:
        """Return whether the native input descriptor is available."""
        return self._fd is not None and not self._closing

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return bounded capture, echo and keymap diagnostics."""
        return {
            "receiver_status": self.status,
            "listener_running": self.connected,
            "capture_count": self.capture_count,
            "event_count": self.event_count,
            "suppressed_echo_count": self.suppressed_echo_count,
            "kernel_key_conflicts": self.kernel_key_conflicts,
            "stale_capture_count": self.stale_capture_count,
            "last_frame": self.last_frame,
            "last_lag_ms": self.last_lag_ms,
        }

    def add_callback(
        self, callback: Callable[[str | None], None]
    ) -> Callable[[], None]:
        """Subscribe to receiver updates and return an unsubscribe callback."""
        self.callbacks.append(callback)
        return lambda: (
            self.callbacks.remove(callback) if callback in self.callbacks else None
        )

    def _notify(self, name: str | None = None) -> None:
        for callback in tuple(self.callbacks):
            callback(name)

    def _reset(self) -> None:
        self.mapper = RemoteMapper()
        self._last_identity = None

    def set_dispatch_enabled(self, *, enabled: bool) -> None:
        """Switch semantic dispatch and discard prior receiver gesture state."""
        self.enabled = bool(enabled)
        self._accept_after_ns = time.monotonic_ns()
        self._reset()
        self._mode_frames.clear()
        self._press_mappers.clear()
        self._notify()

    def note_transmit(
        self, commands: object, *, repeats: int = 1, delay: float = 0.0
    ) -> None:
        """Suppress only the exact known outgoing frame, not unrelated user input."""
        commands = [commands] if isinstance(commands, str) else commands
        if not isinstance(commands, (list, tuple)):
            return
        now = time.monotonic_ns()
        self._echo_until = {
            key: end for key, end in self._echo_until.items() if end > now
        }
        for code in commands[:32]:
            if not isinstance(code, str):
                continue
            frames = decode_frames(code)
            duration = sum(pulses_from_code(code)) / 1_000_000
            # Tuya sends can be queued briefly. This guard is per fingerprint:
            # Chromecast navigation and other native keys remain uninterrupted.
            seconds = min(6.0, max(1.2, repeats * (duration + delay) + 0.8))
            until = now + int(seconds * 1_000_000_000)
            for frame in frames:
                if not frame["repeat"]:
                    key = (frame["address"], frame["command"])
                    self._echo_until[key] = max(self._echo_until.get(key, 0), until)
        # Bound diagnostic state even if a third-party caller supplies many codes.
        if len(self._echo_until) > _MAX_ECHO_FRAMES:
            self._echo_until = dict(
                sorted(self._echo_until.items(), key=lambda item: item[1])[-64:]
            )

    def process_records(self, data: bytes) -> None:
        """Process one kernel read. Never reconstruct timing from HA receive time."""
        if self._closing:
            return
        if len(data) % _RECORD.size:
            self._reset()
            self.last_error = "Invalid native IR record length"
            self._notify()
            return
        received_ns = time.monotonic_ns()
        received_wall = time.time()
        for record in _RECORD.iter_unpack(data):
            self._process_record(record, received_ns, received_wall)
        self._notify()

    def _fresh_capture(self, timestamp_ns: int, received_ns: int) -> bool:
        if (
            timestamp_ns < self._accept_after_ns
            or received_ns - timestamp_ns > _MAX_AGE_NS
            or timestamp_ns > received_ns + 1_000_000
        ):
            self.stale_capture_count += 1
            self._reset()
            return False
        return True

    def _continued_mode(self, key: tuple[int, int], timestamp_ns: int) -> bool:
        if key not in {(0xBF00, 0xED12), (0xBF00, 0x9966)}:
            return False
        previous = self._mode_frames.get(key)
        continued = (
            previous is not None and timestamp_ns - previous < _MODE_PRESS_GAP_NS
        )
        self._mode_frames[key] = timestamp_ns
        return continued

    def _accept_universal_frame(
        self, frame: FullFrame, timestamp_ns: int, keycode: int
    ) -> bool:
        if not RemoteMapper().feed([frame], timestamp_ns / 1e9):
            self._reset()
            return False
        if keycode:
            # A matching Linux keymap could already deliver this to Kodi.
            # Reject duplicate semantic input instead of grabbing a keyboard.
            self.kernel_key_conflicts += 1
            self.last_error = "Universal remote scancode is also mapped to a Linux key"
            self.status = "keymap_conflict"
            self._reset()
            return False
        return True

    def _semantic_frame(
        self,
        frame: FullFrame,
        identity: tuple[int, int],
        flags: int,
        timestamp_ns: int,
        *,
        continued_mode: bool,
    ) -> IrFrame | None:
        if continued_mode and not flags & _REPEAT:
            # Unknown frames/our own echoes may reset semantic hold memory.
            # They cannot invent a release of an INPUT/AUDIO still held.
            self._last_identity = identity
            self.mapper.feed([{"repeat": True, "offset": 0}], timestamp_ns / 1e9)
            return None
        if flags & _REPEAT:
            if identity != self._last_identity:
                self._reset()
                return None
            return {"repeat": True, "offset": 0}
        self._last_identity = identity
        return frame

    def _publish_names(self, names: list[str], captured_wall: float) -> None:
        if not names:
            return
        self.validated = True
        self.last_error = None
        self.status = "capture_received"
        self.last_received = captured_wall
        if self.enabled:
            self.event_count += len(names)
            for name in names:
                self._notify(name)

    def _press_mapper(self, frame: FullFrame) -> RemoteMapper:
        if frame["address"] != UNIVERSAL_ADDRESS or MAIN.get(
            frame["command"]
        ) not in HOLD | {"power"}:
            return self.mapper
        # Keep only the four physical power/hold gestures across unrelated IR
        # and transmit echoes. Navigation and T9 still reset immediately.
        self.mapper = RemoteMapper()
        command = frame["command"]
        if command not in self._press_mappers:
            self._press_mappers[command] = RemoteMapper()
        return self._press_mappers[command]

    def _map_names(
        self,
        frame: FullFrame,
        identity: tuple[int, int],
        flags: int,
        timestamp_ns: int,
        *,
        continued_mode: bool,
    ) -> list[str]:
        mapper = self._press_mapper(frame)
        protected = mapper is not self.mapper
        semantic = self._semantic_frame(
            frame, identity, flags, timestamp_ns, continued_mode=continued_mode
        )
        if semantic is not None:
            names = mapper.feed([semantic], timestamp_ns / 1e9)
            if names:
                self.last_press_id = mapper.event_press_ids[-1]
            return names
        # An orphan repeat cannot emit an event, but still proves that a known
        # physical burst has not ended. Do not erase a held key's silence gap.
        if (
            protected
            and flags & _REPEAT
            and 0 <= timestamp_ns / 1e9 - mapper.last_at <= REPEAT_MAX_GAP
        ):
            mapper.last_at = timestamp_ns / 1e9
        return []

    def _process_record(
        self,
        record: tuple[int, int, int, int, int],
        received_ns: int,
        received_wall: float,
    ) -> None:
        timestamp_ns, flags, protocol, keycode, scancode = record
        self.capture_count += 1
        self.last_lag_ms = round((received_ns - timestamp_ns) / 1_000_000, 3)
        self.last_frame = {
            "protocol": protocol,
            "scancode": hex(scancode),
            "repeat": bool(flags & _REPEAT),
            "keycode": keycode,
        }
        if not self._fresh_capture(timestamp_ns, received_ns):
            return
        frame = scancode_frame(protocol, scancode)
        if frame is None:
            self._reset()
            return
        key = (frame["address"], frame["command"])
        continued_mode = self._continued_mode(key, timestamp_ns)
        if timestamp_ns <= self._echo_until.get(key, -1):
            self.suppressed_echo_count += 1
            self._reset()
            return
        captured_wall = received_wall - (received_ns - timestamp_ns) / 1e9
        # OEM projector POWER is deliberately outside the universal map.
        # Observe real, fresh, non-echo captures before the semantic filter.
        if self.frame_observer is not None and not flags & _REPEAT:
            self.frame_observer(frame, captured_wall)
        if not self._accept_universal_frame(frame, timestamp_ns, keycode):
            return
        names = self._map_names(
            frame,
            (protocol, scancode),
            flags,
            timestamp_ns,
            continued_mode=continued_mode,
        )
        self._publish_names(names, captured_wall)

    async def async_start(self, _hass: HomeAssistant | None = None) -> None:
        """Start observing native IR after Home Assistant starts."""
        if self._closing:
            return
        self._started = True
        if self._fd is None:
            if self._connect_task is None or self._connect_task.done():
                self._connect_task = self.hass.async_create_background_task(
                    self._connect(), "odroid_native_ir_connect"
                )
            await asyncio.shield(self._connect_task)

    async def _connect(self) -> None:
        try:
            fd = await self.hass.async_add_executor_job(open_lirc_device)
        except OSError as err:
            self.status, self.last_error = "disconnected", str(err)
            self._notify()
            self._schedule_retry()
            return
        if self._closing:
            os.close(fd)
            return
        self._fd = fd
        self._accept_after_ns = time.monotonic_ns()
        self._reset()
        self._mode_frames.clear()
        self._press_mappers.clear()
        self.last_error = None
        self.status = "listening"
        try:
            self.hass.loop.add_reader(fd, self._readable)
        except Exception:
            os.close(fd)
            self._fd = None
            raise
        self._notify()

    def _schedule_retry(self) -> None:
        if self._closing or not self._started or self._retry_handle is not None:
            return

        def retry() -> None:
            self._retry_handle = None
            if not self._closing:
                self._connect_task = self.hass.async_create_background_task(
                    self._connect(), "odroid_native_ir_reconnect"
                )

        self._retry_handle = self.hass.loop.call_later(2.0, retry)

    def _readable(self) -> None:
        if self._reading:
            return
        self._reading = True
        try:
            # One kernel FIFO per callback, then yield to HA's event loop.
            data = _read_native_records(self._fd)
            self.process_records(data)
        except BlockingIOError:
            return
        except OSError as err:
            self._disconnect()
            self.status, self.last_error = "disconnected", str(err)
            self._notify()
            self._schedule_retry()
        finally:
            self._reading = False

    def drain_pending(self) -> None:
        """Consume the ready FIFO before another callback can expire its gestures."""
        if self.connected:
            self._readable()

    def _disconnect(self) -> None:
        if self._fd is not None:
            self.hass.loop.remove_reader(self._fd)
            with suppress(OSError):
                os.close(self._fd)
            self._fd = None
        self._reset()

    async def async_probe(self, seconds: float = 30) -> dict[str, bool]:
        """Observe capture activity for a bounded diagnostic interval."""
        if not self.connected:
            error_message = "Odroid IR receiver is not connected"
            raise HomeAssistantError(error_message)
        before = self.capture_count
        waiter = self.hass.loop.create_future()

        def captured(_name: str | None) -> None:
            if self.capture_count > before and not waiter.done():
                waiter.set_result(True)

        unsubscribe = self.add_callback(captured)
        try:
            with suppress(TimeoutError):
                await asyncio.wait_for(waiter, seconds)
            return {
                "received": self.capture_count > before,
                "validated": self.validated,
                "receiver": "Odroid",
                "last_frame": self.last_frame,
            }
        finally:
            unsubscribe()

    async def async_close(self) -> None:
        """Stop observation and close native input without appliance commands."""
        if self._closing:
            return
        self._closing = True
        self.enabled = False
        if self._retry_handle:
            self._retry_handle.cancel()
            self._retry_handle = None
        self._disconnect()
        if self._connect_task and not self._connect_task.done():
            # An O_NONBLOCK open may be finishing in the executor. _connect
            # closes its result when _closing is set; never leak a late fd.
            await asyncio.shield(self._connect_task)
        self.status = "stopped"
        self._notify()
