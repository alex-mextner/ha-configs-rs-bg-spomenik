"""Exercise soil sampling, bounded HTTP access, and HA calibration contracts."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import socket
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import yaml

from scripts.soil_moisture import soil_bridge
from scripts.soil_moisture.soil_bridge import (
    Config,
    SoilSampler,
    ThrottledSampler,
    build_handler,
    estimate_resistance,
    make_server,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator


class FakePower:
    """Record the probe power transitions without requesting a GPIO line."""

    def __init__(self) -> None:
        self.transitions = []

    @contextlib.contextmanager
    def connect(self, _config: Config) -> Iterator[Callable[..., None]]:
        self.transitions.append(False)  # Initially inactive.

        def set_power(*, on: bool) -> None:
            self.transitions.append(on)

        try:
            yield set_power
        finally:
            self.transitions.append(False)


class SoilBridgeTests(unittest.TestCase):
    """Keep GPIO safety, sampling freshness, and HTTP limits observable."""

    def setUp(self) -> None:
        self.config = Config(
            settle_seconds=0.1, sample_count=5, sample_gap_seconds=0.01
        )
        self.power = FakePower()
        self.sleeps = []

    def make_sampler(self, readings: Iterable[int | str]) -> SoilSampler:
        readings = iter(readings)

        def read(path: Path) -> str:
            if str(path).endswith("_scale"):
                return "0.439453125"
            return str(next(readings))

        return SoilSampler(
            self.config,
            power=self.power.connect,
            read_text=read,
            sleep=self.sleeps.append,
        )

    def test_both_resistors_limit_adc_to_1_65_volts(self) -> None:
        voltage = 3.3 * 10_000 / (10_000 + 0 + 10_000)
        self.assertAlmostEqual(voltage, 1.65)
        self.assertAlmostEqual(estimate_resistance(voltage, 3.3, 10_000, 10_000), 0)

    def test_resistance_for_open_dry_probe_is_unknown(self) -> None:
        self.assertIsNone(estimate_resistance(0, 3.3, 10_000, 10_000))

    def test_resistance_for_16_7_kohm_soil(self) -> None:
        self.assertAlmostEqual(
            estimate_resistance(0.9, 3.3, 10_000, 10_000), 16_666.67, places=1
        )

    def test_sample_reads_median_and_switches_off(self) -> None:
        result = self.make_sampler([1000, 1010, 3000, 990, 1005]).sample()
        self.assertEqual(result["adc_raw"], 1005)
        self.assertEqual(result["physical_pin"], 22)
        self.assertEqual(self.power.transitions, [False, True, False, False])
        self.assertEqual(self.sleeps, [0.1, 0.01, 0.01, 0.01, 0.01])
        self.assertAlmostEqual(result["voltage_v"], 1005 * 0.439453125 / 1000, places=4)
        self.assertTrue(result["soil_resistance_ohm"] > 0)
        self.assertIn("sampled_at", result)

    def test_sample_switches_off_even_when_adc_fails(self) -> None:
        sampler = self.make_sampler(["invalid"])
        with self.assertRaises(ValueError):
            sampler.sample()
        self.assertEqual(self.power.transitions[-1], False)

    def test_health_endpoint_does_not_power_sensor(self) -> None:
        sampler = self.make_sampler([])
        handler = build_handler(sampler)
        status, body = handler.route("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})
        self.assertEqual(self.power.transitions, [])

    def test_sample_endpoint_responds_with_json(self) -> None:
        sampler = self.make_sampler([50, 51, 49, 50, 50])
        status, body = build_handler(sampler).route("/reading")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["adc_raw"], 50)

    def test_other_path_is_not_a_sensor_reading(self) -> None:
        status, _body = build_handler(self.make_sampler([])).route("/other")
        self.assertEqual(status, 404)
        self.assertEqual(self.power.transitions, [])

    def test_idle_http_client_cannot_block_bridge_forever(self) -> None:
        try:
            server = make_server("127.0.0.1", 0, self.make_sampler([]))
        except PermissionError:
            self.skipTest("Local sandbox denies even loopback socket creation")
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        idle = None
        try:
            server_thread.start()
            idle = socket.create_connection(server.server_address, timeout=1)
            # An idle socket monopolized the former single-threaded HTTPServer.
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/healthz", timeout=1
            ) as response:
                self.assertEqual(json.load(response), {"status": "ok"})
        finally:
            if idle is not None:
                idle.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_concurrent_requests_never_power_gpio_together(self) -> None:
        class CountingSampler:
            active = 0
            maximum = 0
            mutex = threading.Lock()

            def sample(self) -> dict[str, int]:
                with self.mutex:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                time.sleep(0.03)
                with self.mutex:
                    self.active -= 1
                return {"adc_raw": 100}

        sampler = CountingSampler()
        route = build_handler(sampler).route
        workers = [threading.Thread(target=route, args=("/reading",)) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sampler.maximum, 1)

    def test_pulse_rate_limit_returns_recent_reading_until_next_interval(self) -> None:
        class CountingSampler:
            calls = 0

            def sample(self) -> dict[str, int]:
                self.calls += 1
                return {"adc_raw": self.calls}

        fake_time = [100.0]
        probe = CountingSampler()
        gate = ThrottledSampler(probe, minimum_interval=15, clock=lambda: fake_time[0])
        self.assertEqual(gate.sample()["adc_raw"], 1)
        fake_time[0] = 114.9
        self.assertEqual(gate.sample()["adc_raw"], 1)
        self.assertEqual(probe.calls, 1)
        fake_time[0] = 115.0
        self.assertEqual(gate.sample()["adc_raw"], 2)

    def test_pulse_rate_limit_also_applies_to_failed_sampling(self) -> None:
        class BrokenSampler:
            calls = 0

            def sample(self) -> dict[str, int]:
                self.calls += 1
                message = "ADC disconnected"
                raise OSError(message)

        fake_time = [10.0]
        probe = BrokenSampler()
        gate = ThrottledSampler(probe, minimum_interval=15, clock=lambda: fake_time[0])
        for i in range(5):
            with self.assertRaises(OSError if i == 0 else RuntimeError):
                gate.sample()
        self.assertEqual(probe.calls, 1)
        fake_time[0] = 25
        with self.assertRaises(OSError):
            gate.sample()
        self.assertEqual(probe.calls, 2)

    def test_fresh_sample_waits_for_pulse_limit_and_never_uses_cache(self) -> None:
        class CountingSampler:
            calls = 0

            def sample(self) -> dict[str, int]:
                self.calls += 1
                return {"adc_raw": self.calls * 100}

        fake_time = [100.0]
        waited = []

        def sleep(seconds: float) -> None:
            waited.append(seconds)
            fake_time[0] += seconds

        gate = ThrottledSampler(
            CountingSampler(),
            minimum_interval=15,
            clock=lambda: fake_time[0],
            sleep=sleep,
        )
        self.assertEqual(gate.sample()["adc_raw"], 100)
        fake_time[0] += 0.001
        self.assertEqual(gate.sample(force=True)["adc_raw"], 200)
        self.assertAlmostEqual(waited[0], 14.999, places=3)

    def test_fresh_wait_does_not_block_normal_read_and_coalesces_fresh_calls(
        self,
    ) -> None:
        class CountingSampler:
            calls = 0

            def sample(self) -> dict[str, int]:
                self.calls += 1
                return {"adc_raw": self.calls * 100}

        fake_time = [100.0]
        sleeping_count = [0]
        sleeping_lock = threading.Lock()
        both_sleeping = threading.Event()
        release_waiters = threading.Event()

        def fake_sleep(seconds: float) -> None:
            with sleeping_lock:
                sleeping_count[0] += 1
                if sleeping_count[0] == 2:
                    both_sleeping.set()
            release_waiters.wait(timeout=2)
            with sleeping_lock:
                fake_time[0] += seconds

        probe = CountingSampler()
        gate = ThrottledSampler(
            probe,
            minimum_interval=15,
            clock=lambda: fake_time[0],
            sleep=fake_sleep,
        )
        self.assertEqual(gate.sample()["adc_raw"], 100)
        fake_time[0] += 0.01
        results = []
        fresh_workers = [
            threading.Thread(target=lambda: results.append(gate.sample(force=True)))
            for _ in range(2)
        ]
        try:
            for worker in fresh_workers:
                worker.start()
            self.assertTrue(
                both_sleeping.wait(timeout=1), "Both fresh calls should wait"
            )
            cached = []
            ordinary = threading.Thread(target=lambda: cached.append(gate.sample()))
            ordinary.start()
            ordinary.join(timeout=0.2)
            self.assertFalse(
                ordinary.is_alive(), "Fresh cooldown blocked normal REST poll"
            )
            self.assertEqual(cached, [{"adc_raw": 100}])
        finally:
            release_waiters.set()
            for worker in fresh_workers:
                worker.join(timeout=2)
        self.assertEqual(probe.calls, 2)
        self.assertEqual(results, [{"adc_raw": 200}, {"adc_raw": 200}])

    def test_real_http_fresh_route_bypasses_cached_measurement(self) -> None:
        class CountingSampler:
            calls = 0

            def sample(self) -> dict[str, int]:
                self.calls += 1
                return {"adc_raw": self.calls * 100}

        try:
            server = make_server(
                "127.0.0.1", 0, CountingSampler(), minimum_interval=1.0
            )
        except PermissionError:
            self.skipTest("Local sandbox denies loopback sockets")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        try:
            thread.start()

            def read(suffix: str = "") -> int:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/reading{suffix}", timeout=2
                ) as response:
                    return json.load(response)["adc_raw"]

            self.assertEqual(read(), 100)
            self.assertEqual(read(), 100)
            self.assertEqual(read("/fresh"), 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_bridge_bounds_concurrent_connections(self) -> None:
        try:
            server = make_server("127.0.0.1", 0, self.make_sampler([]))
        except PermissionError:
            self.skipTest("Local sandbox denies loopback sockets")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        clients = []
        try:
            thread.start()
            clients.extend(
                socket.create_connection(server.server_address, timeout=1)
                for _ in range(8)
            )
            time.sleep(0.15)  # Allow server to accept each waiting client.
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/healthz", timeout=1
                ):
                    self.fail("Ninth connection should be rejected")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 503)
                exc.close()
            except (urllib.error.URLError, ConnectionResetError):
                # TCP may reset a rejected request that has unread bytes.
                pass
        finally:
            for client in clients:
                client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_adc_out_of_range_returns_503_and_switches_off(self) -> None:
        status, body = build_handler(self.make_sampler([5000] * 5)).route("/reading")
        self.assertEqual(status, 503)
        self.assertIn("outside 12-bit range", json.loads(body)["error"])
        self.assertEqual(self.power.transitions[-1], False)

    def test_bad_adc_text_returns_503_and_switches_off(self) -> None:
        status, _body = build_handler(self.make_sampler(["invalid"])).route("/reading")
        self.assertEqual(status, 503)
        self.assertEqual(self.power.transitions[-1], False)

    def test_ha_capture_scripts_use_direct_fresh_readings(self) -> None:
        conf = yaml.safe_load(
            (Path(__file__).parents[2] / "packages" / "soil_moisture.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("strelitzia_soil_measure", conf["rest_command"])
        self.assertIn("strelitzia_soil_measure_fresh", conf["rest_command"])
        for name in ("strelitzia_soil_capture_dry", "strelitzia_soil_capture_wet"):
            steps = conf["script"][name]["sequence"]
            self.assertEqual(
                steps[0]["action"], "rest_command.strelitzia_soil_measure_fresh"
            )
            self.assertEqual(steps[0]["response_variable"], "soil_sample")
            self.assertNotIn("delay", [next(iter(s)) for s in steps])
            self.assertIn("homeassistant.update_entity", str(steps))
            self.assertIn("wait_template", str(steps))

    def test_ha_availability_and_calibration_guard(self) -> None:
        conf = yaml.safe_load(
            (Path(__file__).parents[2] / "packages" / "soil_moisture.yaml").read_text(
                encoding="utf-8"
            )
        )
        adc = conf["rest"][0]["sensor"][0]
        self.assertIn("availability", adc)
        self.assertIn("adc_raw", adc["availability"])
        self.assertIn("strelitzia_soil_dry_calibrated", conf["input_boolean"])
        moisture = conf["template"][0]["sensor"][0]
        self.assertIn(
            "input_boolean.strelitzia_soil_dry_calibrated", moisture["availability"]
        )
        resistance = conf["template"][0]["sensor"][1]
        self.assertIn(
            "is_number(states('sensor.strelitzia_soil_adc'))",
            resistance["availability"],
        )
        dry_actions = conf["script"]["strelitzia_soil_capture_dry"]["sequence"]
        self.assertIn(
            "input_boolean.turn_on",
            [step.get("action") for step in dry_actions],
        )
        self.assertTrue(
            any(
                "input_number.strelitzia_soil_wet_adc" in str(step)
                for step in dry_actions
            )
        )
        self.assertFalse(
            any(step.get("stop", "").startswith("Сухой ADC") for step in dry_actions)
        )
        wet_actions = conf["script"]["strelitzia_soil_capture_wet"]["sequence"]
        self.assertIn("input_boolean.strelitzia_soil_dry_calibrated", str(wet_actions))


class BridgeLoggingTests(unittest.TestCase):
    """Verify private operational logs without opening sockets or powering GPIO."""

    def test_rotated_logs_remain_private_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "bridge.log"
            handler = soil_bridge._PrivateRotatingFileHandler(
                log_path, maxBytes=80, backupCount=2, encoding="utf-8"
            )
            try:
                for index in range(12):
                    handler.emit(
                        logging.makeLogRecord(
                            {"msg": "bounded safe event %s", "args": (index,)}
                        )
                    )
            finally:
                handler.close()
            logs = list(Path(directory).glob("bridge.log*"))
            self.assertEqual(len(logs), 3)
            self.assertLessEqual(sum(path.stat().st_size for path in logs), 240)
            for path in logs:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_main_logs_startup_and_completion_to_private_file(self) -> None:
        class FinishedServer:
            closed = False

            def serve_forever(self, *, poll_interval: float) -> None:
                return

            def server_close(self) -> None:
                self.closed = True

        server = FinishedServer()
        output = io.StringIO()
        create_directory = tempfile.mkdtemp
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(soil_bridge, "make_server", return_value=server),
                patch.object(
                    soil_bridge.tempfile,
                    "mkdtemp",
                    side_effect=lambda **kwargs: create_directory(
                        dir=directory, **kwargs
                    ),
                ),
                contextlib.redirect_stderr(output),
            ):
                soil_bridge.main()

            logs = list(Path(directory).glob("ha-soil-moisture-*/soil-bridge.log"))
            self.assertEqual(len(logs), 1)
            log_path = logs[0]
            contents = log_path.read_text(encoding="utf-8")
            self.assertIn("Starting soil bridge", contents)
            self.assertIn("Soil bridge completed", contents)
            self.assertIn(str(log_path), output.getvalue())
            self.assertEqual(stat.S_IMODE(log_path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
            self.assertTrue(server.closed)

    def test_main_logs_failure_without_error_details_and_closes_server(self) -> None:
        class FailedServer:
            closed = False

            def serve_forever(self, *, poll_interval: float) -> None:
                message = "private request details"
                raise RuntimeError(message)

            def server_close(self) -> None:
                self.closed = True

        server = FailedServer()
        output = io.StringIO()
        create_directory = tempfile.mkdtemp
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(soil_bridge, "make_server", return_value=server),
                patch.object(
                    soil_bridge.tempfile,
                    "mkdtemp",
                    side_effect=lambda **kwargs: create_directory(
                        dir=directory, **kwargs
                    ),
                ),
                contextlib.redirect_stderr(output),
                self.assertRaises(RuntimeError),
            ):
                soil_bridge.main()

            log_path = next(Path(directory).glob("ha-soil-moisture-*/soil-bridge.log"))
            contents = log_path.read_text(encoding="utf-8")
            self.assertIn("Soil bridge failed (RuntimeError)", contents)
            self.assertIn(str(log_path), output.getvalue())
            self.assertNotIn("private request details", contents + output.getvalue())
            self.assertTrue(server.closed)


if __name__ == "__main__":
    unittest.main()
