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

    def test_absent_image_command_preserves_compose_command(self) -> None:
        image = {
            "Id": deploy.IMAGE_ID,
            "Config": {"Entrypoint": [], "Env": []},
        }
        for command in (None, [], ["--existing-argument"]):
            with self.subTest(command=command):
                config = self.config()
                config["services"]["music-assistant"]["command"] = command
                raw = self.raw()
                raw["Config"]["Cmd"] = command
                try:
                    with patch.object(
                        deploy, "run", return_value=json.dumps([image]).encode()
                    ):
                        deploy.verify_running_config(config, raw)
                except KeyError as error:
                    self.fail(f"Optional image command field was indexed: {error}")

    def test_absent_image_command_still_rejects_runtime_drift(self) -> None:
        image = {
            "Id": deploy.IMAGE_ID,
            "Config": {"Entrypoint": [], "Env": []},
        }
        config = self.config()
        config["services"]["music-assistant"]["command"] = None
        raw = self.raw()
        raw["Config"]["Cmd"] = ["--unexpected-argument"]
        try:
            with (
                patch.object(deploy, "run", return_value=json.dumps([image]).encode()),
                self.assertRaisesRegex(RuntimeError, "command differs"),
            ):
                deploy.verify_running_config(config, raw)
        except KeyError as error:
            self.fail(f"Optional image command field was indexed: {error}")

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


class RollbackTests(unittest.TestCase):
    """Keep backout available for damaged overlays and partial installations."""

    def receipt(self, status: str = "verified") -> dict:
        return {
            "source_sha": deploy.SOURCE,
            "action": "install",
            "status": status,
            "before": {"id": "original-container", "image_id": deploy.IMAGE_ID},
            "original_frontend_hashes": {"index.html": "a" * 64},
            "compose_proposed_sha256": deploy.digest(b"patched-compose"),
        }

    def test_damaged_release_does_not_prevent_normal_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(self.receipt()))
            args = SimpleNamespace(install_receipt=path)
            raw = {"Id": "replacement-container", "Image": deploy.IMAGE_ID}
            with (
                patch.object(deploy, "verify_running_config") as verify,
                patch.object(
                    deploy, "release_hashes", side_effect=RuntimeError("damaged")
                ) as release,
                patch.object(deploy, "frontend_hashes") as frontend,
            ):
                target, original, config_only = deploy.frontend_plan(
                    "rollback",
                    args,
                    b"patched-compose",
                    ({"current": True}, {"restored": True}),
                    raw,
                )
            self.assertEqual(target, self.receipt()["original_frontend_hashes"])
            self.assertIsNone(original)
            self.assertFalse(config_only)
            verify.assert_called_once_with({"current": True}, raw)
            release.assert_not_called()
            frontend.assert_not_called()

    def test_partial_install_uses_exact_original_container_and_removed_mount_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prior = self.receipt("compose_updated")
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(prior))
            args = SimpleNamespace(install_receipt=path)
            raw = {"Id": "original-container", "Image": deploy.IMAGE_ID}
            with (
                patch.object(deploy, "runtime", return_value=prior["before"]),
                patch.object(deploy, "verify_running_config") as verify,
                patch.object(deploy, "release_hashes") as release,
                patch.object(
                    deploy,
                    "frontend_hashes",
                    return_value=prior["original_frontend_hashes"],
                ),
            ):
                _, _, config_only = deploy.frontend_plan(
                    "rollback",
                    args,
                    b"patched-compose",
                    ({"current": True}, {"restored": True}),
                    raw,
                )
            self.assertTrue(config_only)
            verify.assert_called_once_with({"restored": True}, raw)
            release.assert_not_called()

    def test_partial_install_requires_the_exact_recorded_post_patch_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(self.receipt("compose_updated")))
            with self.assertRaisesRegex(
                RuntimeError, "Partial-install Compose differs"
            ):
                deploy.frontend_plan(
                    "rollback",
                    SimpleNamespace(install_receipt=path),
                    b"unreviewed-change",
                    ({}, {}),
                    {"Id": "original-container", "Image": deploy.IMAGE_ID},
                )


class FinalRecreationTests(unittest.TestCase):
    """Recheck reviewed bytes after the last Docker/configuration read."""

    def exercise(
        self,
        directory: str,
        *,
        edit_on_inspect: bool = False,
        configuration_only: bool = False,
        changed_environment: bool = False,
    ) -> tuple[dict, list]:
        compose = Path(directory) / "compose.yaml"
        compose.write_bytes(b"patched")
        config = {"services": {"music-assistant": {"image": "same-image"}}}
        receipt = {
            "state_before": IDLE,
            "before": {},
            "effective_proposed_sha256": deploy.digest(
                json.dumps(config, sort_keys=True).encode()
            ),
            "configuration_only": configuration_only,
        }
        calls = []

        def command(*args: str, **_kwargs: object) -> bytes:
            calls.append(args)
            if args[:3] == ("docker", "image", "inspect"):
                if edit_on_inspect:
                    compose.write_bytes(b"late-unreviewed-edit")
                return json.dumps([{"Id": deploy.IMAGE_ID}]).encode()
            message = "Unexpected mutating command"
            raise AssertionError(message)

        final = copy.deepcopy(config)
        if changed_environment:
            final["services"]["music-assistant"]["environment"] = {"CHANGED": "late"}
        with (
            patch.object(deploy, "COMPOSE", compose),
            patch.object(deploy, "MaintenanceLock"),
            patch.object(
                deploy, "preflight", return_value=(receipt, b"old", b"patched", {})
            ),
            patch.object(deploy, "snapshot", return_value=IDLE),
            patch.object(deploy, "inspect"),
            patch.object(deploy, "runtime", return_value={}),
            patch.object(deploy, "effective", side_effect=[config, final]),
            patch.object(deploy, "replace_exact"),
            patch.object(deploy, "run", side_effect=command),
            patch.object(deploy, "verify_after", return_value={"checked": True}),
        ):
            result = deploy.apply_change(
                SimpleNamespace(), Path(directory), lambda _: None
            )
        return result, calls

    def test_partial_install_backout_never_recreates_the_original_container(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, calls = self.exercise(directory, configuration_only=True)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][:3], ("docker", "image", "inspect"))

    def test_compose_edit_during_final_image_inspection_prevents_recreation(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(RuntimeError, "immediately before recreation"),
        ):
            self.exercise(directory, edit_on_inspect=True)
        # exercise() raises for any mutating Docker invocation.

    def test_late_environment_change_prevents_recreation(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(RuntimeError, "configuration changed before"),
        ):
            self.exercise(directory, changed_environment=True)


if __name__ == "__main__":
    unittest.main()
