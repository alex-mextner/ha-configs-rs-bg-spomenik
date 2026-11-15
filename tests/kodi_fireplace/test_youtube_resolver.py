"""Prepared playback generation, audio requirements, and storage ownership."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
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
        self.selection_path = self.state.parent / "fireplace-selection.json"
        self.set_selection()
        self.request_id = str(uuid4())
        self.set_generation(self.request_id)
        self.module = load_resolver()
        self.log = Mock()
        ranges = {
            "initRange": {"start": "0", "end": "735"},
            "indexRange": {"start": "736", "end": "1067"},
            "approxDurationMs": "36000000",
        }
        self.video = {
            **ranges,
            "itag": 137,
            "url": "https://rr1.example.googlevideo.com/video?signed=private",
            "mimeType": 'video/mp4; codecs="avc1.640028"',
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "bitrate": 3500000,
        }
        self.audio = {
            **ranges,
            "itag": 140,
            "url": "https://rr1.example.googlevideo.com/audio?signed=private",
            "mimeType": 'audio/mp4; codecs="mp4a.40.2"',
            "audioChannels": 2,
            "audioSampleRate": "44100",
            "bitrate": 128000,
        }
        self.result = {
            "id": VIDEO_ID,
            "title": "Public fireplace",
            "duration": 36000,
            "formats": [self.audio, self.video],
        }
        self.player = Mock(side_effect=lambda: self.result)
        self.inner = types.SimpleNamespace(
            CLIENTS={"android_testsuite": {"headers": {"User-Agent": "public-player"}}},
            player_responses=Mock(side_effect=self.player_responses),
        )
        self.builder = types.SimpleNamespace(
            manifest=Mock(side_effect=self.build_fixture),
            _usable=lambda item: isinstance(item, dict) and not item.get("isDrc"),
            _mime=lambda item: (
                item["mimeType"].partition(";")[0],
                item["mimeType"].partition('codecs="')[2].rstrip('"'),
            ),
        )
        self.payload = b"<MPD><Period>fixture from upstream builder</Period></MPD>"
        self.runtimes = patch.object(
            self.module, "smarttube_modules", return_value=(self.inner, self.builder)
        )
        self.runtime_lookup = self.runtimes.start()
        self.addCleanup(self.runtimes.stop)
        self.transport_patch = patch.object(
            self.module, "urlopen", side_effect=self.transport
        )
        self.transport_patch.start()
        self.addCleanup(self.transport_patch.stop)

    def set_generation(self, request_id):
        (self.state / "current-request.json").write_text(
            json.dumps({"request_id": request_id})
        )

    def set_selection(self, active=None, pending=None, phase="preparing"):
        records = {}
        for role, attempt_id in (("active", active), ("pending", pending)):
            identity = None
            if attempt_id is not None:
                identity = {
                    "attempt_id": attempt_id,
                    "source_id": "youtube-" + role,
                    "source_kind": "youtube",
                    "request_id": "voice-" + role,
                    "launch_started_at": "2026-11-15T00:00:00+00:00",
                    "title": "Public fireplace",
                }
                if role == "pending":
                    identity["phase"] = phase
                if role == "active" or phase != "preparing":
                    identity.update(file_hash="a" * 64, open_hash="b" * 64)
            records[role] = identity
        self.selection_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "last_successful_source_id": "youtube-active" if active else None,
                    **records,
                }
            )
        )

    def transport(self, _request, **_kwargs):
        return io.BytesIO(
            json.dumps(
                {
                    "playabilityStatus": {"status": "OK"},
                    "videoDetails": {"videoId": self.result["id"]},
                }
            ).encode()
        )

    def player_responses(self, _video_id, *, opener):
        result = self.player()
        opener(
            "https://www.youtube.com/youtubei/v1/player",
            b"{}",
            {"User-Agent": "public-player"},
            15,
        )
        yield (
            {
                "playable": True,
                "is_live": False,
                "title": result["title"],
                "length_seconds": result["duration"],
                "adaptive": result["formats"],
            },
            "android_testsuite",
        )

    def build_fixture(self, formats, *, hdr):
        self.assertEqual(formats, [self.video, self.audio])
        self.assertFalse(hdr)
        owner = json.loads(
            (self.state / "requests" / self.request_id / "owner.json").read_text()
        )
        self.assertEqual(owner["owner"], self.module.OWNER_NAME)
        self.assertEqual(owner["request_id"], self.request_id)
        return self.payload.decode()

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
        self.builder.manifest.assert_not_called()

    def test_audio_without_hd_video_is_rejected(self):
        self.result["formats"] = [
            self.audio,
            {**self.video, "height": 360, "width": 640},
        ]
        with self.assertRaisesRegex(
            self.module.ResolveError, "hd_audio_video_unavailable"
        ):
            self.resolve()
        self.builder.manifest.assert_not_called()

    def test_unsupported_codec_and_resolution_are_filtered(self):
        self.result["formats"] = [
            self.audio,
            self.video,
            {**self.video, "mimeType": 'video/webm; codecs="vp09.00.51.08"'},
            {**self.video, "height": 2160, "width": 3840},
            {**self.video, "height": 360, "width": 640},
        ]
        selected = self.module.select_smarttube_formats(
            self.result["formats"], self.builder
        )
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

        self.player.side_effect = extract
        with self.assertRaisesRegex(self.module.ResolveError, "request_cancelled"):
            self.resolve()
        self.assertFalse((self.state / "requests" / self.request_id).exists())

    def test_cancel_during_builder_removes_own_unpublished_files(self):
        def build(*args, **kwargs):
            candidate = self.build_fixture(*args, **kwargs)
            self.set_generation(str(uuid4()))
            return candidate

        self.builder.manifest.side_effect = build
        with self.assertRaisesRegex(self.module.ResolveError, "request_cancelled"):
            self.resolve()
        self.assertFalse((self.state / "requests" / self.request_id).exists())

    def test_wrong_upstream_video_identity_cannot_publish(self):
        self.result["id"] = "different01"
        with self.assertRaisesRegex(self.module.ResolveError, "unexpected_video"):
            self.resolve()
        self.builder.manifest.assert_not_called()

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

    def assert_manifest_retained(self, directory):
        manifest = directory / "manifest.mpd"
        self.assertTrue(manifest.is_file())
        self.assertEqual(manifest.read_text(), "<MPD/>")

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

    def test_failed_replacement_keeps_aged_active_manifest(self):
        active = self.make_owned_request(str(uuid4()), age=200000)
        self.set_selection(active=active.name, pending=self.request_id)
        self.result["formats"] = [self.audio]
        with self.assertRaisesRegex(
            self.module.ResolveError, "hd_audio_video_unavailable"
        ):
            self.resolve()
        self.set_generation(active.name)
        self.assertTrue((active / "descriptor.json").is_file())
        self.assert_manifest_retained(active)

    def test_failed_replacement_keeps_count_evicted_active_manifest(self):
        active = self.make_owned_request(str(uuid4()), age=300)
        newer = [
            self.make_owned_request(str(uuid4()), age=60 + index) for index in range(3)
        ]
        self.set_selection(active=active.name, pending=self.request_id)
        self.result["formats"] = [self.audio]
        with (
            patch.object(self.module, "RETAIN_REQUESTS", 2),
            self.assertRaisesRegex(
                self.module.ResolveError, "hd_audio_video_unavailable"
            ),
        ):
            self.resolve()
        self.set_generation(active.name)
        self.assert_manifest_retained(active)
        self.assertEqual([path.exists() for path in newer], [True, True, False])

    def test_cleanup_preserves_pending_attempt_in_each_durable_phase(self):
        for phase in ("preparing", "prepared", "opening"):
            with self.subTest(phase=phase):
                pending = self.make_owned_request(str(uuid4()), age=200000)
                obsolete = self.make_owned_request(str(uuid4()), age=200000)
                self.set_selection(pending=pending.name, phase=phase)
                self.module.cleanup_obsolete(self.state, time.time())
                self.assert_manifest_retained(pending)
                self.assertFalse(obsolete.exists())

    def test_cleanup_skips_malformed_selection(self):
        invalid_states = [
            "{",
            "[]",
            '{"version": 2, "active": null, "pending": null}',
            '{"version": true, "active": null, "pending": null}',
            '{"version": 1}',
            '{"version": 1, "active": [], "pending": null}',
            '{"version": 1, "active": {"attempt_id": null}, "pending": null}',
            '{"version": 1, "active": {"source_kind": "youtube"}, "pending": null}',
            '{"version": 1, "active": null, "pending": {"attempt_id": "../other"}}',
        ]
        for raw in invalid_states:
            with self.subTest(selection=raw):
                old = self.make_owned_request(str(uuid4()), age=200000)
                self.selection_path.write_text(raw)
                self.module.cleanup_obsolete(self.state, time.time())
                self.assert_manifest_retained(old)

    def test_cleanup_skips_missing_selection(self):
        old = self.make_owned_request(str(uuid4()), age=200000)
        self.selection_path.unlink()
        self.module.cleanup_obsolete(self.state, time.time())
        self.assert_manifest_retained(old)

    def test_cleanup_skips_unreadable_selection(self):
        old = self.make_owned_request(str(uuid4()), age=200000)
        read_json = self.module.read_json

        def unavailable_selection(path, limit):
            if path == self.selection_path:
                raise PermissionError("selection unavailable")
            return read_json(path, limit)

        with patch.object(self.module, "read_json", side_effect=unavailable_selection):
            self.module.cleanup_obsolete(self.state, time.time())
        self.assert_manifest_retained(old)

    def test_cleanup_skips_symlink_selection(self):
        old = self.make_owned_request(str(uuid4()), age=200000)
        target = self.home / "other-selection.json"
        self.selection_path.rename(target)
        self.selection_path.symlink_to(target)
        self.module.cleanup_obsolete(self.state, time.time())
        self.assert_manifest_retained(old)

    def test_cleanup_rechecks_new_active_selection_before_removal(self):
        old = self.make_owned_request(str(uuid4()), age=200000)
        remove = self.module.remove_obsolete_request

        def select_before_removal(state, directory):
            self.set_selection(active=directory.name)
            remove(state, directory)

        with patch.object(
            self.module, "remove_obsolete_request", side_effect=select_before_removal
        ):
            self.module.cleanup_obsolete(self.state, time.time())
        self.assert_manifest_retained(old)

    def test_cleanup_rechecks_malformed_selection_before_removal(self):
        old = self.make_owned_request(str(uuid4()), age=200000)
        remove = self.module.remove_obsolete_request

        def corrupt_before_removal(state, directory):
            self.selection_path.write_text("{")
            remove(state, directory)

        with patch.object(
            self.module, "remove_obsolete_request", side_effect=corrupt_before_removal
        ):
            self.module.cleanup_obsolete(self.state, time.time())
        self.assert_manifest_retained(old)

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
