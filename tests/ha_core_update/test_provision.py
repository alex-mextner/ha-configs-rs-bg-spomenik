# Copyright (c) 2026 Alex Ultra.
"""Exercise restore, clean provisioning and fail-closed reconciliation without HA."""

import copy
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
            return {"state": self.current if path.endswith("local") else self.latest}
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
