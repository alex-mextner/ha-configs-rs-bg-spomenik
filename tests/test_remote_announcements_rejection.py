"""Bounded rejection feedback queue tests without speakers or Home Assistant."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "custom_components/remote_announcements/rejection.py"
)


def load_feedback() -> Any:
    """Load the standalone queue without importing Home Assistant."""
    spec = importlib.util.spec_from_file_location("remote_rejection_under_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RejectionQueueTests(unittest.IsolatedAsyncioTestCase):
    """One logical rejection is audible once; new presses never cancel it."""

    async def asyncSetUp(self) -> None:
        self.assertTrue(SOURCE.is_file(), "Quiet rejection feedback is not implemented")
        self.feedback = load_feedback()
        self.now = 100.0
        self.played = []
        self.reports = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

        async def play(item) -> str:
            self.started.set()
            await self.release.wait()
            self.played.append(item.press_id)
            return "sent_unconfirmed"

        self.queue = self.feedback.RejectionQueue(
            play,
            report=lambda status, values: self.reports.append((status, values)),
            clock=lambda: self.now,
        )
        self.addAsyncCleanup(self.queue.stop, 0.01)

    async def test_same_press_rejected_by_two_guards_sounds_once(self) -> None:
        self.assertEqual(self.queue.submit("busy", "power:123")["status"], "queued")
        await self.started.wait()
        self.assertEqual(self.queue.submit("busy", "power:123")["status"], "duplicate")
        self.release.set()
        await self.queue.drain()
        self.assertEqual(self.played, ["power:123"])

    async def test_distinct_fast_presses_are_preserved_without_canceling_active_cue(
        self,
    ) -> None:
        self.queue.submit("off", "up:123")
        await self.started.wait()
        self.queue.submit("off", "up:124")
        self.queue.submit("off", "up:125")
        self.release.set()
        await self.queue.drain()
        self.assertEqual(self.played, ["up:123", "up:124", "up:125"])
        self.assertFalse(any(status == "not_delivered" for status, _ in self.reports))

    async def test_missing_identity_means_distinct_calls(self) -> None:
        self.queue.submit("off")
        self.queue.submit("off")
        self.release.set()
        await self.queue.drain()
        self.assertEqual(len(self.played), 2)
        self.assertNotEqual(self.played[0], self.played[1])

    async def test_expired_waiting_feedback_is_reported_and_not_replayed(self) -> None:
        self.queue.submit("busy", "first")
        await self.started.wait()
        self.queue.submit("busy", "stale")
        self.now += self.feedback.MAX_AGE + 0.01
        self.release.set()
        await self.queue.drain()
        self.assertEqual(self.played, ["first"])
        self.assertTrue(
            any(values.get("drop_reason") == "expired" for _, values in self.reports)
        )

    async def test_queue_overflow_reports_the_dropped_new_press(self) -> None:
        self.queue.submit("busy", "active")
        await self.started.wait()
        for index in range(self.feedback.MAX_PENDING):
            self.assertEqual(self.queue.submit("busy", str(index))["status"], "queued")
        result = self.queue.submit("busy", "overflow")
        self.assertEqual(result, {"status": "dropped", "reason": "queue_full"})
        self.release.set()
        await self.queue.drain()
        self.assertNotIn("overflow", self.played)
        self.assertEqual(len(self.played), 1 + self.feedback.MAX_PENDING)

    async def test_shutdown_drops_pending_and_does_not_accept_later_work(self) -> None:
        self.queue.submit("busy", "active")
        await self.started.wait()
        self.queue.submit("busy", "pending")
        self.release.set()
        await self.queue.stop(0.1)
        self.assertEqual(self.played, ["active"])
        self.assertEqual(self.queue.submit("busy", "late")["reason"], "closed")


class QuietVolumeTests(unittest.TestCase):
    """Validate the no-raise and no-unmute volume boundary."""

    def test_volume_ceiling_never_raises_existing_volume(self) -> None:
        self.assertTrue(SOURCE.is_file(), "Quiet rejection feedback is not implemented")
        feedback = load_feedback()
        self.assertEqual(feedback.quiet_volume(0.4), 0.1)
        self.assertEqual(feedback.quiet_volume(0.05), 0.05)
        for value in (None, False, 0, -0.1, 1.1, float("nan"), float("inf")):
            with (
                self.subTest(value=value),
                self.assertRaises(feedback.CueNotReadyError),
            ):
                feedback.quiet_volume(value)


if __name__ == "__main__":
    unittest.main()
