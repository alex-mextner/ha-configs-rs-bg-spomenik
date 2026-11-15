"""Behavior tests for announcement selection, cache lifetime and recovery."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_core():
    path = ROOT / "custom_components/remote_announcements/core.py"
    if not path.exists():
        raise AssertionError("Announcement cache and coordinator are not implemented")
    spec = importlib.util.spec_from_file_location("remote_announcement_core", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SelectionTests(unittest.TestCase):
    def test_only_effective_changes_after_initial_state_are_announced(self):
        core = load_core()
        gate = core.TargetGate()
        self.assertFalse(gate.changed("chromecast"))
        self.assertFalse(gate.changed("chromecast"))
        self.assertTrue(gate.changed("speaker"))
        self.assertFalse(gate.changed("unavailable"))
        self.assertFalse(gate.changed("speaker"))
        self.assertTrue(gate.changed("tv"))

    def test_volume_does_not_lower_loud_music(self):
        core = load_core()
        for current, wanted in [(0.2, 0.5), (0.5, 0.5), (0.8, 0.8), (None, 0.5), (3, 1.0)]:
            with self.subTest(current=current):
                self.assertEqual(core.speech_volume(current, 0.5), wanted)

    def test_restore_never_overwrites_user_volume_or_different_content(self):
        core = load_core()
        self.assertTrue(core.can_restore_volume(0.5, 0.5, False))
        self.assertFalse(core.can_restore_volume(0.7, 0.5, False))
        self.assertFalse(core.can_restore_volume(0.5, 0.5, True))
        self.assertTrue(core.can_resume("track-1", "track-1", "paused", True))
        self.assertFalse(core.can_resume("track-1", "track-2", "paused", True))
        self.assertFalse(core.can_resume("track-1", "track-1", "playing", True))
        self.assertFalse(core.can_resume("track-1", "track-1", "paused", False))


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_generates_once_and_lookup_never_synthesizes(self):
        core = load_core()
        calls = []

        async def generate(text):
            calls.append(text)
            return b"valid-fixture-audio", 0.8

        with tempfile.TemporaryDirectory() as folder:
            cache = core.ClipCache(Path(folder), "bracho-voice", 1.25, {"tv": "Телевизор."})
            self.assertIsNone(cache.get("tv"))
            await cache.prepare(generate)
            first = cache.get("tv")
            self.assertIsNotNone(first)
            self.assertEqual(first.duration, 0.8)
            fresh = core.ClipCache(Path(folder), "bracho-voice", 1.25, {"tv": "Телевизор."})
            await fresh.prepare(generate)
            self.assertEqual(calls, ["Телевизор."])
            self.assertEqual(first.key, fresh.get("tv").key)
            self.assertIsNone(fresh.get("speaker"))
            self.assertEqual(calls, ["Телевизор."])

    async def test_corrupt_cache_is_rebuilt_only_during_prepare(self):
        core = load_core()
        calls = []

        async def generate(text):
            calls.append(text)
            return b"valid-fixture-audio", 0.9

        with tempfile.TemporaryDirectory() as folder:
            cache = core.ClipCache(Path(folder), "bracho-voice", 1.25, {"tv": "Телевизор."})
            await cache.prepare(generate)
            cache.get("tv").path.write_bytes(b"broken")
            fresh = core.ClipCache(Path(folder), "bracho-voice", 1.25, {"tv": "Телевизор."})
            self.assertIsNone(fresh.get("tv"))
            self.assertEqual(len(calls), 1)
            await fresh.prepare(generate)
            self.assertEqual(len(calls), 2)

    async def test_generation_failure_never_publishes_partial_manifest(self):
        core = load_core()

        async def generate(text):
            raise RuntimeError("provider unavailable")

        with tempfile.TemporaryDirectory() as folder:
            cache = core.ClipCache(Path(folder), "bracho-voice", 1.25, {"tv": "Телевизор."})
            with self.assertRaises(RuntimeError):
                await cache.prepare(generate)
            self.assertIsNone(cache.get("tv"))
            self.assertFalse(list(Path(folder).glob("*.part")))

    async def test_quick_changes_coalesce_without_cancelling_recovery(self):
        core = load_core()
        started, release = asyncio.Event(), asyncio.Event()
        history = []

        async def announce(target):
            history.append("start:" + target)
            started.set()
            if target == "tv":
                await release.wait()
            history.append("restore:" + target)

        queue = core.LatestAnnouncement(announce, debounce=0.01)
        queue.submit("chromecast")
        queue.submit("tv")
        await asyncio.wait_for(started.wait(), 1)
        queue.submit("projector")
        queue.submit("speaker")
        release.set()
        await queue.drain()
        self.assertEqual(history, ["start:tv", "restore:tv", "start:speaker", "restore:speaker"])

    async def test_failure_does_not_poison_later_route(self):
        core = load_core()
        results, errors = [], []
        started = asyncio.Event()

        async def announce(target):
            if target == "tv":
                started.set()
                raise RuntimeError("disconnected")
            results.append(target)

        queue = core.LatestAnnouncement(announce, debounce=0, on_error=errors.append)
        queue.submit("tv")
        await started.wait()
        await queue.drain()
        queue.submit("speaker")
        await queue.drain()
        self.assertEqual(results, ["speaker"])
        self.assertEqual([type(error) for error in errors], [RuntimeError])


if __name__ == "__main__":
    unittest.main()
