"""Behavior tests for announcement selection, cache lifetime and recovery."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from scripts import prepare_remote_announcements, provision_remote_announcements

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def load_core() -> ModuleType:
    path = ROOT / "custom_components/remote_announcements/core.py"
    if not path.exists():
        message = "Announcement cache and coordinator are not implemented"
        raise AssertionError(message)
    spec = importlib.util.spec_from_file_location("remote_announcement_core", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SelectionTests(unittest.TestCase):
    """Check effective-target gating and conservative recovery decisions."""

    def test_only_effective_changes_after_initial_state_are_announced(self) -> None:
        core = load_core()
        gate = core.TargetGate()
        self.assertFalse(gate.changed("chromecast"))
        self.assertFalse(gate.changed("chromecast"))
        self.assertTrue(gate.changed("speaker"))
        self.assertFalse(gate.changed("unavailable"))
        self.assertFalse(gate.changed("speaker"))
        self.assertTrue(gate.changed("tv"))

    def test_volume_does_not_lower_loud_music(self) -> None:
        core = load_core()
        for current, wanted in [
            (0.2, 0.5),
            (0.5, 0.5),
            (0.8, 0.8),
            (None, 0.5),
            (3, 1.0),
        ]:
            with self.subTest(current=current):
                self.assertEqual(core.speech_volume(current, 0.5), wanted)

    def test_restore_never_overwrites_user_volume_or_different_content(self) -> None:
        core = load_core()
        self.assertTrue(core.can_restore_volume(0.5, 0.5, muted=False))
        self.assertFalse(core.can_restore_volume(0.7, 0.5, muted=False))
        self.assertFalse(core.can_restore_volume(0.5, 0.5, muted=True))
        self.assertTrue(
            core.can_resume("track-1", "track-1", "paused", paused_by_us=True)
        )
        self.assertFalse(
            core.can_resume("track-1", "track-2", "paused", paused_by_us=True)
        )
        self.assertFalse(
            core.can_resume("track-1", "track-1", "playing", paused_by_us=True)
        )
        self.assertFalse(
            core.can_resume("track-1", "track-1", "paused", paused_by_us=False)
        )

    def test_error_diagnostics_strip_private_messages_and_chains(self) -> None:
        core = load_core()
        private = "fixture" + "-private-credential"

        def fail() -> None:
            raise RuntimeError(private) from ValueError(private)

        try:
            fail()
        except RuntimeError as error:
            diagnostic = "".join(
                traceback.format_exception(*core.exception_info(error))
            )
        self.assertIn("RuntimeError", diagnostic)
        self.assertNotIn(private, diagnostic)
        self.assertNotIn("ValueError:", diagnostic)

    def test_cli_urls_are_http_only_before_credentials_are_sent(self) -> None:
        invalid = [
            "file:///fixture",
            "ftp://ha.invalid",
            "http:///missing-host",
            "http://user:fixture@ha.invalid",
            "http://ha.invalid?token=fixture",
            "http://ha.invalid#fragment",
        ]
        for module in (prepare_remote_announcements, provision_remote_announcements):
            self.assertEqual(
                module.http_base("http://localhost:8123/"), "http://localhost:8123"
            )
            for value in invalid:
                with (
                    self.subTest(module=module.__name__, value=value),
                    self.assertRaises(ValueError),
                ):
                    module.http_base(value)
        with tempfile.TemporaryDirectory() as folder:
            token_file = Path(folder) / "environment"
            token_file.write_text("HA_TOKEN=fixture-local-credential\n")
            args = argparse.Namespace(token_file=token_file, base_url="file:///fixture")
            with (
                patch.object(provision_remote_announcements, "urlopen") as request,
                self.assertRaises(ValueError),
            ):
                provision_remote_announcements.prepare_home_assistant(args, Mock())
            request.assert_not_called()


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    """Check cache persistence and announcement queue lifecycle."""

    async def test_requested_labels_regenerate_only_changed_cache_entries(self) -> None:
        """Preserve speaker audio while migrating the four exact requested labels."""
        core = load_core()
        calls = []

        async def generate(text: str) -> tuple[bytes, float]:
            calls.append(text)
            return text.encode(), 0.6

        old_phrases = {
            "chromecast": "Хромкаст.",
            "tv": "Телевизор.",
            "kodi": "Коди.",
            "projector": "Проектор.",
            "speaker": "Колонка.",
        }
        with tempfile.TemporaryDirectory() as folder:
            old = core.ClipCache(
                Path(folder), core.VOICE_ID, core.DEFAULT_TEMPO, old_phrases
            )
            await old.prepare(generate)
            speaker_key = old.get("speaker").key
            calls.clear()
            current = core.ClipCache(Path(folder), core.VOICE_ID, core.DEFAULT_TEMPO)
            await current.prepare(generate)
            self.assertEqual(calls, ["chrome cast.", "TV.", "kodi.", "проектор."])
            self.assertEqual(current.get("speaker").key, speaker_key)
            calls.clear()
            await current.prepare(generate)
            self.assertEqual(calls, [])

    async def test_cache_generates_once_and_lookup_never_synthesizes(self) -> None:
        core = load_core()
        calls = []

        async def generate(text) -> tuple[bytes, float]:
            calls.append(text)
            return b"valid-fixture-audio", 0.8

        with tempfile.TemporaryDirectory() as folder:
            cache = core.ClipCache(
                Path(folder), "bracho-voice", 1.25, {"tv": "Телевизор."}
            )
            self.assertIsNone(cache.get("tv"))
            await cache.prepare(generate)
            first = cache.get("tv")
            self.assertIsNotNone(first)
            self.assertEqual(first.duration, 0.8)
            fresh = core.ClipCache(
                Path(folder), "bracho-voice", 1.25, {"tv": "Телевизор."}
            )
            await fresh.prepare(generate)
            self.assertEqual(calls, ["Телевизор."])
            self.assertEqual(first.key, fresh.get("tv").key)
            self.assertIsNone(fresh.get("speaker"))
            self.assertEqual(calls, ["Телевизор."])

    async def test_corrupt_cache_is_rebuilt_only_during_prepare(self) -> None:
        core = load_core()
        calls = []

        async def generate(text) -> tuple[bytes, float]:
            calls.append(text)
            return b"valid-fixture-audio", 0.9

        with tempfile.TemporaryDirectory() as folder:
            cache = core.ClipCache(
                Path(folder), "bracho-voice", 1.25, {"tv": "Телевизор."}
            )
            await cache.prepare(generate)
            cache.get("tv").path.write_bytes(b"broken")
            fresh = core.ClipCache(
                Path(folder), "bracho-voice", 1.25, {"tv": "Телевизор."}
            )
            self.assertIsNone(fresh.get("tv"))
            self.assertEqual(len(calls), 1)
            await fresh.prepare(generate)
            self.assertEqual(len(calls), 2)

    async def test_generation_failure_never_publishes_partial_manifest(self) -> None:
        core = load_core()

        async def generate(_text) -> tuple[bytes, float]:
            message = "provider unavailable"
            raise RuntimeError(message)

        with tempfile.TemporaryDirectory() as folder:
            cache = core.ClipCache(
                Path(folder), "bracho-voice", 1.25, {"tv": "Телевизор."}
            )
            with self.assertRaises(RuntimeError):
                await cache.prepare(generate)
            self.assertIsNone(cache.get("tv"))
            self.assertFalse(
                await asyncio.to_thread(lambda: list(Path(folder).glob("*.part")))
            )

    async def test_quick_changes_coalesce_without_cancelling_recovery(self) -> None:
        core = load_core()
        started, release = asyncio.Event(), asyncio.Event()
        history = []

        async def announce(target) -> None:
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
        self.assertEqual(
            history, ["start:tv", "restore:tv", "start:speaker", "restore:speaker"]
        )

    async def test_failure_does_not_poison_later_route(self) -> None:
        core = load_core()
        results, errors = [], []
        started = asyncio.Event()

        async def announce(target) -> None:
            if target == "tv":
                started.set()
                message = "disconnected"
                raise RuntimeError(message)
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
