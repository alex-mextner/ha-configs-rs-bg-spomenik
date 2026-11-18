"""
ODROID N2+ resistive soil probe -> localhost JSON for Home Assistant.

Wiring (physical header pin numbers):
  GPIO pin 22 -- 10 kOhm -- electrode A
  ADC  pin 37 ------------ electrode B -- 10 kOhm -- GND pin 39

GPIO remains LOW except while a reading is being taken. No HW-103 board is used.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import statistics
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager
    from socket import socket
    from typing import TextIO

_LOGGER = logging.getLogger("ha-soil-moisture")
_ADC_MAX_RAW = 4095
_ADC_MAX_VOLTS = 1.8


@dataclass(frozen=True)
class Config:
    """Describe the existing wiring and the probe's short sampling pulse."""

    gpio_chip: str = "/dev/gpiochip0"
    gpio_line: str = "PIN_22"  # Line 67, physical pin 22; preserve pin 11 PWM.
    physical_pin: int = 22
    adc_raw: Path = Path("/sys/bus/iio/devices/iio:device0/in_voltage3_raw")
    adc_scale: Path = Path("/sys/bus/iio/devices/iio:device0/in_voltage_scale")
    gpio_volts: float = 3.3
    top_ohms: int = 10_000
    bottom_ohms: int = 10_000
    settle_seconds: float = 0.12
    sample_gap_seconds: float = 0.01
    sample_count: int = 11


_DEFAULT_CONFIG = Config()


class PowerSwitch(Protocol):
    """Accept explicit probe power transitions."""

    def __call__(self, *, on: bool) -> None:
        """Switch the probe power on or off."""
        ...


class Sampler(Protocol):
    """Produce the bridge's JSON-compatible sensor reading."""

    def sample(self) -> dict[str, object]:
        """Return one reading or raise the underlying acquisition failure."""
        ...


def estimate_resistance(
    voltage: float, supply: float, top_ohms: float, bottom_ohms: float
) -> float | None:
    """
    Electrode resistance in ohms for the 10k/probe/10k voltage divider.

    Conductivity depends heavily on salts and substrate; this is not absolute
    volumetric water content. For open circuit we return None, not infinity.
    """
    if voltage <= 0:
        return None
    return max(0.0, supply * bottom_ohms / voltage - top_ohms - bottom_ohms)


@contextlib.contextmanager
def gpio_power(config: Config) -> Iterator[PowerSwitch]:
    """Request the line LOW, then guarantee LOW in a finally block."""
    # Only the deployed hardware path requires Debian's python3-libgpiod package.
    import gpiod  # noqa: PLC0415
    from gpiod.line import Direction, Value  # noqa: PLC0415

    with gpiod.request_lines(
        config.gpio_chip,
        consumer="ha-soil-moisture",
        config={
            config.gpio_line: gpiod.LineSettings(
                direction=Direction.OUTPUT, output_value=Value.INACTIVE
            )
        },
    ) as request:

        def switch(*, on: bool) -> None:
            request.set_value(config.gpio_line, Value.ACTIVE if on else Value.INACTIVE)

        try:
            yield switch
        finally:
            switch(on=False)


