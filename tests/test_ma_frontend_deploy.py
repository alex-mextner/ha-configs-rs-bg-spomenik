# Copyright (c) 2026 Alex Mextner.
"""Safety regressions for the isolated MA frontend deployment procedure."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from scripts import ma_frontend_deploy as deploy

BASE = 'services:\n  music-assistant:\n    image: unchanged-image\n    entrypoint: ["/existing-wrapper"]\n    volumes:\n      - /home/ultra/music-assistant:/data\n      - /home/ultra/homeassistant/docker/music-assistant/person_likes:/app/venv/lib/python3.14/site-packages/music_assistant/providers/person_likes:ro\n    network_mode: host\n  another-service:\n    image: independently-edited-image\n    environment:\n      KEEP_THIS: changed-by-another-owner\n'
IDLE = {
    "ha_started_at": "unchanged",
    "players": [{"player_id": "test-player", "available": True}],
    "queues": [
        {
            "queue_id": "test-queue",
            "state": "idle",
            "items": 1,
            "current_index": 0,
            "elapsed_time": 2,
            "current_item": "test-item",
            "next_item": None,
        }
    ],
}


class ComposePatchTests(unittest.TestCase):
    """Preserve unrelated configuration and reject concurrent writes."""

    def test_install_and_rollback_preserve_every_unrelated_byte(self) -> None:
        changed = deploy.patch_compose(BASE, "install")
        self.assertEqual(changed.replace(deploy.BLOCK, ""), BASE)
        self.assertEqual(deploy.patch_compose(changed, "rollback"), BASE)

    def test_rollback_preserves_later_independent_changes(self) -> None:
        changed = deploy.patch_compose(BASE, "install").replace(
            "independently-edited-image", "newer-independent-image"
        )
        expected = BASE.replace("independently-edited-image", "newer-independent-image")
        self.assertEqual(deploy.patch_compose(changed, "rollback"), expected)

    def test_duplicate_install_is_refused(self) -> None:
        changed = deploy.patch_compose(BASE, "install")
        with self.assertRaisesRegex(RuntimeError, "already"):
            deploy.patch_compose(changed, "install")

    def test_modified_volume_cannot_be_removed(self) -> None:
        changed = deploy.patch_compose(BASE, "install").replace(
            "        read_only: true", "        read_only: false"
        )
        with self.assertRaisesRegex(RuntimeError, "absent or changed"):
            deploy.patch_compose(changed, "rollback")

    def test_provider_anchor_in_another_service_is_not_used(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "MA service"):
            deploy.patch_compose(
                BASE.replace("  music-assistant:", "  other:"), "install"
            )

    def test_effective_config_cannot_change_another_service(self) -> None:
        before = {
            "services": {"music-assistant": {"volumes": []}, "other": {"image": "old"}}
        }
        after = copy.deepcopy(before)
        after["services"]["music-assistant"]["volumes"].append(deploy.VOLUME)
        deploy.verify_delta(before, after, "install")
        after["services"]["other"]["image"] = "new"
        with self.assertRaisesRegex(RuntimeError, "exceed"):
            deploy.verify_delta(before, after, "install")

    def test_effective_volume_must_be_read_only_and_fail_on_missing_source(
        self,
    ) -> None:
        before = {"services": {"music-assistant": {"volumes": []}}}
        for change in ({"read_only": False}, {"bind": {"create_host_path": True}}):
            with self.subTest(change=change):
                volume = {**deploy.VOLUME, **change}
                after = {"services": {"music-assistant": {"volumes": [volume]}}}
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    deploy.verify_delta(before, after, "install")

    def test_compare_and_replace_refuses_a_changed_preimage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compose.yaml"
            path.write_bytes(b"another writer")
            with self.assertRaisesRegex(RuntimeError, "changed since"):
                deploy.replace_exact(path, b"old snapshot", b"replacement")
            self.assertEqual(path.read_bytes(), b"another writer")

    def test_compare_and_replace_rechecks_after_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compose.yaml"
            path.write_bytes(b"old snapshot")
            real_mkstemp = tempfile.mkstemp

            def concurrent_writer(*args: Any, **kwargs: Any) -> tuple[int, str]:
                result = real_mkstemp(*args, **kwargs)
                path.write_bytes(b"intervening edit")
                return result

            with (
                patch.object(deploy.tempfile, "mkstemp", side_effect=concurrent_writer),
                self.assertRaisesRegex(RuntimeError, "changed before"),
            ):
                deploy.replace_exact(path, b"old snapshot", b"replacement")
            self.assertEqual(path.read_bytes(), b"intervening edit")
            self.assertEqual(list(Path(directory).iterdir()), [path])


class MaintenanceTests(unittest.TestCase):
    """Defer maintenance without stealing locks or interrupting playback."""

    def test_unknown_lock_is_never_stolen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "maintenance.lock"
            lock.write_text("another owner")
            with self.assertRaises(FileExistsError), deploy.MaintenanceLock(lock):
                self.fail("Lock acquisition must fail")
            self.assertEqual(lock.read_text(), "another owner")

    def test_only_exact_owned_lock_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "maintenance.lock"
            with deploy.MaintenanceLock(lock) as held:
                held.check()
                lock.write_text("replacement owner")
            self.assertEqual(lock.read_text(), "replacement owner")

    def test_owned_lock_is_released_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "maintenance.lock"
            with (
                self.assertRaisesRegex(RuntimeError, "failure"),
                deploy.MaintenanceLock(lock),
            ):
                msg = "failure"
                raise RuntimeError(msg)
            self.assertFalse(lock.exists())

    def test_playing_or_paused_queue_after_preflight_stops_before_file_write(
        self,
    ) -> None:
        for state in ("playing", "paused"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                changed = copy.deepcopy(IDLE)
                changed["queues"][0]["state"] = state
                receipt = {"state_before": IDLE, "before": {}}
                with (
                    patch.object(deploy, "MaintenanceLock"),
                    patch.object(
                        deploy, "preflight", return_value=(receipt, b"a", b"b", {})
                    ),
                    patch.object(deploy, "snapshot", return_value=changed),
                    patch.object(deploy, "replace_exact") as replace,
                    patch.object(deploy, "run") as run,
                    self.assertRaisesRegex(RuntimeError, "not idle"),
                ):
                    deploy.apply_change(
                        SimpleNamespace(), Path(directory), lambda _: None
                    )
                replace.assert_not_called()
                run.assert_not_called()

    def test_queue_change_after_compose_patch_prevents_recreation(self) -> None:
        changed = copy.deepcopy(IDLE)
        changed["queues"][0]["state"] = "paused"
        receipt = {
            "state_before": IDLE,
            "before": {},
            "effective_proposed_sha256": deploy.digest(b"{}"),
        }
        with tempfile.TemporaryDirectory() as directory:
            compose = Path(directory) / "compose.yaml"
            compose.write_bytes(b"b")
            with (
                patch.object(deploy, "COMPOSE", compose),
                patch.object(deploy, "MaintenanceLock"),
                patch.object(
                    deploy, "preflight", return_value=(receipt, b"a", b"b", {})
                ),
                patch.object(deploy, "snapshot", side_effect=[IDLE, changed]),
                patch.object(deploy, "inspect"),
                patch.object(deploy, "runtime", return_value={}),
                patch.object(deploy, "effective", return_value={}),
                patch.object(deploy, "replace_exact"),
                patch.object(deploy, "run") as run,
                self.assertRaisesRegex(RuntimeError, "not idle"),
            ):
                deploy.apply_change(SimpleNamespace(), Path(directory), lambda _: None)
            run.assert_not_called()
            self.assertEqual(
                json.loads((Path(directory) / "receipt.json").read_text())["status"],
                "compose_updated",
            )


class RuntimeIdentityTests(unittest.TestCase):
    """Reject mutable-tag, wrapper, and mount drift."""

    def raw(self) -> dict:
        return {
            "Image": deploy.IMAGE_ID,
            "Config": {
                "Entrypoint": ["/existing-wrapper"],
                "Cmd": [],
                "Env": [],
                "Labels": {
                    "com.docker.compose.project": "homeassistant",
                    "com.docker.compose.project.config_files": str(deploy.COMPOSE),
                },
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/test-data",
                    "Destination": "/data",
                    "RW": True,
                }
            ],
            "State": {"Running": True},
        }

    def config(self) -> dict:
        return {
            "services": {
                "music-assistant": {
                    "image": "mutable-tag",
                    "entrypoint": ["/existing-wrapper"],
                    "command": [],
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "/test-data",
                            "target": "/data",
                            "read_only": False,
                        }
                    ],
                }
            }
        }

    def test_same_tag_resolving_to_different_image_is_refused(self) -> None:
        image = {"Id": "sha256:different", "Config": {}}
        with (
            patch.object(deploy, "run", return_value=json.dumps([image]).encode()),
            self.assertRaisesRegex(RuntimeError, "image IDs differ"),
        ):
            deploy.verify_running_config(self.config(), self.raw())

    def test_existing_mount_drift_is_refused(self) -> None:
        raw = self.raw()
        raw["Mounts"][0]["Source"] = "/unexpected-data"
        image = {
            "Id": deploy.IMAGE_ID,
            "Config": {"Entrypoint": [], "Cmd": [], "Env": []},
        }
        with (
            patch.object(deploy, "run", return_value=json.dumps([image]).encode()),
            self.assertRaisesRegex(RuntimeError, "mounts differ"),
        ):
            deploy.verify_running_config(self.config(), raw)

    def test_running_wrapper_drift_is_refused(self) -> None:
        raw = self.raw()
        raw["Config"]["Entrypoint"] = ["/different-wrapper"]
        image = {
            "Id": deploy.IMAGE_ID,
            "Config": {"Entrypoint": [], "Cmd": [], "Env": []},
        }
        with (
            patch.object(deploy, "run", return_value=json.dumps([image]).encode()),
            self.assertRaisesRegex(RuntimeError, "entrypoint differs"),
        ):
            deploy.verify_running_config(self.config(), raw)


if __name__ == "__main__":
    unittest.main()
