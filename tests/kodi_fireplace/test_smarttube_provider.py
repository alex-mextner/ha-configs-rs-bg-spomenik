"""Fast public playback uses pinned SmartTube without the slow yt-dlp path."""

from __future__ import annotations

import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from test_youtube_resolver import load_resolver

VIDEO_ID = "2rkB6CNfqJ4"


def public_player_response():
    """Minimal raw InnerTube response with real adaptive-format field names."""
    ranges = {
        "initRange": {"start": "0", "end": "735"},
        "indexRange": {"start": "736", "end": "1067"},
        "approxDurationMs": "36012000",
    }
    return {
        "playabilityStatus": {"status": "OK"},
        "videoDetails": {
            "videoId": VIDEO_ID,
            "title": "Fast public fireplace",
            "lengthSeconds": "36012",
            "isLiveContent": False,
        },
        "streamingData": {
            "adaptiveFormats": [
                {
                    **ranges,
                    "itag": 137,
                    "mimeType": 'video/mp4; codecs="avc1.640028"',
                    "url": "https://rr1.fixture.googlevideo.com/video?signed=private",
                    "width": 1920,
                    "height": 1080,
                    "fps": 30,
                    "bitrate": 3500000,
                },
                {
                    **ranges,
                    "itag": 140,
                    "mimeType": 'audio/mp4; codecs="mp4a.40.2"',
                    "url": "https://rr1.fixture.googlevideo.com/audio?signed=private",
                    "audioChannels": 2,
                    "audioSampleRate": "44100",
                    "bitrate": 128000,
                },
            ],
        },
    }


def parsed_player_response(raw):
    """Model the public parser boundary, which intentionally drops videoId."""
    details = raw["videoDetails"]
    return {
        "playable": raw["playabilityStatus"]["status"] == "OK",
        "title": details["title"],
        "is_live": details["isLiveContent"],
        "length_seconds": int(details["lengthSeconds"]),
        "adaptive": raw["streamingData"]["adaptiveFormats"],
    }


class SmartTubeProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="smarttube-provider-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.state = self.home / ".config/ha-host-actions/youtube"
        self.state.mkdir(parents=True)
        self.request_id = str(uuid4())
        (self.state / "current-request.json").write_text(
            json.dumps({"request_id": self.request_id})
        )
        (self.state.parent / "fireplace-selection.json").write_text(
            json.dumps({"version": 1, "active": None, "pending": None})
        )
        self.module = load_resolver()
        self.log = Mock()
        self.raw = public_player_response()
        self.payload = "<MPD><Period>fast upstream fixture</Period></MPD>"
        self.inner = types.SimpleNamespace(
            CLIENTS={"android_testsuite": {"headers": {"User-Agent": "public-client"}}},
            player_responses=Mock(side_effect=self.responses),
        )
        self.dash = types.SimpleNamespace(
            manifest=Mock(return_value=self.payload),
            _usable=lambda item: isinstance(item, dict) and not item.get("isDrc"),
            _mime=lambda item: (
                item["mimeType"].partition(";")[0],
                item["mimeType"].partition('codecs="')[2].rstrip('"'),
            ),
        )
        self.runtime_patch = patch.object(
            self.module,
            "smarttube_modules",
            return_value=(self.inner, self.dash),
            create=True,
        )
        self.runtime_lookup = self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        self.slow_patch = patch.object(
            self.module,
            "runtime_modules",
            side_effect=AssertionError("The legacy slow provider was accessed"),
            create=True,
        )
        self.slow_lookup = self.slow_patch.start()
        self.addCleanup(self.slow_patch.stop)
        self.transport_patch = patch.object(
            self.module, "urlopen", side_effect=self.transport, create=True
        )
        self.http = self.transport_patch.start()
        self.addCleanup(self.transport_patch.stop)

    def transport(self, _request, **_kwargs):
        return io.BytesIO(json.dumps(self.raw).encode())

    def responses(self, video_id, *, opener):
        self.assertEqual(video_id, VIDEO_ID)
        raw = opener(
            "https://www.youtube.com/youtubei/v1/player",
            b"{}",
            {"User-Agent": "public-client"},
            15,
        )
        yield parsed_player_response(json.loads(raw)), "android_testsuite"

    def resolve(self):
        try:
            return self.module.resolve(VIDEO_ID, self.request_id, self.home, self.log)
        except AssertionError as exc:
            self.fail(str(exc))

    def test_primary_uses_smarttube_and_preserves_private_protocol(self):
        result = self.resolve()
        self.assertEqual(result["title"], "Fast public fireplace")
        self.assertEqual(
            result["open_file"],
            "plugin://plugin.video.hayoutube/?request_id=" + self.request_id,
        )
        self.runtime_lookup.assert_called_once_with(self.home)
        self.slow_lookup.assert_not_called()
        directory = self.state / "requests" / self.request_id
        self.assertEqual((directory / "manifest.mpd").read_text(), self.payload)
        descriptor = json.loads((directory / "descriptor.json").read_text())
        self.assertEqual((descriptor["width"], descriptor["height"]), (1920, 1080))
        self.assertEqual(descriptor["headers"], {"User-Agent": "public-client"})
        self.assertNotIn("signed=private", json.dumps(result))
        self.assertLessEqual(self.http.call_args.kwargs["timeout"], 8)

    def test_actual_raw_source_identity_is_required(self):
        for identity in ("different01", None):
            with self.subTest(identity=identity):
                self.raw["videoDetails"]["videoId"] = identity
                with self.assertRaisesRegex(
                    self.module.ResolveError, "unexpected_video"
                ):
                    self.resolve()
                self.assertFalse((self.state / "requests" / self.request_id).exists())

    def test_live_source_is_rejected(self):
        self.raw["videoDetails"]["isLiveContent"] = True
        with self.assertRaisesRegex(
            self.module.ResolveError, "hd_audio_video_unavailable"
        ):
            self.resolve()
        self.dash.manifest.assert_not_called()

    def test_hd_requires_aac_and_safe_media_origin(self):
        formats = self.raw["streamingData"]["adaptiveFormats"]
        for invalid in (
            [formats[0]],
            [{**formats[0], "height": 360, "width": 640}, formats[1]],
            [{**formats[0], "url": "http://127.0.0.1/private"}, formats[1]],
            [
                formats[0],
                {
                    **formats[1],
                    "url": "https://user:secret@rr1.fixture.googlevideo.com/a",
                },
            ],
        ):
            with self.subTest(formats=len(invalid)):
                self.raw["streamingData"]["adaptiveFormats"] = invalid
                with self.assertRaisesRegex(
                    self.module.ResolveError, "hd_audio_video_unavailable"
                ):
                    self.resolve()
                self.assertFalse((self.state / "requests" / self.request_id).exists())

    def test_builder_receives_only_supported_video_and_non_drc_aac(self):
        video, audio = self.raw["streamingData"]["adaptiveFormats"]
        self.raw["streamingData"]["adaptiveFormats"] += [
            {**video, "mimeType": 'video/webm; codecs="vp09.00.51.08"'},
            {**video, "height": 2160, "width": 3840},
            {**audio, "isDrc": True},
        ]
        self.resolve()
        self.dash.manifest.assert_called_once_with([video, audio], hdr=False)

    def test_cancel_during_transport_cannot_publish(self):
        def cancelled(request, **kwargs):
            (self.state / "current-request.json").write_text(
                json.dumps({"request_id": str(uuid4())})
            )
            return self.transport(request, **kwargs)

        self.http.side_effect = cancelled
        with self.assertRaisesRegex(self.module.ResolveError, "request_cancelled"):
            self.resolve()
        self.assertFalse((self.state / "requests" / self.request_id).exists())

    def test_loader_executes_verified_bytes_after_source_replacement(self):
        path = self.home / "upstream.py"
        payload = b'VALUE = "verified"\n'
        path.write_bytes(payload)
        digest = self.module.hashlib.sha256(payload).hexdigest()
        create_module = self.module.importlib.util.module_from_spec

        def replace_source(spec):
            path.write_text('VALUE = "changed-after-verification"\n')
            return create_module(spec)

        with patch.object(
            self.module.importlib.util, "module_from_spec", side_effect=replace_source
        ):
            imported = self.module.load_pinned_module(self.home, path.name, digest)
        self.assertEqual(imported.VALUE, "verified")
        self.assertEqual(imported.__file__, str(path))
        self.assertEqual(imported.__spec__.origin, str(path))

    def test_modified_upstream_source_is_not_executed(self):
        self.runtime_patch.stop()
        directory = self.home / ".kodi/addons/plugin.video.smarttube/resources/lib"
        directory.mkdir(parents=True)
        for name in ("innertube.py", "dash.py"):
            (directory / name).write_text('raise RuntimeError("must not execute")\n')
        with self.assertRaisesRegex(
            self.module.ResolveError, "upstream_source_mismatch"
        ):
            self.resolve()
        self.assertFalse((self.state / "requests" / self.request_id).exists())
