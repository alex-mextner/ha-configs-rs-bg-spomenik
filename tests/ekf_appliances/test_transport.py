"""Ordering tests at the asynchronous service boundary."""
import asyncio
from pathlib import Path
import sys
import unittest
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components"))
from ekf_appliances.transport import Transport


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_entire_six_frame_burst_precedes_waiting_command(self):
        events = []
        first = asyncio.Event()
        release = asyncio.Event()

        async def send(command):
            events.append(command)
            if len(events) == 1:
                first.set()
                await release.wait()

        async def pause(seconds):
            events.append(seconds)

        runtime = Transport(send)
        with patch("ekf_appliances.transport.asyncio.sleep", pause):
            burst = asyncio.create_task(runtime.async_send((520,650), repeats=6, gap=0.15))
            await first.wait()
            other = asyncio.create_task(runtime.async_send((562,5000)))
            release.set()
            await asyncio.gather(burst, other)
        self.assertEqual(events, ["b64:CAKKAg==",0.00117,0.15]*5+["b64:CAKKAg==",0.00117,"b64:MgKIEw==",0.005562])

    async def test_failure_aborts_burst_and_releases_lock(self):
        commands = []

        async def send(command):
            commands.append(command)
            if len(commands) == 2:
                raise RuntimeError("local transmitter unavailable")

        runtime = Transport(send)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            await runtime.async_send((520,650), repeats=6, gap=0)
        await runtime.async_send((562,5000))
        self.assertEqual(commands, ["b64:CAKKAg==","b64:CAKKAg==","b64:MgKIEw=="])

    async def test_bad_repeat_parameters_never_reach_sender(self):
        async def send(command):
            self.fail("invalid repeat request transmitted")
        runtime = Transport(send)
        for kwargs in ({"repeats":0},{"repeats":True},{"repeats":2,"gap":-1},{"gap":float("nan")}):
            with self.assertRaises(ValueError):
                await runtime.async_send((520,650), **kwargs)

    async def test_cancellation_preserves_last_frame_quiet_time(self):
        sent = asyncio.Event()
        when = []
        async def send(command):
            when.append(time.monotonic())
            sent.set()
        runtime = Transport(send)
        first = asyncio.create_task(runtime.async_send((20000,20000)))
        await sent.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await runtime.async_send((520,650))
        self.assertGreaterEqual(when[1] - when[0], 0.035)