class SoilSampler:
    """Pulse the existing probe, then return median ADC and derived resistance."""

    def __init__(
        self,
        config: Config = _DEFAULT_CONFIG,
        *,
        power: Callable[[Config], AbstractContextManager[PowerSwitch]] = gpio_power,
        read_text: Callable[[Path], str] = lambda p: p.read_text(
            encoding="ascii"
        ).strip(),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Configure acquisition, allowing hardware-free readers in tests."""
        self.config = config
        self.power = power
        self.read_text = read_text
        self.sleep = sleep

    def sample(self) -> dict[str, object]:
        """Take one short powered sample and always release the probe LOW."""
        cfg = self.config
        if cfg.sample_count < 1:
            message = "sample_count must be positive"
            raise ValueError(message)

        with self.power(cfg) as switch:
            switch(on=True)
            self.sleep(cfg.settle_seconds)
            readings = []
            for i in range(cfg.sample_count):
                readings.append(int(self.read_text(cfg.adc_raw)))
                if i + 1 < cfg.sample_count:
                    self.sleep(cfg.sample_gap_seconds)
            switch(on=False)

        raw = int(statistics.median(readings))
        if not 0 <= raw <= _ADC_MAX_RAW:
            message = f"ADC value outside 12-bit range: {raw}"
            raise ValueError(message)
        # Meson SAR ADC's in_voltage_scale is millivolts / ADC count.
        scale_mv = float(self.read_text(cfg.adc_scale))
        voltage = raw * scale_mv / 1000
        if not 0 <= voltage <= _ADC_MAX_VOLTS:
            message = f"ADC voltage outside safe range: {voltage} V"
            raise ValueError(message)
        resistance = estimate_resistance(
            voltage, cfg.gpio_volts, cfg.top_ohms, cfg.bottom_ohms
        )
        return {
            "adc_raw": raw,
            "voltage_v": round(voltage, 5),
            "soil_resistance_ohm": round(resistance)
            if resistance is not None
            else None,
            "sampled_at": datetime.now(UTC).isoformat(),
            "physical_pin": cfg.physical_pin,
            "samples": readings,
        }


class ThrottledSampler:
    """
    Serialize GPIO access and bound pulse rate even under failing requests.

    For a short interval return the same successful reading (and its original
    timestamp), or raise a cached failure rather than powering the probe again.
    """

    def __init__(
        self,
        sampler: Sampler,
        *,
        minimum_interval: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Keep successful and failed attempts within one shared pulse budget."""
        self.sampler = sampler
        self.minimum_interval = minimum_interval
        self.clock = clock
        self.sleep = sleep
        self.lock = Lock()
        self.last_attempt = float("-inf")
        self.last_reading: dict[str, object] | None = None
        self.last_failure: str | None = None

    def sample(self, *, force: bool = False) -> dict[str, object]:
        """Reuse ordinary polls and wait outside the lock for a fresh reading."""
        requested_at = self.clock()
        while True:
            with self.lock:
                now = self.clock()
                # Multiple calibrations can share a measurement that was made
                # after both requests arrived, without waiting for two pulses.
                if force and self.last_attempt >= requested_at:
                    if self.last_failure is not None:
                        message = f"ADC read failed: {self.last_failure}"
                        raise RuntimeError(message)
                    if self.last_reading is not None:
                        return dict(self.last_reading)

                remaining = self.minimum_interval - (now - self.last_attempt)
                if remaining > 0:
                    if not force:
                        if self.last_failure is not None:
                            message = f"ADC retry deferred: {self.last_failure}"
                            raise RuntimeError(message)
                        if self.last_reading is not None:
                            return dict(self.last_reading)
                    # Fresh callers sleep OUTSIDE the lock, leaving normal REST
                    # polls and health checks responsive during the cooldown.
                else:
                    self.last_attempt = now
                    self.last_reading = None
                    self.last_failure = None
                    try:
                        reading = self.sampler.sample()
                    except Exception as exc:
                        self.last_failure = f"{type(exc).__name__}: {exc}"
                        raise
                    self.last_reading = dict(reading)
                    return dict(reading)

            self.sleep(remaining)


def build_handler(
    sampler: Sampler, *, minimum_interval: float = 15.0
) -> type[BaseHTTPRequestHandler]:
    """Build the unchanged JSON routes around one serialized sampling gate."""
    # Threaded HTTP handles idle clients; the gate serializes and rate-limits GPIO.
    gated_sampler = ThrottledSampler(sampler, minimum_interval=minimum_interval)

    class Handler(BaseHTTPRequestHandler):
        # Bound the lifetime of incomplete HTTP requests.
        timeout = 5

        @staticmethod
        def route(path: str) -> tuple[int, str]:
            if path == "/healthz":
                return 200, json.dumps({"status": "ok"})
            if path not in ("/reading", "/reading/fresh"):
                return 404, json.dumps({"error": "not found"})
            try:
                return 200, json.dumps(
                    gated_sampler.sample(force=path == "/reading/fresh"),
                    ensure_ascii=False,
                )
            except (OSError, RuntimeError, ValueError, ImportError) as exc:
                return 503, json.dumps({"error": str(exc)})

        def do_GET(self) -> None:
            status, body = self.route(self.path.split("?", maxsplit=1)[0])
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            # Never include client-controlled paths, headers, or sample contents.
            _LOGGER.info("HTTP response status=%s bytes=%s", code, size)

        def log_message(self, _format_string: str, *_args: object) -> None:
            _LOGGER.warning("HTTP protocol event")

    return Handler


class BoundedHTTPServer(ThreadingHTTPServer):
    """Limit outstanding localhost sockets, including stalled or idle clients."""

    max_clients = 8
    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Preserve HTTPServer construction with a bounded admission semaphore."""
        self.client_slots = BoundedSemaphore(self.max_clients)
        super().__init__(*args, **kwargs)

    def process_request(self, request: socket, client_address: tuple[str, int]) -> None:
        """Reject excess sockets without acquiring or powering the probe."""
        if not self.client_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                # A rejected client may already have closed the socket.
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.client_slots.release()
            raise

    def process_request_thread(
        self, request: socket, client_address: tuple[str, int]
    ) -> None:
        """Release the admission slot after every request outcome."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.client_slots.release()


def make_server(
    host: str, port: int, sampler: Sampler, *, minimum_interval: float = 15.0
) -> ThreadingHTTPServer:
    """Construct the bounded localhost server without starting acquisition."""
    return BoundedHTTPServer(
        (host, port), build_handler(sampler, minimum_interval=minimum_interval)
    )


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Keep the active log private after each rotation, regardless of umask."""

    def _open(self) -> TextIO:
        stream = super()._open()
        try:
            os.fchmod(stream.fileno(), 0o600)
        except OSError:
            stream.close()
            raise
        return stream


@contextlib.contextmanager
def bridge_logging() -> Iterator[Path]:
    """Keep terminal diagnostics and a private, timestamped, rotating temp log."""
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    terminal_handler = logging.StreamHandler()
    terminal_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [terminal_handler]
    old_level, old_propagate = _LOGGER.level, _LOGGER.propagate
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False
    _LOGGER.addHandler(terminal_handler)
    try:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        log_directory = Path(tempfile.mkdtemp(prefix=f"ha-soil-moisture-{timestamp}-"))
        log_path = log_directory / "soil-bridge.log"
        log_path.touch(mode=0o600, exist_ok=False)
        file_handler = _PrivateRotatingFileHandler(
            log_path, maxBytes=1_048_576, backupCount=2, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
        _LOGGER.addHandler(file_handler)
        _LOGGER.info("Starting soil bridge; log=%s", log_path)
        yield log_path
    finally:
        for handler in handlers:
            _LOGGER.removeHandler(handler)
            handler.close()
        _LOGGER.setLevel(old_level)
        _LOGGER.propagate = old_propagate


def main() -> None:
    """Initialize diagnostics before binding and close the server on every exit."""
    with bridge_logging() as log_path:
        started_at = time.monotonic()
        try:
            host = os.getenv("SOIL_HTTP_HOST", "127.0.0.1")
            port = int(os.getenv("SOIL_HTTP_PORT", "8768"))
            http = make_server(host, port, SoilSampler())
            _LOGGER.info("Soil bridge listening on http://%s:%s", host, port)
            try:
                http.serve_forever(poll_interval=0.5)
            finally:
                http.server_close()
        except BaseException as exc:
            # Preserve the original failure/exit status without copying request data.
            exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
            if isinstance(exc, SystemExit) and isinstance(exc.code, int):
                exit_code = exc.code
            _LOGGER.error(  # noqa: TRY400 - traceback details can contain private data.
                "Soil bridge failed (%s); log=%s elapsed=%.3fs exit=%s",
                type(exc).__name__,
                log_path,
                time.monotonic() - started_at,
                exit_code,
            )
            raise
        else:
            _LOGGER.info(
                "Soil bridge completed; log=%s elapsed=%.3fs exit=0",
                log_path,
                time.monotonic() - started_at,
            )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:  # noqa: BLE001 - the CLI boundary suppresses private traceback data.
        raise SystemExit(1) from None
