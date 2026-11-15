"""CI contracts with real pinned OSS builders; only network transport is faked.

The upstream sources stay in a temporary directory, outside this repository.
Run through the existing native CI/Runpod suite, never on the home machines.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
import types
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs
from uuid import uuid4

from test_smarttube_provider import VIDEO_ID, public_player_response
from test_youtube_adapter import FakeItem, load_adapter
from test_youtube_resolver import load_resolver

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_COMMIT = "617f8f50504abebf69e234abcf48e78f43863ac3"  # v0.10.3, MIT
UPSTREAM_FILES = {
    "core/dash_builder.py": "d95f70e4e8df00e2b364034a6a17cb24a16c266ecfa77380c5901a28b7808969",
    "core/playback_selection.py": "9ea429b62e579b697853320b7248076cac0f3860c6d4d322f63deefeb1d33371",
    "LICENSE.md": "561d39342c6361920f8009f36f59250ac7d336d4540d2a461f55abc555877912",
}


def load_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch_pinned_source(
    directory,
    name,
    expected,
    repository="firsttris/plugin.video.sendtokodi",
    commit=UPSTREAM_COMMIT,
):
    url = "https://raw.githubusercontent.com/" + repository + "/" + commit + "/" + name
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = response.read(128 * 1024 + 1)
    if hashlib.sha256(payload).hexdigest() != expected:
        raise AssertionError("Pinned upstream source digest mismatch: " + name)
    destination = directory / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)


class UpstreamDashContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_temp = tempfile.TemporaryDirectory(prefix="sendtokodi-contract-")
        cls.addClassCleanup(cls.source_temp.cleanup)
        source = Path(cls.source_temp.name)
        for name, digest in UPSTREAM_FILES.items():
            fetch_pinned_source(source, name, digest)
        # The actual builder's XML methods and real selector run unchanged.
        # Its HTTP range function is replaced with fixed byte-range metadata.
        cls.requests = types.ModuleType("requests")
        cls.requests.get = Mock(
            side_effect=AssertionError("Unexpected media network request")
        )
        with patch.dict("sys.modules", {"requests": cls.requests}):
            cls.builder = load_path(
                "sendtokodi_contract_builder", source / "core/dash_builder.py"
            )
        cls.selection = load_path(
            "sendtokodi_contract_selection", source / "core/playback_selection.py"
        )
        resolver = load_resolver()
        for filename, digest in resolver.SMARTTUBE_FILES.items():
            fetch_pinned_source(
                source,
                "resources/lib/" + filename,
                digest,
                repository="primez-x/plugin.video.smarttube",
                commit=resolver.SMARTTUBE_COMMIT,
            )
        cls.inner = load_path(
            "smarttube_contract_innertube", source / "resources/lib/innertube.py"
        )
        cls.smart_dash = load_path(
            "smarttube_contract_dash", source / "resources/lib/dash.py"
        )
        cls.manifest = load_path(
            "ha_youtube_manifest_contract",
            ROOT / "scripts/host/ha_kodi_youtube_manifest.py",
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="youtube-contract-state-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.state = self.home / ".config/ha-host-actions/youtube"
        self.state.mkdir(parents=True)
        self.request_id = str(uuid4())
        (self.state / "current-request.json").write_text(
            json.dumps({"request_id": self.request_id})
        )
        (self.state.parent / "token").write_text("contract-private-host-token")
        self.resolver = load_resolver()
        self.adapter = load_adapter()
        self.adapter.xbmcgui.ListItem = FakeItem
        self.adapter.xbmcplugin.setResolvedUrl = Mock()

    @staticmethod
    def public_metadata_fixture():
        return {
            "id": "L_LUpnjgPso",
            "title": "Contract fireplace",
            "duration": 36000,
            "formats": [
                {
                    "format_id": "140",
                    "format": "140 - audio only",
                    "url": "https://rr1.fixture.googlevideo.com/videoplayback?id=fixture-audio",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "container": "m4a_dash",
                    "ext": "m4a",
                    "asr": 44100,
                    "audio_channels": 2,
                    "tbr": 128,
                    "abr": 128,
                    "language": "en-US",
                    "language_preference": 10,
                },
                {
                    "format_id": "137",
                    "format": "137 - 1920x1080",
                    "url": "https://rr1.fixture.googlevideo.com/videoplayback?id=fixture-video",
                    "vcodec": "avc1.640028",
                    "acodec": "none",
                    "container": "mp4_dash",
                    "ext": "mp4",
                    "width": 1920,
                    "height": 1080,
                    "fps": 30,
                    "resolution": "1920x1080",
                    "tbr": 3500,
                    "http_headers": {"User-Agent": "contract-public-player"},
                },
            ],
        }

    def prepare_sendtokodi(self, _video_id, _home, _log):
        """Keep validating the enabled SendToKodi builder through publication."""
        metadata = self.public_metadata_fixture()
        have_video, have_audio, videos, audios = self.selection.analyze_formats(
            metadata["formats"]
        )
        video = self.selection.pick_best_dash_video_format(videos, 1920)
        prepared = {
            "title": metadata["title"],
            "width": video["width"],
            "height": video["height"],
            "headers": self.resolver.safe_headers(video["http_headers"]),
        }

        def capture(payload, **_ignored):
            prepared["payload"] = payload
            return str(self.state / "requests" / self.request_id / "manifest.mpd")

        self.selection.build_dash_manifest_candidate(
            metadata["duration"],
            [video],
            audios,
            have_video,
            have_audio,
            self.builder.Manifest,
            capture,
            preferred_video_format=video,
        )
        return prepared

    def assert_prepared_delivery(self, result, title):
        payload = self.manifest.read_manifest(
            self.state, {"request_id": [self.request_id]}
        )
        tree = ET.fromstring(payload)
        kinds = {
            node.get("contentType") for node in tree.findall(".//{*}AdaptationSet")
        }
        self.assertEqual(kinds, {"audio", "video"})
        self.assertEqual(len(tree.findall(".//{*}Representation")), 2)
        self.assertEqual(len(tree.findall(".//{*}SegmentBase")), 2)
        self.adapter.resolve_item(self.state, "?request_id=" + self.request_id, 11)
        handle, succeeded, item = self.adapter.xbmcplugin.setResolvedUrl.call_args.args
        self.assertEqual((handle, succeeded), (11, True))
        self.assertEqual(item.path, result["file"])
        self.assertEqual(item.mime, "application/dash+xml")
        self.assertEqual(
            parse_qs(item.properties["inputstream.adaptive.manifest_headers"]),
            {"X-HA-Host-Token": ["contract-private-host-token"]},
        )
        self.assertNotIn(
            "contract-private-host-token",
            item.properties["inputstream.adaptive.stream_headers"],
        )
        item.info.setTitle.assert_called_once_with(title)
        return payload

    def test_real_sendtokodi_output_crosses_publication_reader_and_adapter(self):
        with (
            patch.object(
                self.resolver, "prepare_smarttube", side_effect=self.prepare_sendtokodi
            ),
            patch.object(
                self.builder,
                "find_init_and_index_ranges",
                return_value=((0, 735), (736, 1067)),
            ) as ranges,
        ):
            result = self.resolver.resolve(
                "L_LUpnjgPso", self.request_id, self.home, Mock()
            )
        self.assertEqual(ranges.call_count, 2)
        self.requests.get.assert_not_called()
        payload = self.assert_prepared_delivery(result, "Contract fireplace")
        print(
            "SendToKodi v0.10.3 contract commit="
            + UPSTREAM_COMMIT
            + " manifest_sha256="
            + hashlib.sha256(payload).hexdigest()
        )

    def test_real_smarttube_source_crosses_resolver_reader_and_adapter(self):
        raw = json.dumps(public_player_response()).encode()
        with (
            patch.object(
                self.resolver,
                "smarttube_modules",
                return_value=(self.inner, self.smart_dash),
            ),
            patch.object(
                self.resolver, "urlopen", return_value=io.BytesIO(raw)
            ) as transport,
        ):
            result = self.resolver.resolve(VIDEO_ID, self.request_id, self.home, Mock())
        self.assertEqual(transport.call_count, 1)
        payload = self.assert_prepared_delivery(result, "Fast public fireplace")
        print(
            "SmartTube v0.5.3 contract commit="
            + self.resolver.SMARTTUBE_COMMIT
            + " manifest_sha256="
            + hashlib.sha256(payload).hexdigest()
            + " bytes="
            + str(len(payload))
        )

    def test_real_smarttube_live_flags_reject_publication(self):
        for flag in ("isLive", "isLiveNow"):
            with self.subTest(flag=flag):
                response = public_player_response()
                if flag == "isLive":
                    response["videoDetails"][flag] = True
                else:
                    response["microformat"] = {
                        "playerMicroformatRenderer": {
                            "liveBroadcastDetails": {flag: True}
                        }
                    }
                raw = json.dumps(response).encode()
                with (
                    patch.object(
                        self.resolver,
                        "smarttube_modules",
                        return_value=(self.inner, self.smart_dash),
                    ),
                    patch.object(
                        self.resolver,
                        "urlopen",
                        side_effect=lambda *_args, **_kwargs: io.BytesIO(raw),
                    ),
                    self.assertRaisesRegex(
                        self.resolver.ResolveError, "hd_audio_video_unavailable"
                    ),
                ):
                    self.resolver.resolve(VIDEO_ID, self.request_id, self.home, Mock())
                self.assertFalse((self.state / "requests" / self.request_id).exists())
