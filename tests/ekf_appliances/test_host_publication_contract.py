"""Behavior contracts for the publication-only host helper extraction."""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "published_host", ROOT / "scripts/host/ha-host-actions.py"
)
HOST = importlib.util.module_from_spec(SPEC)
with patch.object(sys, "path", [str(ROOT / "scripts/host"), *sys.path]):
    SPEC.loader.exec_module(HOST)


class HostPublicationContracts(unittest.TestCase):
    """Verify extracted helper and copied-bundle behavior without host actions."""

    def test_transmission_retries_only_after_a_valid_session_challenge(self) -> None:
        requests = []

        def open_request(request, timeout) -> io.BytesIO:
            requests.append(request)
            self.assertEqual(timeout, 7)
            if len(requests) == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    409,
                    "session",
                    {"X-Transmission-Session-Id": "fixture-session"},
                    None,
                )
            return io.BytesIO(b'{"result":"success","arguments":{"ok":true}}')

        with patch.object(HOST.urllib.request, "urlopen", side_effect=open_request):
            result = HOST.transmission_rpc(
                "torrent-get", {"fields": ["name"]}, timeout=7
            )
        self.assertTrue(result["arguments"]["ok"])
        self.assertEqual(
            requests[1].get_header("X-transmission-session-id"), "fixture-session"
        )
        self.assertEqual(
            json.loads(requests[1].data),
            {"method": "torrent-get", "arguments": {"fields": ["name"]}},
        )
        failure = urllib.error.HTTPError("http://127.0.0.1", 409, "session", {}, None)
        with (
            patch.object(HOST.urllib.request, "urlopen", side_effect=failure) as opener,
            self.assertRaises(urllib.error.HTTPError),
        ):
            HOST.transmission_rpc("torrent-get")
        self.assertEqual(opener.call_count, 1)

    def test_completed_files_preserve_stats_fallback_and_unwanted_filter(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in (
                "complete.mkv",
                "fallback.mp4",
                "unwanted.mkv",
                "incomplete.mkv",
                "unfinished.mkv.part",
            ):
                (root / name).write_bytes(b"video")
            torrent = {
                "downloadDir": folder,
                "files": [
                    {"name": "complete.mkv", "length": 5, "bytesCompleted": 5},
                    {"name": "fallback.mp4", "length": 5, "bytesCompleted": 5},
                    {"name": "unwanted.mkv", "length": 5, "bytesCompleted": 5},
                    {"name": "incomplete.mkv", "length": 5, "bytesCompleted": 4},
                    {"name": "unfinished.mkv.part", "length": 5, "bytesCompleted": 5},
                    {"name": "missing.mkv", "length": 5, "bytesCompleted": 5},
                    None,
                ],
                "fileStats": [
                    {"bytesCompleted": 5},
                    {"bytesCompleted": 0},
                    {"wanted": False},
                ],
            }
            with patch.object(HOST, "MIN_VIDEO_SIZE", 1):
                self.assertEqual(
                    HOST.completed_video_files_for_torrent(torrent),
                    [root / "complete.mkv", root / "fallback.mp4"],
                )
                self.assertEqual(
                    HOST.completed_video_files_for_torrent({"files": "invalid"}), []
                )

    def test_media_type_keeps_override_labels_and_episode_count_boundaries(
        self,
    ) -> None:
        root = Path("/fixture/library")
        cases = [
            ([root / "S01E01.mkv"], {"media_type": "movies"}, {}, "movies"),
            ([root / "feature.mkv"], {}, {"labels": ["series"]}, "series"),
            ([root / "episode.S01E01.mkv"], {}, {}, "series"),
            ([root / f"video-{n}.mkv" for n in range(5)], {}, {}, "movies"),
            ([root / f"video-{n}.mkv" for n in range(6)], {}, {}, "series"),
        ]
        for candidates, payload, torrent, expected in cases:
            with self.subTest(expected=expected, candidates=candidates):
                self.assertEqual(
                    HOST.detect_media_type(root, candidates, payload, torrent), expected
                )

    def test_sources_xml_preserves_unrelated_entries_and_first_backup(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sources = root / "sources.xml"
            original = '<mediasources><video><source><name>Owner</name><path>/owner/</path></source><source><name>HA Series</name><path pathversion="1">/old/</path></source></video></mediasources>'
            sources.write_text(original)
            with patch.multiple(
                HOST,
                KODI_USERDATA_DIR=root,
                KODI_SERIES_DIR=root / "Series",
                KODI_MOVIES_DIR=root / "Movies",
            ):
                self.assertTrue(HOST.ensure_kodi_sources_xml()["changed"])
                self.assertFalse(HOST.ensure_kodi_sources_xml()["changed"])
            self.assertEqual(
                sources.with_suffix(".xml.ha-host-actions.bak").read_text(), original
            )
            # Parse only XML written by this local fixture and the helper under test.
            tree = ET.parse(sources)  # noqa: S314
            entries = {
                source.findtext("name"): source.findtext("path")
                for source in tree.findall("./video/source")
            }
            self.assertEqual(
                entries,
                {
                    "Owner": "/owner/",
                    "HA Series": str(root / "Series") + "/",
                    "HA Movies": str(root / "Movies") + "/",
                },
            )

    def test_sync_keeps_partial_failure_and_scans_only_changed_roots(self) -> None:
        root = Path("/fixture")
        torrent = {"name": "Example", "downloadDir": str(root)}
        candidates = [root / "first.mkv", root / "second.mkv"]
        setup = {"sources_xml": {"changed": False}, "video_db": {"changed": False}}
        with (
            patch.multiple(
                HOST,
                KODI_LIBRARY_DIR=root / "Library",
                KODI_SERIES_DIR=root / "Library/Series",
                KODI_MOVIES_DIR=root / "Library/Movies",
            ),
            patch.object(HOST, "ensure_kodi_library_setup", return_value=setup),
            patch.object(HOST, "transmission_torrents", return_value=[torrent]),
            patch.object(
                HOST, "completed_video_files_for_torrent", return_value=candidates
            ),
            patch.object(
                HOST,
                "ensure_symlink",
                side_effect=[OSError("fixture failure"), "created"],
            ),
            patch.object(HOST, "kodi_running", return_value=True),
            patch.object(HOST, "wait_for_kodi_jsonrpc"),
            patch.object(HOST, "kodi_jsonrpc", return_value={"ok": True}) as rpc,
        ):
            result = HOST.sync_completed_files_to_kodi()
        self.assertEqual((result["linked_count"], result["known_count"]), (1, 1))
        self.assertEqual(
            result["skipped"],
            [{"source": str(candidates[0]), "error": "OSError('fixture failure')"}],
        )
        self.assertEqual(result["linked"][0]["source"], str(candidates[1]))
        self.assertEqual(
            rpc.call_args.args,
            ("VideoLibrary.Scan", {"directory": str(root / "Library/Movies") + "/"}),
        )

    def test_network_inventory_preserves_filter_and_favicon_contract(self) -> None:
        data = {"devices": [{"services": [{"favicon_data_url": "image-data"}]}]}
        process = subprocess.CompletedProcess([], 0, json.dumps(data), "")
        with patch.object(HOST, "router_cmd", return_value=process) as router:
            result = HOST.network_inventory(
                {"filter": ["active"], "favicons": "0", "since": "1d"}
            )
        self.assertEqual(
            router.call_args.args[0],
            [
                "inventory",
                "list",
                "--json",
                "--filter",
                "active",
                "--since",
                "1d",
                "--no-favicons",
            ],
        )
        self.assertIsNone(result["devices"][0]["services"][0]["favicon_data_url"])
        with patch.object(HOST, "router_cmd") as router:
            with self.assertRaises(HOST.InvalidInput):
                HOST.network_inventory({"filter": "invalid"})
            router.assert_not_called()

    def test_copied_bridge_bundle_loads_all_registered_remote_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory)
            for name in (
                "ha-host-actions.py",
                "ha_host_media.py",
                "ha_host_audit.py",
                "ha_host_fireplace.py",
                "ha_kodi_fireplace.py",
                "ha_kodi_fireplace_rotation.py",
                "ha_kodi_youtube_manifest.py",
                "ha_kodi_youtube_resolve.py",
                "kodi_fireplaces.json",
            ):
                shutil.copyfile(ROOT / "scripts/host" / name, installed / name)
            probe = (
                "import json, runpy; "
                "module = runpy.run_path('ha-host-actions.py', run_name='installed_contract'); "
                "print(json.dumps(sorted({path for method, path in module['ROUTES'] "
                "if path in {'/kodi/status', '/kodi/text-input', '/kodi/keyboard', "
                "'/kodi/fireplace', '/kodi/youtube/manifest'}})))"
            )
            # Run the current Python executable with the fixed copied-bundle probe.
            result = subprocess.run(  # noqa: S603
                [sys.executable, "-c", probe],
                cwd=installed,
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            self.assertEqual(
                json.loads(result.stdout),
                [
                    "/kodi/fireplace",
                    "/kodi/keyboard",
                    "/kodi/status",
                    "/kodi/text-input",
                    "/kodi/youtube/manifest",
                ],
            )
