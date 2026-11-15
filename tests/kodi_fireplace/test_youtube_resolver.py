"""Prepared playback generation, audio requirements, and storage ownership."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/host/ha_kodi_youtube_resolve.py"
VIDEO_ID = "L_LUpnjgPso"


def load_resolver():
    spec = importlib.util.spec_from_file_location(
        "ha_youtube_resolver_under_test", SOURCE
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.state = self.home / ".config/ha-host-actions/youtube"
        self.state.mkdir(parents=True)
        self.request_id = str(uuid4())
        self.set_generation(self.request_id)
        self.module = load_resolver()
        self.log = Mock()
        self.video = {
            "url": "https://rr1.example.googlevideo.com/video?signed=private",
            "vcodec": "avc1.640028",
            "acodec": "none",
            "container": "mp4_dash",
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "http_headers": {"User-Agent": "public-player"},
        }
        self.audio = {
            "url": "https://rr1.example.googlevideo.com/audio?signed=private",
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "container": "m4a_dash",
        }
        self.result = {
            "id": VIDEO_ID,
            "title": "Public fireplace",
            "duration": 36000,
            "formats": [self.audio, self.video],
        }
        self.extractor = Mock()
        self.extractor.__enter__ = Mock(return_value=self.extractor)
        self.extractor.__exit__ = Mock(return_value=False)
        self.extractor.extract_info = Mock(side_effect=lambda *_a, **_kw: self.result)
        self.ytdlp = types.SimpleNamespace(YoutubeDL=Mock(return_value=self.extractor))
        self.selection = types.SimpleNamespace(
            analyze_formats=Mock(side_effect=self.analyze_fixture),
            pick_best_dash_video_format=Mock(return_value=self.video),
            build_dash_manifest_candidate=Mock(side_effect=self.build_fixture),
        )
        self.builder = types.SimpleNamespace(Manifest=object())
        self.payload = b"<MPD><Period>fixture from upstream builder</Period></MPD>"
        self.runtimes = patch.object(
            self.module,
            "runtime_modules",
            return_value=(
                self.ytdlp,
                self.builder,
                self.selection,
                Path("/runtime/deno"),
                "2026.10.26",
                "v2.9.8",
            ),
        )
        self.runtime_lookup = self.runtimes.start()
        self.addCleanup(self.runtimes.stop)

    def set_generation(self, request_id):
        (self.state / "current-request.json").write_text(
            json.dumps({"request_id": request_id})
        )

    @staticmethod
    def analyze_fixture(formats):
        video = [item for item in formats if item.get("vcodec") != "none"]
        audio = [item for item in formats if item.get("acodec") != "none"]
        return bool(video), bool(audio), video, audio

    def build_fixture(
        self,
        duration,
        videos,
        audios,
        have_video,
        have_audio,
        factory,
        capture,
        **kwargs,
    ):
        self.assertEqual(duration, 36000)
        self.assertEqual(videos, [self.video])
        self.assertEqual(audios, [self.audio])
        self.assertTrue(have_video and have_audio)
        self.assertIs(factory, self.builder.Manifest)
        owner = json.loads(
            (self.state / "requests" / self.request_id / "owner.json").read_text()
        )
        self.assertEqual(owner["owner"], self.module.OWNER_NAME)
        self.assertEqual(owner["request_id"], self.request_id)
        return {"url": capture(self.payload), "events": []}

    def resolve(self):
        return self.module.resolve(VIDEO_ID, self.request_id, self.home, self.log)

    def test_prepared_result_has_exact_http_identity_and_private_digest(self):
        result = self.resolve()
        expected = self.module.MANIFEST_BASE_URL + "?request_id=" + self.request_id
        self.assertEqual(result["file"], expected)
        self.assertEqual(
            result["open_file"],
            "plugin://plugin.video.hayoutube/?request_id=" + self.request_id,
        )
        directory = self.state / "requests" / self.request_id
        descriptor = json.loads((directory / "descriptor.json").read_text())
        self.assertEqual(descriptor["file"], expected)
        self.assertEqual(descriptor["manifest_file"], str(directory / "manifest.mpd"))
        self.assertEqual(
            descriptor["manifest_sha256"], hashlib.sha256(self.payload).hexdigest()
        )
        self.assertEqual((directory / "manifest.mpd").stat().st_mode & 0o777, 0o600)
        self.assertEqual((directory / "descriptor.json").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("signed=private", json.dumps(result))
        self.assertEqual(
            json.loads((self.state / "current-request.json").read_text()),
            {"request_id": self.request_id},
        )

    def test_hd_without_aac_is_rejected_before_builder(self):
        self.result["formats"] = [self.video]
        with self.assertRaisesRegex(
            self.module.ResolveError, "hd_audio_video_unavailable"
        ):
            self.resolve()
        self.selection.build_dash_manifest_candidate.assert_not_called()

    def test_audio_without_hd_video_is_rejected(self):
        self.result["formats"] = [
            self.audio,
            {**self.video, "height": 360, "width": 640},
        ]
        with self.assertRaisesRegex(
            self.module.ResolveError, "hd_audio_video_unavailable"
        ):
            self.resolve()
        self.selection.build_dash_manifest_candidate.assert_not_called()

    def test_unsupported_codec_and_resolution_are_filtered(self):
        self.result["formats"] = [
            self.audio,
            self.video,
            {**self.video, "vcodec": "vp09.00.51.08"},
            {**self.video, "height": 2160, "width": 3840},
            {**self.video, "height": 360, "width": 640},
        ]
        selected = self.module.select_hd_formats(self.result)
        self.assertEqual(selected, [self.audio, self.video])

    def test_stale_request_does_not_start_extraction(self):
        self.set_generation(str(uuid4()))
        with self.assertRaisesRegex(self.module.ResolveError, "request_cancelled"):
            self.resolve()
        self.runtime_lookup.assert_not_called()

    def test_cancel_during_extraction_cannot_publish(self):
        def extract(*_a, **_kw):
            self.set_generation(str(uuid4()))
            return self.result

        self.extractor.extract_info.side_effect = extract
        with self.assertRaisesRegex(self.module.ResolveError, "request_cancelled"):
            self.resolve()
        self.assertFalse((self.state / "requests" / self.request_id).exists())

    def test_cancel_during_builder_removes_own_unpublished_files(self):
        def build(*args, **kwargs):
            candidate = self.build_fixture(*args, **kwargs)
            self.set_generation(str(uuid4()))
            return candidate

        self.selection.build_dash_manifest_candidate.side_effect = build
        with self.assertRaisesRegex(self.module.ResolveError, "request_cancelled"):
            self.resolve()
        self.assertFalse((self.state / "requests" / self.request_id).exists())

    def test_wrong_upstream_video_identity_cannot_publish(self):
        self.result["id"] = "different01"
        with self.assertRaisesRegex(self.module.ResolveError, "unexpected_video"):
            self.resolve()
        self.selection.build_dash_manifest_candidate.assert_not_called()

    def make_owned_request(self, request_id, age):
        directory = self.state / "requests" / request_id
        directory.mkdir(parents=True)
        (directory / "owner.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "owner": self.module.OWNER_NAME,
                    "request_id": request_id,
                }
            )
        )
        (directory / "descriptor.json").write_text(
            json.dumps({"version": 1, "request_id": request_id})
        )
        (directory / "manifest.mpd").write_text("<MPD/>")
        modified = time.time() - age
        os.utime(directory, (modified, modified))
        return directory

    def test_cleanup_preserves_current_and_unknown_files(self):
        active = self.make_owned_request(self.request_id, age=200000)
        old = self.make_owned_request(str(uuid4()), age=200000)
        unknown = self.make_owned_request(str(uuid4()), age=200000)
        (unknown / "human-note.txt").write_text("preserve")
        os.utime(unknown, (time.time() - 200000, time.time() - 200000))
        other = self.state / "requests" / "another-addon"
        other.mkdir()
        (other / "manifest.mpd").write_text("preserve")
        self.module.cleanup_obsolete(self.state, time.time())
        self.assertTrue(active.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(other.exists())
        self.assertFalse(old.exists())

    def test_cleanup_bounds_obsolete_owned_request_count(self):
        directories = [
            self.make_owned_request(str(uuid4()), age=60 + index) for index in range(4)
        ]
        with patch.object(self.module, "RETAIN_REQUESTS", 2):
            self.module.cleanup_obsolete(self.state, time.time())
        self.assertEqual(
            [path.exists() for path in directories], [True, True, False, False]
        )

    def test_headers_never_retain_auth_or_cookies(self):
        self.assertEqual(
            self.module.safe_headers(
                {
                    "User-Agent": "public",
                    "Cookie": "private",
                    "Authorization": "private",
                    "X-HA-Host-Token": "private",
                    "Referer": "bad\r\nheader",
                }
            ),
            {"User-Agent": "public"},
        )

    def test_invalid_identifiers_never_reach_runtime(self):
        for video_id, request_id in (
            (VIDEO_ID, "../other"),
            ("../other", self.request_id),
        ):
            with (
                self.subTest(video_id=video_id),
                self.assertRaises(self.module.ResolveError),
            ):
                self.module.resolve(video_id, request_id, self.home, self.log)
        self.runtime_lookup.assert_not_called()

    def test_failure_logs_do_not_expose_provider_exception(self):
        log = Mock()
        argv = ["resolver", "--video-id", VIDEO_ID, "--request-id", self.request_id]
        with (
            patch.object(self.module, "SafeLog", return_value=log),
            patch.object(
                self.module, "resolve", side_effect=RuntimeError("private-signed-url")
            ),
            patch.object(self.module.sys, "argv", argv),
            patch.object(self.module.signal, "signal"),
            patch.object(self.module.signal, "alarm"),
        ):
            self.assertEqual(self.module.main(), 1)
        self.assertNotIn("private-signed-url", str(log.mock_calls))
        log.finish.assert_called_once_with(1)

    def test_cleanup_reclaims_owned_preparations_killed_before_descriptor(self):
        active = self.make_owned_request(self.request_id, age=200000)
        (active / "descriptor.json").unlink()
        building = self.make_owned_request(str(uuid4()), age=200000)
        (building / "descriptor.json").unlink()
        (building / "manifest.mpd").unlink()
        (building / ".manifest.tmp").write_bytes(b"partial signed manifest")
        published_mpd = self.make_owned_request(str(uuid4()), age=200000)
        (published_mpd / "descriptor.json").unlink()
        unknown = self.state / "requests" / str(uuid4())
        unknown.mkdir()
        (unknown / "manifest.mpd").write_bytes(b"unknown ownership")
        old = time.time() - 200000
        for directory in (active, building, published_mpd, unknown):
            os.utime(directory, (old, old))
        self.module.cleanup_obsolete(self.state, time.time())
        self.assertTrue(active.exists())
        self.assertTrue(unknown.exists())
        self.assertFalse(building.exists())
        self.assertFalse(published_mpd.exists())

    def test_log_retention_preserves_recent_unknown_and_other_logs(self):
        logs = self.home / "logs"
        logs.mkdir()
        now = time.time()
        paths = []
        for suffix, age in (("new", 1), ("old1", 300), ("old2", 400)):
            path = logs / (
                self.module.LOG_PREFIX + "20261115T000000Z-" + suffix + ".log"
            )
            path.write_text(
                "2026-11-15T00:00:00+00:00 starting log=" + str(path) + "\n"
            )
            path.chmod(0o600)
            os.utime(path, (now - age, now - age))
            paths.append(path)
        unknown = logs / (self.module.LOG_PREFIX + "20261115T000000Z-unknown.log")
        unknown.write_text("human file, not an automatic task log\n")
        unknown.chmod(0o600)
        os.utime(unknown, (now - 2000000, now - 2000000))
        other = logs / "ha-host-actions-production.log"
        other.write_text("preserve audit")
        self.module.prune_logs(logs, now=now, keep=2)
        self.assertEqual([path.exists() for path in paths], [True, True, False])
        self.assertTrue(unknown.exists())
        self.assertTrue(other.exists())
