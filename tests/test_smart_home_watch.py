# Copyright (c) 2026 Alex Mextner
"""A real local inotify event substitutes for maintenance-lock polling."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/host"))
from ha_smart_home_watch import watch_marker


class MaintenanceEventsTests(unittest.IsolatedAsyncioTestCase):
    """Observe one owned temporary marker without touching a service."""

    async def test_creation_removal_and_stale_deadline_are_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maintenance"
            events = asyncio.Queue()

            async def callback(kind: str) -> None:
                await events.put((kind, path.exists()))

            task = asyncio.create_task(watch_marker(path, callback, 0.03))
            try:
                self.assertEqual(
                    await asyncio.wait_for(events.get(), 1), ("changed", False)
                )
                path.write_text("owned")
                self.assertEqual(
                    await asyncio.wait_for(events.get(), 1), ("changed", True)
                )
                self.assertEqual(
                    await asyncio.wait_for(events.get(), 1), ("expired", True)
                )
                path.unlink()
                self.assertEqual(
                    await asyncio.wait_for(events.get(), 1), ("changed", False)
                )
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task


if __name__ == "__main__":
    unittest.main()
