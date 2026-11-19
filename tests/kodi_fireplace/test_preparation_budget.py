"""Cold source preparation remains bounded without a premature rejection."""

from __future__ import annotations

import io
import json
import subprocess
import threading
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import test_rotation as fixtures
from test_youtube_resolver import load_resolver


class DelayedResolver:
    """Model the subprocess protocol without wall-clock delay or public traffic."""

    def __init__(self, clock, delay: float | None, target: str, tick) -> None:
        self.clock, self.tick = clock, tick
        self.ready_at = None if delay is None else clock.now + delay
        self.target = target
        self.pid = 123456
        self.returncode = None
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.reaped = False

    def communicate(self, timeout: float) -> tuple[str, str]:
        remaining = (
            float("inf") if self.ready_at is None else self.ready_at - self.clock.now
        )
        self.clock.now += min(timeout, max(0.0, remaining))
        self.tick()
        if self.ready_at is not None and self.clock.now >= self.ready_at:
            self.returncode = 0
            return json.dumps({"file": self.target, "title": "one.webm"}), ""
        command = "fixture-resolver"
        raise subprocess.TimeoutExpired(command, timeout)

    def wait(self, timeout: float) -> int:
        self.reaped = True
        if self.returncode is None:
            self.returncode = -9
        return self.returncode


class PreparationBudgetCases(fixtures.FixtureBase):
    """Exercise controller state, actual wait logic and cancellation together."""

    def setUp(self) -> None:
        super().setUp()
        self.clock = SimpleNamespace(now=100.0)
        self.native_start = self.clock.now
        self.delays: list[float | None] = []
        self.processes: list[DelayedResolver] = []
        self.tick = lambda: None
        clock = SimpleNamespace(monotonic=lambda: self.clock.now)
        for module in (fixtures.fireplace, fixtures.fireplace._rotation):
            clock_patch = patch.object(module, "time", clock)
            clock_patch.start()
            self.addCleanup(clock_patch.stop)
        self.native_rpc = self.kodi.rpc
        self.kodi.rpc = self.clocked_rpc
        process_patch = patch.object(
            fixtures.fireplace._rotation.subprocess, "Popen", self.spawn
        )
        process_patch.start()
        self.addCleanup(process_patch.stop)
        kill_patch = patch.object(fixtures.fireplace._rotation.os, "killpg")
        kill_patch.start()
        self.addCleanup(kill_patch.stop)

    def spawn(self, command, **options: Any) -> DelayedResolver:
        self.assertEqual(command[2], "--video-id")
        self.assertTrue(options["start_new_session"])
        process = DelayedResolver(
            self.clock, self.delays.pop(0), str(self.media), self.tick
        )
        self.processes.append(process)
        return process

    def clocked_rpc(self, method, params, deadline) -> Any:
        result = self.native_rpc(method, params, deadline)
        if method == "Player.Open":
            self.native_start = self.clock.now
        if method == "Player.GetProperties":
            elapsed = self.clock.now - self.native_start
            result["time"] = {
                "hours": 0,
                "minutes": 0,
                "seconds": int(elapsed),
                "milliseconds": round(elapsed % 1 * 1000),
            }
        return result

    def controller(self, *, count: int = 1, **options: Any) -> Any:
        controller = fixtures.fireplace.FireplaceController(
            self.kodi.rpc,
            self.root / "owner.json",
            sources=[
                {
                    "id": "youtube-" + str(index),
                    "kind": "youtube",
                    "youtube_id": "abcdefghij" + str(index),
                }
                for index in range(count)
            ],
            auto_monitor=False,
            chooser=lambda _items: None,
            progress_interval=0.25,
            **options,
        )
        self.controllers.append(controller)
        wait_patch = patch.object(
            controller._closing, "wait", side_effect=self.advance_wait
        )
        wait_patch.start()
        self.addCleanup(wait_patch.stop)
        return controller

    def advance_wait(self, timeout: float) -> bool:
        self.clock.now += timeout
        return False

    def assert_playing_once(self, controller, request_id: str) -> dict[str, Any]:
        try:
            proof = controller.start(request_id)
        except fixtures.fireplace.FireplaceError as error:
            self.fail("A viable cold source was rejected: " + str(error))
        self.assertEqual("playing", proof["state"])
        self.assertTrue(proof["owned"] and proof["progressing"])
        self.assertEqual(
            1, sum(method == "Player.Open" for method, _ in self.kodi.calls)
        )
        self.assertTrue(all(process.reaped for process in self.processes))
        return proof

    def test_thirty_second_source_preparation_can_play(self) -> None:
        self.delays = [30.0]
        controller = self.controller()
        self.assert_playing_once(controller, "cold-source")
        self.assertGreater(self.clock.now, 130.0)
        self.assertLess(self.clock.now, 200.0)
        self.assertFalse(any(method == "Player.Stop" for method, _ in self.kodi.calls))

    def test_resolver_wait_honors_the_callers_longer_preparation_deadline(self) -> None:
        self.delays = [30.0]
        try:
            target = fixtures.fireplace._rotation.resolve_source(
                {"kind": "youtube", "youtube_id": "abcdefghij0"},
                145.0,
                "cold-attempt",
            )
        except fixtures.fireplace.SourceUnavailable as error:
            self.fail("The helper imposed a hidden shorter deadline: " + str(error))
        self.assertEqual(str(self.media), target["file"])
        self.assertEqual(130.0, self.clock.now)
        self.assertTrue(self.processes[0].reaped)

    def test_fallback_preparation_shares_the_default_launch_budget(self) -> None:
        self.delays = [None, 40.0]
        controller = self.controller(count=2)
        proof = self.assert_playing_once(controller, "cold-fallback")
        self.assertEqual("youtube-1", proof["source_id"])
        self.assertEqual(2, len(self.processes))
        self.assertGreater(self.clock.now, 185.0)
        self.assertLess(self.clock.now, 200.0)

    def test_preparation_exhaustion_reports_timeout_without_opening(self) -> None:
        self.delays = [None, None]
        controller = self.controller(count=2, operation_timeout=60.0)
        with self.assertRaisesRegex(
            fixtures.fireplace.FireplaceError, "source_resolution_timeout"
        ):
            controller.start("bounded-cold-sources")
        self.assertLessEqual(self.clock.now, 160.0)
        self.assertEqual(2, len(self.processes))
        self.assertTrue(all(process.reaped for process in self.processes))
        self.assertFalse(any(method == "Player.Open" for method, _ in self.kodi.calls))
        self.assertEqual("source_resolution_timeout", controller.status()["error"])

    def test_stop_cancels_extended_preparation_without_opening(self) -> None:
        self.delays = [None]
        controller = self.controller()
        stopped, errors = [], []
        stopper = None

        def stop() -> None:
            try:
                stopped.append(controller.stop())
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        def cancel_after_old_deadline() -> None:
            nonlocal stopper
            if self.clock.now >= 125.0 and stopper is None:
                stopper = threading.Thread(target=stop)
                stopper.start()
                self.assertTrue(controller._cancelled.wait(1))

        self.tick = cancel_after_old_deadline
        try:
            with self.assertRaisesRegex(fixtures.fireplace.FireplaceError, "cancelled"):
                controller.start("cancel-cold-preparation")
        finally:
            if stopper is not None:
                stopper.join(timeout=3)
        self.assertEqual([], errors)
        self.assertEqual(1, len(stopped))
        self.assertEqual("idle", stopped[0]["state"])
        self.assertLess(self.clock.now, 145.0)
        self.assertTrue(all(process.reaped for process in self.processes))
        self.assertFalse(any(method == "Player.Open" for method, _ in self.kodi.calls))

    def test_source_diagnostics_do_not_expose_private_target(self) -> None:
        self.delays = [0.0]
        controller = self.controller()
        logs = io.StringIO()
        with redirect_stderr(logs):
            self.assert_playing_once(controller, "safe-source-diagnostics")
        events = [json.loads(line) for line in logs.getvalue().splitlines()]
        self.assertEqual(
            ["source_preparation_started", "source_preparation_ready"],
            [event["stage"] for event in events],
        )
        self.assertEqual("youtube-0", events[0]["source_id"])
        self.assertNotIn(str(self.media), logs.getvalue())


