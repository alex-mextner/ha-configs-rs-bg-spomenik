"""Authenticated manifest delivery and fireplace request boundary regressions."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts/host" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest = load("ha_kodi_youtube_manifest")
bridge = load("ha-host-actions")


class ManifestFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "youtube"
        self.ident = str(uuid4())
        self.directory = self.state / "requests" / self.ident
        self.directory.mkdir(parents=True)
        self.payload = b'<?xml version="1.0"?><MPD></MPD>'
        self.media = self.directory / "manifest.mpd"
        self.media.write_bytes(self.payload)
        self.descriptor = {
            "version": 1,
            "request_id": self.ident,
            "manifest_file": str(self.media),
            "manifest_sha256": hashlib.sha256(self.payload).hexdigest(),
        }
        (self.directory / "descriptor.json").write_text(json.dumps(self.descriptor))
        (self.state / "current-request.json").write_text(
            json.dumps({"request_id": self.ident})
        )
        self.query = {"request_id": [self.ident]}

    def tearDown(self):
        self.temp.cleanup()


class ManifestCases(ManifestFixture):
    def test_returns_only_the_current_verified_bytes(self):
        self.assertEqual(manifest.read_manifest(self.state, self.query), self.payload)

    def test_stale_generation_and_modified_manifest_fail_closed(self):
        self.media.write_bytes(b"changed")
        with self.assertRaisesRegex(manifest.ManifestError, "manifest_unavailable"):
            manifest.read_manifest(self.state, self.query)
        self.media.write_bytes(self.payload)
        (self.state / "current-request.json").write_text(
            json.dumps({"request_id": str(uuid4())})
        )
        with self.assertRaisesRegex(manifest.ManifestError, "request_cancelled"):
            manifest.read_manifest(self.state, self.query)

    def test_rejects_duplicate_unknown_and_noncanonical_parameters(self):
        for query in (
            {},
            {"request_id": [self.ident, self.ident]},
            {"request_id": ["../other"]},
            {"request_id": [self.ident.replace("-", "")]},
            {"request_id": [self.ident], "filename": ["/private/file"]},
        ):
            with self.subTest(query=query), self.assertRaises(manifest.ManifestError):
                manifest.read_manifest(self.state, query)

    def test_symlink_cannot_serve_an_unrelated_file(self):
        other = self.root / "private-secret"
        other.write_bytes(self.payload)
        self.media.unlink()
        self.media.symlink_to(other)
        with self.assertRaises(manifest.ManifestError):
            manifest.read_manifest(self.state, self.query)


class ManifestHttpCases(ManifestFixture):
    def setUp(self):
        super().setUp()
        self.config = patch.object(bridge, "CONFIG_DIR", self.root)
        self.config.start()
        self.token = patch.object(bridge.Handler, "token", "fixture-token")
        self.token.start()
        self.server = bridge.ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%s/kodi/youtube/manifest?request_id=%s" % (
            self.server.server_address[1],
            self.ident,
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.token.stop()
        self.config.stop()
        super().tearDown()

    def test_manifest_requires_existing_bridge_auth_and_has_no_cache(self):
        for headers in ({}, {"X-HA-Host-Token": "wrong-token"}):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                urllib.request.urlopen(
                    urllib.request.Request(self.url, headers=headers), timeout=2
                )
            self.assertEqual(failure.exception.code, 403)
        request = urllib.request.Request(
            self.url, headers={"X-HA-Host-Token": "fixture-token"}
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(response.headers["Content-Type"], "application/dash+xml")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.read(), self.payload)

    def test_cancelled_manifest_never_serves_previous_bytes(self):
        (self.state / "current-request.json").write_text(
            json.dumps({"request_id": str(uuid4())})
        )
        request = urllib.request.Request(
            self.url, headers={"X-HA-Host-Token": "fixture-token"}
        )
        with self.assertRaises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(failure.exception.code, 404)
        body = failure.exception.read().decode()
        self.assertNotIn(str(self.root), body)
        self.assertNotIn("fixture-token", body)


class FireplaceApiCases(unittest.TestCase):
    def test_invalid_action_or_request_has_no_controller_side_effect(self):
        payloads = (
            {},
            {"action": "start", "request_id": ""},
            {"action": "start", "request_id": False},
            {"action": "stop", "request_id": "valid"},
            {"action": "start", "url": "arbitrary"},
            {"action": ["start"]},
            {"action": "start", "request_id": "x" * 97},
        )
        with patch.object(bridge, "_fireplace_controller") as controller:
            for payload in payloads:
                with (
                    self.subTest(payload=payload),
                    self.assertRaises(bridge.InvalidInput),
                ):
                    bridge.kodi_fireplace(payload)
            controller.assert_not_called()

    def test_request_id_is_forwarded_without_rewrite_and_legacy_start_still_works(self):
        with patch.object(bridge, "_fireplace_controller") as factory:
            controller = factory.return_value
            controller.start.return_value = {
                "state": "playing",
                "request_id": "voice-request",
            }
            proof = bridge.kodi_fireplace(
                {"action": "start", "request_id": "voice-request"}
            )
            self.assertEqual(proof["request_id"], "voice-request")
            controller.start.assert_called_once_with("voice-request")
            bridge.kodi_fireplace({"action": "start"})
            controller.start.assert_called_with(None)
