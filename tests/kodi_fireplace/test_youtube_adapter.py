"""Kodi adapter ownership, payload integrity, and header isolation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from urllib.parse import parse_qs
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/host/kodi/plugin.video.hayoutube/default.py"


def load_adapter() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ha_youtube_adapter_under_test", SOURCE
    )
    module = importlib.util.module_from_spec(spec)
    replacements = {
        name: types.ModuleType(name)
        for name in ("xbmc", "xbmcgui", "xbmcplugin", "xbmcvfs")
    }
    with patch.dict("sys.modules", replacements):
        spec.loader.exec_module(module)
    return module


class FakeItem:
    """Record ListItem properties and call a supplied property callback."""

    def __init__(self, label="", path="", callback=None) -> None:
        self.label = label
        self.path = path
        self.properties = {}
        self.info = Mock()
        self.callback = callback

    def getVideoInfoTag(self) -> Any:  # noqa: N802 - mirror the native Kodi API
        return self.info

    def setProperty(self, key, value) -> None:  # noqa: N802 - mirror the native Kodi API
        self.properties[key] = value
        if self.callback:
            self.callback(key)

    def setMimeType(self, value) -> None:  # noqa: N802 - mirror the native Kodi API
        self.mime = value

    def setContentLookup(self, value) -> None:  # noqa: N802 - mirror the native Kodi API
        self.content_lookup = value


class AdapterTests(unittest.TestCase):
    """Exercise AdapterTests contracts without a real Kodi process."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / ".config/ha-host-actions/youtube"
        self.state.mkdir(parents=True)
        (self.state.parent / "token").write_text("unit-test-private-host-token")
        self.request_id = str(uuid4())
        self.directory = self.state / "requests" / self.request_id
        self.directory.mkdir(parents=True)
        self.set_generation(self.request_id)
        self.manifest = self.directory / "manifest.mpd"
        self.manifest.write_bytes(
            b'<MPD><Period><AdaptationSet contentType="video"><Representation>'
            b"<BaseURL>https://rr1.example.googlevideo.com/videoplayback?token=private</BaseURL>"
            b"</Representation></AdaptationSet>"
            b'<AdaptationSet contentType="audio"><Representation>'
            b"<BaseURL>https://rr1.example.googlevideo.com/audio?token=private</BaseURL>"
            b"</Representation></AdaptationSet></Period></MPD>"
        )
        self.module = load_adapter()
        self.descriptor = {
            "version": 1,
            "request_id": self.request_id,
            "video_id": "L_LUpnjgPso",
            "title": "Test fireplace",
            "file": self.module.MANIFEST_BASE_URL + "?request_id=" + self.request_id,
            "manifest_file": str(self.manifest),
            "manifest_sha256": hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
            "headers": {
                "User-Agent": "public-player",
                "Referer": "https://www.youtube.com/",
            },
        }
        self.save_descriptor()
        self.query = "?request_id=" + self.request_id
        self.resolved = Mock()
        self.module.xbmcplugin.setResolvedUrl = self.resolved
        self.module.xbmcgui.ListItem = FakeItem

    def set_generation(self, value) -> None:
        (self.state / "current-request.json").write_text(
            json.dumps({"request_id": value})
        )

    def save_descriptor(self) -> None:
        (self.directory / "descriptor.json").write_text(json.dumps(self.descriptor))

    def change_manifest(self, payload) -> None:
        self.manifest.write_bytes(payload)
        self.descriptor["manifest_sha256"] = hashlib.sha256(payload).hexdigest()
        self.save_descriptor()

    def test_current_request_sets_http_identity_and_isolates_auth_headers(self) -> None:
        self.module.resolve_item(self.state, self.query, 7)
        handle, succeeded, item = self.resolved.call_args.args
        self.assertEqual((handle, succeeded), (7, True))
        self.assertEqual(item.path, self.descriptor["file"])
        self.assertEqual(item.mime, "application/dash+xml")
        self.assertFalse(item.content_lookup)
        manifest_headers = parse_qs(
            item.properties["inputstream.adaptive.manifest_headers"]
        )
        stream_headers = parse_qs(
            item.properties["inputstream.adaptive.stream_headers"]
        )
        self.assertEqual(
            manifest_headers, {"X-HA-Host-Token": ["unit-test-private-host-token"]}
        )
        self.assertEqual(stream_headers["User-Agent"], ["public-player"])
        self.assertNotIn("X-HA-Host-Token", stream_headers)
        self.assertNotIn("unit-test-private-host-token", str(stream_headers))

    def test_stale_generation_never_resolves(self) -> None:
        self.set_generation(str(uuid4()))
        with self.assertRaisesRegex(self.module.RequestError, "request_cancelled"):
            self.module.resolve_item(self.state, self.query, 7)
        self.resolved.assert_not_called()

    def test_generation_change_at_last_property_cannot_resolve(self) -> None:
        def change_generation(key) -> None:
            if key == "inputstream.adaptive.manifest_headers":
                self.set_generation(str(uuid4()))

        self.module.xbmcgui.ListItem = lambda **kw: FakeItem(
            callback=change_generation, **kw
        )
        with self.assertRaisesRegex(self.module.RequestError, "request_cancelled"):
            self.module.resolve_item(self.state, self.query, 7)
        self.resolved.assert_not_called()

    def test_changed_manifest_is_rejected(self) -> None:
        self.manifest.write_bytes(b"<MPD>changed</MPD>")
        with self.assertRaisesRegex(self.module.RequestError, "manifest_changed"):
            self.module.resolve_item(self.state, self.query, 7)
        self.resolved.assert_not_called()

    def test_malformed_uuid_and_extra_query_are_rejected(self) -> None:
        for query in (
            "?request_id=../../elsewhere",
            self.query + "&file=/tmp/elsewhere",
        ):
            with self.subTest(query=query), self.assertRaises(self.module.RequestError):
                self.module.resolve_item(self.state, query, 7)
        self.resolved.assert_not_called()

    def test_descriptor_cannot_redirect_manifest_origin(self) -> None:
        self.descriptor["file"] = "https://example.com/manifest.mpd"
        self.save_descriptor()
        with self.assertRaisesRegex(self.module.RequestError, "descriptor_invalid"):
            self.module.resolve_item(self.state, self.query, 7)
        self.resolved.assert_not_called()

    def test_host_token_in_media_headers_is_rejected(self) -> None:
        self.descriptor["headers"]["X-HA-Host-Token"] = "must-not-send"
        self.save_descriptor()
        with self.assertRaisesRegex(self.module.RequestError, "headers_invalid"):
            self.module.resolve_item(self.state, self.query, 7)
        self.resolved.assert_not_called()

    def test_linked_manifest_cannot_receive_host_token(self) -> None:
        self.change_manifest(
            b"<MPD><Location>https://example.com/manifest.mpd</Location></MPD>"
        )
        with self.assertRaisesRegex(
            self.module.RequestError, "manifest_structure_invalid"
        ):
            self.module.resolve_item(self.state, self.query, 7)
        self.resolved.assert_not_called()

    def test_media_origin_must_be_googlevideo_https(self) -> None:
        self.change_manifest(b"<MPD><BaseURL>https://example.com/media</BaseURL></MPD>")
        with self.assertRaisesRegex(self.module.RequestError, "media_origin_invalid"):
            self.module.resolve_item(self.state, self.query, 7)
        self.resolved.assert_not_called()

    def test_log_retention_preserves_recent_unknown_and_symlinked_files(self) -> None:
        logs = Path(self.temp.name) / "logs"
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
        unknown.write_text("preserve unrelated data\n")
        unknown.chmod(0o600)
        os.utime(unknown, (now - 2000000, now - 2000000))
        link = logs / (self.module.LOG_PREFIX + "20261115T000000Z-link.log")
        link.symlink_to(unknown)
        self.module.prune_logs(logs, now=now, keep=2)
        self.assertEqual([path.exists() for path in paths], [True, True, False])
        self.assertTrue(unknown.exists())
        self.assertTrue(link.is_symlink())