class ProviderDiagnosticsCases(unittest.TestCase):
    """Report the network boundary without logging its private request."""

    def test_provider_request_logs_safe_stage_and_duration(self) -> None:
        module = load_resolver()
        messages: list[str] = []
        log = SimpleNamespace(write=messages.append)
        video_id = "abcdefghij0"

        def responses(_video_id, *, opener) -> Any:
            payload = opener(
                "https://www.youtube.com/private?signed=never-log-this",
                b"private-request",
                {"Authorization": "private-header"},
                15,
            )
            yield json.loads(payload), "fixture"

        inner = SimpleNamespace(player_responses=responses)
        raw = {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {"videoId": video_id},
        }
        with (
            patch.object(module, "smarttube_modules", return_value=(inner, None)),
            patch.object(
                module, "urlopen", return_value=io.BytesIO(json.dumps(raw).encode())
            ),
            patch.object(
                module,
                "build_smarttube_response",
                return_value={"width": 1920, "height": 1080},
            ),
        ):
            result = module.prepare_smarttube(video_id, fixtures.ROOT, log)
        self.assertEqual(1080, result["height"])
        self.assertTrue(
            any("player_request_started" in message for message in messages)
        )
        self.assertTrue(
            any("player_request_received" in message for message in messages)
        )
        self.assertTrue(any("elapsed_ms=" in message for message in messages))
        self.assertNotIn("private", " ".join(messages))
        self.assertNotIn("signed=", " ".join(messages))
