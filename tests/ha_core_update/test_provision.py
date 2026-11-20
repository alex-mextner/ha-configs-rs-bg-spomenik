# Copyright (c) 2026 Alex Ultra.
"""Exercise restore, clean provisioning and fail-closed reconciliation without HA."""

import copy
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from email.message import Message
from http.client import IncompleteRead, RemoteDisconnected
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/provision_ha_core_update.py"
if not SCRIPT.exists():
    SCRIPT = Path(__file__).with_name("provision_ha_core_update.py")
spec = importlib.util.spec_from_file_location("provision_notice", SCRIPT)
if spec is None or spec.loader is None:
    message = "Cannot load the provisioner for tests"
    raise ImportError(message)
notice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notice)


class FakeHA:
    """Model native flow saves and independent live metadata."""

    def __init__(self, config: Path) -> None:
        self.config = config
        self.entries: list[dict[str, Any]] = []
        self.registry: list[dict[str, Any]] = []
        self.posts: list[tuple[str, Any]] = []
        self.current = "2026.9.3"
        self.latest = "2026.11.2"
        self.pending: dict[str, Any] | None = None
        self.stale = False
        self.save()

    def save(self) -> None:
        storage = self.config / ".storage"
        storage.mkdir(exist_ok=True)
        (storage / "core.config_entries").write_text(
            json.dumps({"data": {"entries": self.entries}})
        )
        (storage / "core.entity_registry").write_text(
            json.dumps({"data": {"entities": self.registry}})
        )

    def add(self, domain: str, data: Any = None, options: Any = None) -> Any:
        entry_id = str(len(self.entries) + 1)
        self.entries.append(
            {
                "entry_id": entry_id,
                "domain": domain,
                "data": data or {},
                "options": options or {},
                "disabled_by": None,
                "modified_at": "2026-11-20T08:00:00+00:00",
            }
        )
        entity = (
            "update.home_assistant_core"
            if domain == "template"
            else "sensor." + data["source"]
        )
        self.registry.append(
            {"entity_id": entity, "config_entry_id": entry_id, "disabled_by": None}
        )
        self.save()
        return {"type": "create_entry", "result": {"entry_id": entry_id}}

    def get(self, path: str) -> Any:
        if path == notice.ENTRIES:
            metadata = [
                {
                    **entry,
                    "modified_at": datetime.fromisoformat(
                        entry["modified_at"]
                    ).timestamp(),
                }
                for entry in self.entries
            ]
            return metadata + (
                [{"domain": "template", "entry_id": "unsaved", "modified_at": 0}]
                if self.stale
                else []
            )
        if path.startswith("states/sensor."):
            entity = path.removeprefix("states/")
            row = next((r for r in self.registry if r["entity_id"] == entity), None)
            if row is None:
                raise AssertionError(path)
            entry = next(
                e for e in self.entries if e["entry_id"] == row["config_entry_id"]
            )
            return {
                "state": self.current
                if entry["data"]["source"] == "local"
                else self.latest
            }
        if path.startswith("states/update."):
            helper = next(e for e in self.entries if e["domain"] == "template")
            return {
                "state": "on",
                "attributes": {
                    "release_summary": helper["options"].get("release_summary"),
                    "installed_version": self.current,
                    "latest_version": self.latest,
                    "release_url": f"https://github.com/home-assistant/core/releases/tag/{self.latest}",
                    "supported_features": 0,
                    "auto_update": False,
                },
            }
        raise AssertionError(path)

    def request(self, method: str, path: str, data: Any = None) -> Any:
        if method == "GET":
            return self.get(path)
        if method != "POST":
            raise AssertionError(method)
        self.posts.append((path, data))
        if path == notice.FLOW:
            self.pending = {"handler": data["handler"]}
            return {"flow_id": "flow", "step_id": "user", "type": "form"}
        if path != f"{notice.FLOW}/flow" or self.pending is None:
            raise AssertionError(path)
        if self.pending["handler"] == "version":
            return self.version_flow(data)
        if "next_step_id" in data:
            return {"type": "form", "step_id": "update", "flow_id": "flow"}
        return self.add("template", options={"template_type": "update", **data})

    def version_flow(self, data: dict[str, Any]) -> Any:
        if data.get("version_source") == "Local installation":
            return self.add("version", {"source": "local"})
        if "version_source" in data:
            return {"type": "form", "step_id": "version_source", "flow_id": "flow"}
        return self.add(
            "version",
            {
                "source": "supervisor",
                "channel": data["channel"].lower(),
                "image": data["image"],
            },
        )


