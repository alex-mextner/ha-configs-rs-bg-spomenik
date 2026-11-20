# Copyright (c) 2026 Alex Ultra.
"""Exercise restore, clean provisioning and fail-closed reconciliation without HA."""

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/provision_ha_core_update.py"
if not SCRIPT.exists():
    SCRIPT = Path(__file__).with_name("provision_ha_core_update.py")
spec = importlib.util.spec_from_file_location("provision_notice", SCRIPT)
notice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notice)


class FakeHA:
    """Model native flow saves and independent live metadata."""

    def __init__(self, config) -> None:
        self.config = config
        self.entries = []
        self.registry = []
        self.posts = []
        self.current = "2026.9.3"
        self.latest = "2026.11.2"
        self.pending = None
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

    def add(self, domain, data=None, options=None) -> Any:
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

    def get(self, path) -> Any:
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
                    "supported_features": 0,
                    "auto_update": False,
                },
            }
        raise AssertionError(path)

    def request(self, method, path, data=None) -> Any:
        if method == "GET":
            return self.get(path)
        self.posts.append((path, data))
        if path in {notice.FLOW, notice.OPTIONS}:
            self.pending = {"endpoint": path, "handler": data["handler"]}
            schema = []
            if path == notice.OPTIONS:
                current = next(
                    entry
                    for entry in self.entries
                    if entry["entry_id"] == data["handler"]
                )
                schema = [
                    {"name": key, "description": {"suggested_value": value}}
                    for key, value in current["options"].items()
                    if key not in {"name", "template_type", "device_class"}
                ]
            self.pending["allowed_fields"] = {field["name"] for field in schema}
            return {
                "flow_id": "flow",
                "step_id": "update" if path == notice.OPTIONS else "user",
                "type": "form",
                "data_schema": schema,
            }
        if self.pending["endpoint"] == notice.OPTIONS:
            if set(data) - self.pending["allowed_fields"]:
                message = "Native options schema rejects extra keys"
                raise AssertionError(message)
            helper = next(
                e for e in self.entries if e["entry_id"] == self.pending["handler"]
            )
            # Native SchemaCommonFlowHandler removes omitted optional fields.
            kept = {
                key: value
                for key, value in helper["options"].items()
                if key in {"name", "template_type", "device_class"}
            }
            helper["options"].clear()
            helper["options"].update({**kept, **data})
            self.save()
            return {"type": "create_entry"}
        if self.pending["handler"] == "version":
            return self.version_flow(data)
        if "next_step_id" in data:
            return {"type": "form", "step_id": "update", "flow_id": "flow"}
        return self.add("template", options={"template_type": "update", **data})

    def version_flow(self, data) -> Any:
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

    def test_only_summary_changes_and_user_name_is_preserved(self) -> None:
        self.restored()
        helper = self.ha.entries[-1]["options"]
        helper["name"] = "My HA"
        helper["release_summary"] = "Older reminder"
        self.ha.save()
        self.assertTrue(self.provision()["changed"])
        self.assertEqual(helper["name"], "My HA")
        self.assertEqual(helper["release_summary"], notice.SUMMARY)
        self.assertEqual(len(self.ha.entries), 3)

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

    def test_reminder_change_preserves_unrelated_live_options(self) -> None:
        self.restored()
        helper = self.ha.entries[-1]["options"]
        helper["device_id"] = "user-selected-device"
        helper["in_progress"] = "{{ false }}"
        helper["release_summary"] = "Old reminder"
        self.ha.save()
        self.assertTrue(self.provision()["changed"])
        self.assertEqual(helper["device_id"], "user-selected-device")
        self.assertEqual(helper["in_progress"], "{{ false }}")
        self.assertEqual(helper["release_summary"], notice.SUMMARY)

    def test_existing_device_class_survives_reminder_update(self) -> None:
        self.restored()
        helper = self.ha.entries[-1]["options"]
        helper["device_class"] = "firmware"
        helper["release_summary"] = "Old reminder"
        self.ha.save()
        self.assertTrue(self.provision()["changed"])
        self.assertEqual(helper["device_class"], "firmware")
        self.assertEqual(helper["name"], notice.TITLE)
        self.assertEqual(helper["template_type"], "update")
        self.assertEqual(helper["release_summary"], notice.SUMMARY)
        submitted = self.ha.posts[-1][1]
        for field in ("name", "template_type", "device_class"):
            self.assertNotIn(field, submitted)

    def test_check_does_not_create_missing_entries(self) -> None:
        with self.assertRaises(notice.ProvisionError):
            notice.reconcile(self.ha, self.config, check=True, wait=0)
        self.assertEqual(self.ha.posts, [])

    def test_full_reminder_fits_update_entity_limit(self) -> None:
        self.assertLessEqual(len(notice.SUMMARY), 255)
        self.assertEqual(
            [line[:2] for line in notice.SUMMARY.splitlines()], ["1.", "2.", "3.", "4."]
        )
        self.assertTrue(notice.SUMMARY.endswith("основные интеграции."))


if __name__ == "__main__":
    unittest.main()
