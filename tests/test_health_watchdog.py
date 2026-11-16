"""Check maintenance boundaries without real HA, network or process actions."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    """Load without the old module's filesystem logging side effect."""
    key = "health_test_" + name
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    with (
        patch.object(logging, "basicConfig"),
        patch.object(logging, "FileHandler", return_value=logging.NullHandler()),
    ):
        spec.loader.exec_module(module)
    return module


class HealthMonitorTests(unittest.TestCase):
    """Preserve health output while guarding URLs, logs and backups."""

    def setUp(self) -> None:
        """Create a private fixture and fake HA client."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.module = load_script("ha_health_monitor")
        self.monitor = object.__new__(self.module.HAMonitor)
        self.monitor.config = self.module.HAConfig(
            "http://ha.invalid", "fixture", "fixture"
        )
        self.monitor.client = Mock()
        self.monitor.client.get.return_value.status_code = 200
        self.monitor.client.get.return_value.json.return_value = []

    def test_invalid_health_urls_never_send_a_request(self) -> None:
        """Reject invalid authority, credentials and protocols before HTTP."""
        for url in (
            "file:///fixture",
            "ftp://ha.invalid",
            "http:///missing",
            "http://:fixture@ha.invalid",
            "http://ha.invalid:bad",
        ):
            with self.subTest(url=url), patch.object(self.module, "logger"):
                self.monitor.config.url = url
                self.monitor.client.get.reset_mock()
                self.assertFalse(self.monitor.is_ha_available())
                self.assertEqual(self.monitor.get_entities(), [])
                self.monitor.client.get.assert_not_called()

    def test_restart_command_waits_and_failure_contract_are_preserved(self) -> None:
        """Exercise the recovery loop using fake Docker and sleep boundaries."""
        scenarios = (
            ([False, True], True, [30, 2]),
            ([False] * 30, False, [30] + [2] * 30),
        )
        for availability, wanted, waits in scenarios:
            with (
                self.subTest(available=availability),
                patch.object(self.monitor, "is_ha_available", side_effect=availability),
                patch.object(self.module.subprocess, "run") as command,
                patch.object(self.module.time, "sleep") as sleep,
                patch.object(self.module, "logger"),
            ):
                self.assertEqual(self.monitor.restart_ha(), wanted)
                command.assert_called_once_with(
                    ["docker", "restart", "fixture"], check=True
                )
                self.assertEqual([call.args[0] for call in sleep.call_args_list], waits)
        with (
            patch.object(self.module.subprocess, "run", side_effect=OSError("fixture")),
            patch.object(self.module.time, "sleep") as sleep,
            patch.object(self.module, "logger"),
        ):
            self.assertFalse(self.monitor.restart_ha())
            sleep.assert_not_called()

    def test_unavailable_cli_keeps_exit_one_and_empty_stdout(self) -> None:
        """Failure still uses the existing process exit contract."""
        monitor = SimpleNamespace(is_ha_available=lambda: False)
        output = io.StringIO()
        with (
            patch.object(tempfile, "tempdir", str(self.folder)),
            patch.object(logging, "basicConfig"),
            patch.object(self.module, "logger"),
            patch.object(self.module, "get_ha_token", return_value="fixture"),
            patch.object(self.module, "HAMonitor", return_value=monitor),
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            self.module.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(output.getvalue(), "")

    def registry_fixture(self) -> tuple[Path, dict]:
        """Create the registry used by the actual repair method."""
        path = self.folder / "registry.json"
        data = {
            "data": {
                "entities": [
                    {"entity_id": "tts.old", "orphaned_timestamp": 1},
                    {"entity_id": "tts.keep", "config_entry_id": "valid"},
                ]
            }
        }
        path.write_text(json.dumps(data))
        return path, data

    def mapped_path(self, value: str | Path) -> Path:
        """Redirect only the legacy absolute paths to fixture files."""
        text = str(value)
        if text == "/home/ultra/homeassistant/.storage/core.entity_registry":
            return self.folder / "registry.json"
        if text.startswith("/tmp/core.entity_registry.backup."):  # noqa: S108 -- Match the old filename; all I/O maps into the private fixture.
            return self.folder / "legacy-backup"
        return Path(value)

    def test_backup_never_follows_predictable_symlink(self) -> None:
        """Prepare a complete private backup without changing an unrelated file."""
        registry, original = self.registry_fixture()
        sentinel = self.folder / "unrelated"
        sentinel.write_text("untouched")
        (self.folder / "legacy-backup").symlink_to(sentinel)

        def legacy_copy(args: list[str], **_options: object) -> SimpleNamespace:
            shutil.copyfile(args[1], args[2])
            return SimpleNamespace(returncode=0)

        with (
            patch.object(self.module, "Path", self.mapped_path),
            patch.object(self.module.subprocess, "run", legacy_copy),
            patch.object(tempfile, "tempdir", str(self.folder)),
            patch.object(self.module, "logger"),
        ):
            self.assertTrue(self.monitor.fix_orphaned_entity("tts.old"))
        self.assertEqual(sentinel.read_text(), "untouched")
        backups = list(self.folder.glob("core.entity_registry.backup.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual(json.loads(backups[0].read_text()), original)
        self.assertEqual(
            json.loads(registry.read_text())["data"]["entities"],
            [original["data"]["entities"][1]],
        )

    def test_failed_backup_does_not_change_registry(self) -> None:
        """A failed backup stops before the original registry is opened to write."""
        registry, original = self.registry_fixture()
        copy_api = SimpleNamespace(
            copyfileobj=Mock(side_effect=OSError("fixture failure"))
        )
        with (
            patch.object(self.module, "Path", self.mapped_path),
            patch.object(
                self.module.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=1),
            ),
            patch.object(self.module, "shutil", copy_api, create=True),
            patch.object(tempfile, "tempdir", str(self.folder)),
            patch.object(self.module, "logger"),
            self.assertRaises(OSError),
        ):
            self.monitor.fix_orphaned_entity("tts.old")
        self.assertEqual(json.loads(registry.read_text()), original)
        self.assertEqual(list(self.folder.glob("core.entity_registry.backup.*")), [])

    def test_private_log_keeps_created_inode(self) -> None:
        """Replacing the log pathname cannot redirect writes to another file."""
        sentinel = self.folder / "unrelated"
        sentinel.write_text("untouched")
        with (
            patch.object(tempfile, "tempdir", str(self.folder)),
            patch.object(logging, "basicConfig") as configure,
            patch.object(self.module, "logger") as logger,
        ):
            with self.module._logging_context():
                handler = configure.call_args.kwargs["handlers"][0]
                descriptor = handler.stream.fileno()
                self.assertEqual(stat.S_IMODE(os.fstat(descriptor).st_mode), 0o600)
                path = Path(f"/proc/self/fd/{descriptor}").readlink()
                self.assertEqual(path.parent, self.folder)
                path.unlink()
                path.symlink_to(sentinel)
                handler.emit(
                    logging.LogRecord(
                        "fixture", logging.INFO, "fixture", 1, "private log", (), None
                    )
                )
            self.assertEqual(sentinel.read_text(), "untouched")
            self.assertEqual(logger.info.call_count, 2)
            self.assertEqual(
                logger.info.call_args_list[0], logger.info.call_args_list[1]
            )

    def test_main_json_stdout_is_preserved(self) -> None:
        """Private logging leaves the machine-readable CLI result unchanged."""
        result = {"orphaned_removed": 0, "duplicates_found": {}, "pipeline_status": []}
        monitor = SimpleNamespace(
            is_ha_available=lambda: True,
            find_orphaned_entities=list,
            check_and_fix_tts=lambda: result,
        )
        output = io.StringIO()
        with (
            patch.object(tempfile, "tempdir", str(self.folder)),
            patch.object(logging, "basicConfig"),
            patch.object(self.module, "logger"),
            patch.object(self.module, "get_ha_token", return_value="fixture"),
            patch.object(self.module, "HAMonitor", return_value=monitor),
            contextlib.redirect_stdout(output),
        ):
            self.module.main()
        self.assertEqual(output.getvalue(), json.dumps(result, indent=2) + "\n")

    def test_httpx_redirect_and_token_policy_is_preserved(self) -> None:
        """Public probes follow redirects; authenticated entity reads stay at HA."""
        requests = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "ha.invalid":
                return httpx.Response(
                    302, headers={"Location": "https://public.invalid/"}, json=[]
                )
            return httpx.Response(200, json=[])

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            self.monitor.client = client
            self.assertTrue(self.monitor.is_ha_available())
            self.assertEqual(
                [r.url.host for r in requests], ["ha.invalid", "public.invalid"]
            )
            self.assertTrue(all("authorization" not in r.headers for r in requests))
            requests.clear()
            self.assertEqual(self.monitor.get_entities(), [])
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].headers["authorization"], "Bearer fixture")


class DataplicityTests(unittest.TestCase):
    """Guard URLs while retaining once-per-outage recovery."""

    def setUp(self) -> None:
        """Load without real configuration or network access."""
        self.module = load_script("dataplicity_watchdog")

    def test_bad_source_is_rejected_before_opening(self) -> None:
        """Every invalid source fails before any request or opener is built."""
        for url in (
            "file:///fixture",
            "ftp://ha.invalid",
            "http:///missing",
            "http://:fixture@ha.invalid",
            "http://ha.invalid:bad",
            "http://ha.invalid:0",
        ):
            with (
                self.subTest(url=url),
                patch.object(urllib.request, "urlopen") as old_open,
                patch.object(urllib.request, "build_opener") as build,
            ):
                self.assertFalse(self.module.probe(url)[0])
                old_open.assert_not_called()
                build.assert_not_called()

    def test_unsafe_redirects_are_rejected(self) -> None:
        """Redirects cannot change protocol or forward HA credentials elsewhere."""
        handler = getattr(
            self.module, "_HTTPRedirects", urllib.request.HTTPRedirectHandler
        )()
        for target in (
            "ftp://public.invalid/file",
            "file:///fixture",
            "https://:fixture@public.invalid/",
            "https://public.invalid/",
        ):
            request = urllib.request.Request(
                "http://127.0.0.1:8123/fixture",
                headers={"Authorization": "Bearer fixture"},
            )
            with (
                self.subTest(target=target),
                self.assertRaises((ValueError, urllib.error.HTTPError)) as raised,
            ):
                handler.redirect_request(request, None, 302, "Found", Message(), target)
            if isinstance(raised.exception, urllib.error.HTTPError):
                raised.exception.close()

    def test_safe_redirects_keep_existing_behavior(self) -> None:
        """Public HTTPS redirects and same-origin credentials remain available."""
        handler = getattr(
            self.module, "_HTTPRedirects", urllib.request.HTTPRedirectHandler
        )()
        public = handler.redirect_request(
            urllib.request.Request("http://public.invalid/"),
            None,
            302,
            "Found",
            Message(),
            "https://public.invalid/manifest.json",
        )
        self.assertEqual(public.full_url, "https://public.invalid/manifest.json")
        local = handler.redirect_request(
            urllib.request.Request(
                "http://127.0.0.1:8123/old", headers={"Authorization": "Bearer fixture"}
            ),
            None,
            302,
            "Found",
            Message(),
            "http://127.0.0.1:8123/new",
        )
        self.assertEqual(local.get_header("Authorization"), "Bearer fixture")

    def test_outage_reloads_once_then_resets_after_recovery(self) -> None:
        """Notification failure leaves the original outage state transitions intact."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            config, state = folder / "config", folder / "state"
            config.mkdir()
            (config / "core.config").write_text(
                json.dumps({"data": {"external_url": "https://ha.invalid"}})
            )
            (config / "core.config_entries").write_text(
                json.dumps(
                    {
                        "data": {
                            "entries": [
                                {"domain": "dataplicity", "entry_id": "fixture"}
                            ]
                        }
                    }
                )
            )
            calls = []

            def service(name: str, payload: dict) -> None:
                calls.append((name, payload))
                if name.endswith("dismiss"):
                    message = "fixture dismissal failure"
                    raise OSError(message)

            with (
                patch.object(self.module, "CONFIG", config),
                patch.object(self.module, "STATE_DIR", state),
                patch.object(self.module, "ha", service),
                patch.object(self.module.time, "time", return_value=1000),
            ):
                for result in [(False, "503", 0.25)] * 3 + [(True, "200", 0.1)]:
                    with patch.object(self.module, "probe", return_value=result):
                        self.module.main()
            self.assertEqual(
                [name for name, _ in calls],
                [
                    "homeassistant/reload_config_entry",
                    "persistent_notification/create",
                    "persistent_notification/dismiss",
                ],
            )
            self.assertEqual(
                json.loads((state / "state.json").read_text()), {"fails": 0}
            )
            records = [
                json.loads(line)
                for line in (state / "probes.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(records), 4)
            self.assertEqual(records[1]["action"], "reloaded")
            self.assertNotIn("action", records[2])
            self.assertEqual(records[3]["outage_minutes"], 0.0)


if __name__ == "__main__":
    unittest.main()
