"""Authenticated manifest delivery and fireplace request boundary regressions."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def load(name) -> Any:
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts/host" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest = load("ha_kodi_youtube_manifest")
bridge = load("ha-host-actions")


class ManifestFixture(unittest.TestCase):
    """Exercise manifest fixture behavior with deterministic native fixtures."""

    def setUp(self) -> None:
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

    def tearDown(self) -> None:
        self.temp.cleanup()


class ManifestCases(ManifestFixture):
    """Exercise manifest cases behavior with deterministic native fixtures."""

    def test_returns_only_the_current_verified_bytes(self) -> None:
        self.assertEqual(manifest.read_manifest(self.state, self.query), self.payload)

    def test_stale_generation_and_modified_manifest_fail_closed(self) -> None:
        self.media.write_bytes(b"changed")
        with self.assertRaisesRegex(manifest.ManifestError, "manifest_unavailable"):
            manifest.read_manifest(self.state, self.query)
        self.media.write_bytes(self.payload)
        (self.state / "current-request.json").write_text(
            json.dumps({"request_id": str(uuid4())})
        )
        with self.assertRaisesRegex(manifest.ManifestError, "request_cancelled"):
            manifest.read_manifest(self.state, self.query)

    def test_rejects_duplicate_unknown_and_noncanonical_parameters(self) -> None:
        for query in (
            {},
            {"request_id": [self.ident, self.ident]},
            {"request_id": ["../other"]},
            {"request_id": [self.ident.replace("-", "")]},
            {"request_id": [self.ident], "filename": ["/private/file"]},
        ):
            with self.subTest(query=query), self.assertRaises(manifest.ManifestError):
                manifest.read_manifest(self.state, query)

    def test_symlink_cannot_serve_an_unrelated_file(self) -> None:
        other = self.root / "private-secret"
        other.write_bytes(self.payload)
        self.media.unlink()
        self.media.symlink_to(other)
        with self.assertRaises(manifest.ManifestError):
            manifest.read_manifest(self.state, self.query)


class ManifestHttpCases(ManifestFixture):
    """Exercise manifest http cases behavior with deterministic native fixtures."""

    def setUp(self) -> None:
        super().setUp()
        self.config = patch.object(bridge, "CONFIG_DIR", self.root)
        self.config.start()
        self.token = patch.object(bridge.Handler, "token", "fixture-token")
        self.token.start()
        self.server = bridge.ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # Every request below targets this fixture-owned loopback HTTP server.
        port = self.server.server_address[1]
        self.url = (
            f"http://127.0.0.1:{port}/kodi/youtube/manifest?request_id={self.ident}"
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.token.stop()
        self.config.stop()
        super().tearDown()

    def test_manifest_requires_existing_bridge_auth_and_has_no_cache(self) -> None:
        for headers in ({}, {"X-HA-Host-Token": "wrong-token"}):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                urllib.request.urlopen(  # noqa: S310
                    urllib.request.Request(  # noqa: S310
                        self.url, headers=headers
                    ),
                    timeout=2,
                )
            self.assertEqual(failure.exception.code, 403)
        request = urllib.request.Request(  # noqa: S310
            self.url, headers={"X-HA-Host-Token": "fixture-token"}
        )
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
            self.assertEqual(response.headers["Content-Type"], "application/dash+xml")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.read(), self.payload)

    def test_cancelled_manifest_never_serves_previous_bytes(self) -> None:
        (self.state / "current-request.json").write_text(
            json.dumps({"request_id": str(uuid4())})
        )
        request = urllib.request.Request(  # noqa: S310
            self.url, headers={"X-HA-Host-Token": "fixture-token"}
        )
        with self.assertRaises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(request, timeout=2)  # noqa: S310
        self.assertEqual(failure.exception.code, 404)
        body = failure.exception.read().decode()
        self.assertNotIn(str(self.root), body)
        self.assertNotIn("fixture-token", body)


class FireplaceApiCases(unittest.TestCase):
    """Exercise fireplace api cases behavior with deterministic native fixtures."""

    def test_invalid_action_or_request_has_no_controller_side_effect(self) -> None:
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

    def test_request_id_is_forwarded_without_rewrite_and_legacy_start_still_works(
        self,
    ) -> None:
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