class ProvisionTests(unittest.TestCase):
    """Guard restored configuration and mutation-free repeat provisioning."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name)
        self.ha = FakeHA(self.config)

    def provision(self) -> Any:
        return notice.reconcile(self.ha, self.config, wait=0)

    def restored(self) -> None:
        self.ha.add("version", {"source": "local"})
        self.ha.add(
            "version", {"source": "supervisor", "channel": "stable", "image": "default"}
        )
        self.ha.add(
            "template",
            options={
                "name": notice.TITLE,
                "template_type": "update",
                **notice.desired_options("sensor.local", "sensor.supervisor"),
            },
        )

    def test_clean_install_then_immediate_repeat_has_no_writes_or_duplicates(
        self,
    ) -> None:
        self.assertTrue(self.provision()["changed"])
        self.assertEqual(len(self.ha.entries), 3)
        self.ha.posts.clear()
        self.ha.current = "2026.11.2"
        self.assertFalse(self.provision()["changed"])
        self.assertEqual(self.ha.posts, [])
        helper = self.ha.entries[-1]["options"]
        self.assertEqual(helper["installed_version"], "{{ states('sensor.local') }}")
        self.assertNotIn("install", helper)

    def test_restored_storage_reuses_entities_without_post(self) -> None:
        self.restored()
        self.assertEqual(self.provision()["entity_id"], "update.home_assistant_core")
        self.assertEqual(self.ha.posts, [])

    def test_changed_reminder_is_reported_without_mutating_existing_helper(
        self,
    ) -> None:
        self.restored()
        helper = self.ha.entries[-1]["options"]
        helper["name"] = "My HA"
        helper["release_summary"] = "Older reminder"
        self.ha.save()
        before = copy.deepcopy(helper)
        with self.assertRaisesRegex(notice.ProvisionError, "options differ"):
            self.provision()
        self.assertEqual(helper, before)
        self.assertEqual(self.ha.posts, [])

    def test_source_created_during_previous_source_flow_is_reused(self) -> None:
        original_add = self.ha.add

        def add(domain, data=None, options=None) -> Any:
            result = original_add(domain, data, options)
            if domain == "version" and data is not None and data["source"] == "local":
                original_add(
                    "version",
                    {"source": "supervisor", "channel": "stable", "image": "default"},
                )
            return result

        with patch.object(self.ha, "add", side_effect=add):
            self.assertTrue(self.provision()["changed"])
        self.assertEqual(len(self.ha.entries), 3)
        self.assertEqual(
            sum(e["data"].get("source") == "supervisor" for e in self.ha.entries), 1
        )

    def test_source_renamed_while_waiting_is_resolved_before_helper_creation(
        self,
    ) -> None:
        self.ha.add("version", {"source": "local"})
        self.ha.add(
            "version", {"source": "supervisor", "channel": "stable", "image": "default"}
        )
        original_get = self.ha.get

        def get(path: str) -> Any:
            if path == "states/sensor.supervisor":
                self.ha.registry[0]["entity_id"] = "sensor.renamed_local"
                self.ha.save()
            return original_get(path)

        with patch.object(self.ha, "get", side_effect=get):
            self.assertTrue(self.provision()["changed"])
        self.assertEqual(
            self.ha.entries[-1]["options"]["installed_version"],
            "{{ states('sensor.renamed_local') }}",
        )
        self.assertEqual(len(self.ha.entries), 3)

    def test_verification_rechecks_source_settings_and_registry(self) -> None:
        self.restored()
        original_verify = notice.verify_notice
        original_entries = copy.deepcopy(self.ha.entries)
        original_registry = copy.deepcopy(self.ha.registry)
        changes = (
            ("channel", "beta"),
            ("image", "other"),
            ("entry_disabled", "user"),
            ("entity_disabled", "user"),
            ("entity_id", "sensor.renamed_stable"),
        )
        for field, value in changes:
            with self.subTest(field=field):
                self.ha.entries = copy.deepcopy(original_entries)
                self.ha.registry = copy.deepcopy(original_registry)
                self.ha.save()

                def verify(*args: Any, field=field, value=value, **kwargs: Any) -> Any:
                    if field in {"channel", "image"}:
                        self.ha.entries[1]["data"][field] = value
                    elif field == "entry_disabled":
                        self.ha.entries[1]["disabled_by"] = value
                    elif field == "entity_disabled":
                        self.ha.registry[1]["disabled_by"] = value
                    else:
                        self.ha.registry[1]["entity_id"] = value
                    self.ha.save()
                    return original_verify(*args, **kwargs)

                with (
                    patch.object(notice, "verify_notice", side_effect=verify),
                    self.assertRaises(notice.ProvisionError),
                ):
                    notice.reconcile(self.ha, self.config, check=True, wait=0)
        self.assertEqual(self.ha.posts, [])

    def test_readiness_retries_a_missing_source_or_notice_state(self) -> None:
        self.restored()
        original_get = self.ha.get
        for target in ("states/sensor.local", "states/update.home_assistant_core"):
            with self.subTest(target=target):
                attempts: list[str] = []

                def get(path: str, target=target, attempts=attempts) -> Any:
                    if path == target:
                        attempts.append(path)
                        if len(attempts) == 1:
                            api = notice.API("test-token")
                            error = HTTPError(
                                "http://127.0.0.1", 404, "Not Found", Message(), None
                            )
                            with (
                                error,
                                patch.object(api.opener, "open", side_effect=error),
                            ):
                                return api.request("GET", path)
                    return original_get(path)

                with (
                    patch.object(self.ha, "get", side_effect=get),
                    patch.object(notice.time, "sleep"),
                ):
                    result = notice.reconcile(self.ha, self.config, check=True, wait=1)
                self.assertFalse(result["changed"])
                self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual(self.ha.posts, [])

    def test_disconnects_are_retryable_only_for_get(self) -> None:
        for error in (
            RemoteDisconnected("response disconnected"),
            ConnectionResetError("connection reset"),
        ):
            for method in ("GET", "POST"):
                with self.subTest(error=type(error).__name__, method=method):
                    api = notice.API("test-token")
                    expected = (
                        notice.APIUnavailableError
                        if method == "GET"
                        else notice.ProvisionError
                    )
                    with (
                        patch.object(api.opener, "open", side_effect=error) as request,
                        self.assertRaises(expected) as caught,
                    ):
                        api.request(method, "states/sensor.local")
                    self.assertEqual(request.call_count, 1)
                    self.assertNotIn("private response fragment", str(caught.exception))
                    if method == "POST":
                        self.assertNotIsInstance(
                            caught.exception, notice.APIUnavailableError
                        )

    def test_body_disconnects_are_sanitized_without_retrying_post(self) -> None:
        for error in (IncompleteRead(b"private fragment", 100), ConnectionResetError()):
            for method in ("GET", "POST"):
                with self.subTest(error=type(error).__name__, method=method):
                    api = notice.API("test-token")
                    response = MagicMock()
                    response.__enter__.return_value = response
                    response.read.side_effect = error
                    expected = (
                        notice.APIUnavailableError
                        if method == "GET"
                        else notice.ProvisionError
                    )
                    with (
                        patch.object(
                            api.opener, "open", return_value=response
                        ) as request,
                        self.assertRaises(expected) as caught,
                    ):
                        api.request(method, "states/sensor.local")
                    response.read.assert_called_once_with()
                    self.assertEqual(request.call_count, 1)
                    self.assertNotIn("private fragment", str(caught.exception))
                    if method == "POST":
                        self.assertNotIsInstance(
                            caught.exception, notice.APIUnavailableError
                        )

    def test_unsafe_log_directory_fails_with_a_sanitized_message(self) -> None:
        log_dir = self.config / "ha-core-update-notice"
        log_dir.mkdir(mode=0o700)
        log_dir.chmod(0o755)
        stderr = StringIO()
        with (
            patch.object(notice.tempfile, "gettempdir", return_value=str(self.config)),
            patch("sys.argv", ["provision", "--check"]),
            patch("sys.stderr", stderr),
        ):
            self.assertEqual(notice.main(), 1)
        self.assertIn("Provisioning failed", stderr.getvalue())
        self.assertEqual(list(log_dir.iterdir()), [])
        self.assertEqual(self.ha.posts, [])

    def test_readiness_disconnect_stops_at_the_deadline(self) -> None:
        self.restored()
        original_get = self.ha.get
        attempts: list[str] = []

        def get(path: str) -> Any:
            if path == "states/sensor.local":
                attempts.append(path)
                api = notice.API("test-token")
                with patch.object(
                    api.opener, "open", side_effect=RemoteDisconnected("disconnected")
                ):
                    return api.request("GET", path)
            return original_get(path)

        with (
            patch.object(self.ha, "get", side_effect=get),
            patch.object(notice.time, "sleep") as sleep,
            self.assertRaisesRegex(notice.ProvisionError, "not yet saved"),
        ):
            notice.reconcile(self.ha, self.config, check=True, wait=0)
        self.assertEqual(len(attempts), 1)
        sleep.assert_not_called()
        self.assertEqual(self.ha.posts, [])

    def test_metadata_auth_failure_is_reported_without_readiness_retry(self) -> None:
        self.restored()
        for status in (401, 403):
            with self.subTest(status=status):
                api = notice.API("test-token")
                error = HTTPError(
                    "http://127.0.0.1", status, "Forbidden", Message(), None
                )
                with (
                    error,
                    patch.object(api.opener, "open", side_effect=error) as request,
                    self.assertRaisesRegex(notice.ProvisionError, f"HTTP {status}"),
                ):
                    notice.snapshot(api, self.config, wait=0)
                self.assertEqual(request.call_count, 1)

    def test_ambiguous_helper_is_not_changed(self) -> None:
        self.restored()
        self.ha.add("template", options=dict(self.ha.entries[-1]["options"]))
        with self.assertRaisesRegex(notice.ProvisionError, "Ambiguous"):
            self.provision()
        self.assertEqual(self.ha.posts, [])

    def test_unsaved_live_entry_prevents_duplicate_creation(self) -> None:
        self.ha.stale = True
        with self.assertRaisesRegex(notice.ProvisionError, "not ready/in sync"):
            self.provision()
        self.assertEqual(self.ha.posts, [])

    def test_existing_install_action_is_not_overwritten(self) -> None:
        self.restored()
        self.ha.entries[-1]["options"]["install"] = [{"action": "script.user_owned"}]
        self.ha.save()
        with self.assertRaisesRegex(notice.ProvisionError, "installs updates"):
            self.provision()
        self.assertEqual(self.ha.posts, [])

    def test_unsaved_options_with_same_ids_cannot_overwrite_live_changes(self) -> None:
        self.restored()
        helper = self.ha.entries[-1]
        helper["options"]["device_id"] = "user-selected-device"
        helper["modified_at"] = "2026-11-20T08:00:01+00:00"
        # Deliberately do not save: HA has newer options than the atomic disk snapshot.
        with self.assertRaises(notice.ProvisionError):
            self.provision()
        self.assertEqual(self.ha.posts, [])
        self.assertEqual(helper["options"]["device_id"], "user-selected-device")

    def test_matching_helper_preserves_all_unrelated_options_without_a_flow(
        self,
    ) -> None:
        self.restored()
        helper = self.ha.entries[-1]["options"]
        helper["device_id"] = "user-selected-device"
        helper["in_progress"] = "{{ false }}"
        helper["device_class"] = "firmware"
        self.ha.save()
        before = copy.deepcopy(helper)
        self.assertFalse(self.provision()["changed"])
        self.assertEqual(helper, before)
        self.assertEqual(self.ha.posts, [])

    def test_drift_refuses_before_opening_a_stale_options_flow(self) -> None:
        self.restored()
        helper = self.ha.entries[-1]["options"]
        helper["device_id"] = "user-selected-device"
        helper["in_progress"] = "{{ false }}"
        helper["device_class"] = "firmware"
        helper["release_summary"] = "Old reminder"
        self.ha.save()
        before = copy.deepcopy(helper)
        with self.assertRaisesRegex(notice.ProvisionError, "options differ"):
            self.provision()
        self.assertEqual(helper, before)
        self.assertEqual(self.ha.posts, [])

    def test_check_refuses_an_unavailable_notice(self) -> None:
        self.restored()
        original_get = self.ha.get

        def get(path: str) -> Any:
            result = original_get(path)
            if path.startswith("states/update."):
                result["state"] = "unavailable"
            return result

        with (
            patch.object(self.ha, "get", side_effect=get),
            self.assertRaises(notice.ProvisionError),
        ):
            notice.reconcile(self.ha, self.config, check=True, wait=0)
        self.assertEqual(self.ha.posts, [])

    def test_check_refuses_stale_rendered_versions_and_release_link(self) -> None:
        self.restored()
        original_get = self.ha.get
        stale = {
            "installed_version": "2026.8.0",
            "latest_version": "2026.10.0",
            "release_url": "https://github.com/home-assistant/core/releases/tag/2026.10.0",
        }
        for key, value in stale.items():
            with self.subTest(attribute=key):

                def get(path: str, key=key, value=value) -> Any:
                    result = original_get(path)
                    if path.startswith("states/update."):
                        result["attributes"][key] = value
                    return result

                with (
                    patch.object(self.ha, "get", side_effect=get),
                    self.assertRaises(notice.ProvisionError),
                ):
                    notice.reconcile(self.ha, self.config, check=True, wait=0)
        self.assertEqual(self.ha.posts, [])

    def test_check_waits_for_valid_source_versions(self) -> None:
        self.restored()
        self.ha.latest = "unknown"
        with self.assertRaises(notice.ProvisionError):
            notice.reconcile(self.ha, self.config, check=True, wait=0)
        self.assertEqual(self.ha.posts, [])

    def test_check_does_not_create_missing_entries(self) -> None:
        with self.assertRaises(notice.ProvisionError):
            notice.reconcile(self.ha, self.config, check=True, wait=0)
        self.assertEqual(self.ha.posts, [])

    def test_full_reminder_fits_update_entity_limit(self) -> None:
        self.assertLessEqual(len(notice.SUMMARY), 255)
        self.assertEqual(
            [line[:2] for line in notice.SUMMARY.splitlines()], ["1.", "2.", "3.", "4."]
        )


if __name__ == "__main__":
    unittest.main()
