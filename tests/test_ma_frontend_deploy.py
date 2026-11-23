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
    "players": [
        {
            "player_id": "test-player",
            "available": True,
            "playback_state": "idle",
            "active_source": "test-queue",
        }
    ],
    "queues": [
        {
            "queue_id": "test-queue",
            "state": "idle",
            "items": 1,
            "current_index": 0,
            "elapsed_time": 2,
            "current_item": "test-item",
            "next_item": None,
            "queue_item_ids": ["test-item"],
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
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "not idle"):
                FinalRecreationTests().exercise(directory, late_queue_state="paused")
            self.assertEqual((Path(directory) / "compose.yaml").read_bytes(), b"old")
            self.assertEqual(
                json.loads((Path(directory) / "receipt.json").read_text())["status"],
                "compose_reverted",
            )


class RuntimeIdentityTests(unittest.TestCase):
    """Reject mutable-tag, wrapper, and mount drift."""

    def raw(self) -> dict:
        return {
            "Id": "original-container",
            "Image": deploy.IMAGE_ID,
            "Config": {
                "Entrypoint": ["/existing-wrapper"],
                "Cmd": [],
                "Env": [],
                "Labels": {
                    "com.docker.compose.project": "homeassistant",
                    "com.docker.compose.project.config_files": str(deploy.COMPOSE),
                    "com.docker.compose.config-hash": "a" * 64,
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
            "State": {
                "Running": True,
                "StartedAt": "first-start",
                "Pid": 123,
                "Restarting": False,
                "Status": "running",
            },
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
            deploy.verify_running_config(
                self.config(), self.raw(), expected_hash="a" * 64
            )

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
                        deploy.verify_running_config(
                            config, raw, expected_hash="a" * 64
                        )
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
                deploy.verify_running_config(config, raw, expected_hash="a" * 64)
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
            deploy.verify_running_config(self.config(), raw, expected_hash="a" * 64)

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
            deploy.verify_running_config(self.config(), raw, expected_hash="a" * 64)


class RollbackTests(unittest.TestCase):
    """Keep backout available for damaged overlays and partial installations."""

    def receipt(self, status: str = "verified") -> dict:
        return {
            "source_sha": deploy.SOURCE,
            "action": "install",
            "status": status,
            "manifest_sha256": deploy.MANIFEST_SHA,
            "original_frontend_pristine": True,
            "before": {"id": "original-container", "image_id": deploy.IMAGE_ID},
            "original_frontend_hashes": {"index.html": "a" * 64},
            "compose_proposed_sha256": deploy.digest(b"patched-compose"),
        }

    def test_damaged_release_does_not_prevent_normal_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(self.receipt()))
            path.chmod(0o600)
            args = SimpleNamespace(install_receipt=path)
            raw = {
                "Id": "replacement-container",
                "Image": deploy.IMAGE_ID,
                "State": {"Running": True},
            }
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
                    ({"current": True}, {"restored": True}, "a" * 64, "b" * 64),
                    raw,
                )
            self.assertEqual(target, self.receipt()["original_frontend_hashes"])
            self.assertIsNone(original)
            self.assertFalse(config_only)
            verify.assert_called_once_with(
                {"current": True}, raw, expected_hash="a" * 64, allow_stopped=True
            )
            release.assert_not_called()
            frontend.assert_not_called()

    def test_partial_install_uses_exact_original_container_and_removed_mount_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prior = self.receipt("compose_updated")
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(prior))
            path.chmod(0o600)
            args = SimpleNamespace(install_receipt=path)
            raw = {
                "Id": "original-container",
                "Image": deploy.IMAGE_ID,
                "State": {"Running": True},
            }
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
                    ({"current": True}, {"restored": True}, "a" * 64, "b" * 64),
                    raw,
                )
            self.assertTrue(config_only)
            verify.assert_called_once_with(
                {"restored": True}, raw, expected_hash="b" * 64, allow_stopped=True
            )
            release.assert_not_called()

    def test_partial_install_requires_the_exact_recorded_post_patch_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(self.receipt("compose_updated")))
            path.chmod(0o600)
            with self.assertRaisesRegex(
                RuntimeError, "Partial-install Compose differs"
            ):
                deploy.frontend_plan(
                    "rollback",
                    SimpleNamespace(install_receipt=path),
                    b"unreviewed-change",
                    ({}, {}, "a" * 64, "b" * 64),
                    {"Id": "original-container", "Image": deploy.IMAGE_ID},
                )


class FinalRecreationTests(unittest.TestCase):
    """Recheck reviewed bytes after the last Docker/configuration read."""

    def exercise(self, directory: str, **options: Any) -> tuple[dict, list]:
        edit_on_inspect = options.get("edit_on_inspect", False)
        configuration_only = options.get("configuration_only", False)
        changed_environment = options.get("changed_environment", False)
        late_queue_state = options.get("late_queue_state")
        allow_up = options.get("allow_up", False)
        fail_up = options.get("fail_up", False)
        compose = Path(directory) / "compose.yaml"
        compose.write_bytes(b"old")
        config = {"services": {"music-assistant": {"image": "same-image"}}}
        receipt = {
            "action": "rollback" if configuration_only else "install",
            "state_before": IDLE,
            "before": {},
            "effective_proposed_sha256": deploy.digest(
                json.dumps(config, sort_keys=True).encode()
            ),
            "configuration_only": configuration_only,
        }
        calls = []
        state = copy.deepcopy(IDLE)

        def command(*args: str, **kwargs: object) -> bytes:
            calls.append(args)
            if args[:3] == ("docker", "image", "inspect"):
                if edit_on_inspect:
                    compose.write_bytes(b"late-unreviewed-edit")
                if late_queue_state:
                    state["queues"][0]["state"] = late_queue_state
                return json.dumps([{"Id": deploy.IMAGE_ID}]).encode()
            if allow_up and "up" in args:
                saved = json.loads((Path(directory) / "receipt.json").read_text())
                self.assertEqual(saved["status"], "up_started")
                self.assertIsNone(kwargs["timeout"])
                expected_log = str(Path(directory) / "compose-up.log")
                self.assertEqual(saved.get("compose_output_log"), expected_log)
                self.assertEqual(str(kwargs.get("output")), expected_log)
                if fail_up:
                    message = "Compose interrupted after recreation began"
                    raise RuntimeError(message)
                return b""
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
            patch.object(deploy, "snapshot", side_effect=lambda: copy.deepcopy(state)),
            patch.object(deploy, "inspect"),
            patch.object(deploy, "runtime", return_value={}),
            patch.object(deploy, "effective", side_effect=[config, final]),
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
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "immediately before recreation"):
                self.exercise(directory, edit_on_inspect=True)
            self.assertEqual(
                (Path(directory) / "compose.yaml").read_bytes(),
                b"late-unreviewed-edit",
            )

    def test_late_environment_change_prevents_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "configuration changed before"):
                self.exercise(directory, changed_environment=True)
            self.assertEqual((Path(directory) / "compose.yaml").read_bytes(), b"old")
            self.assertEqual(
                json.loads((Path(directory) / "receipt.json").read_text())["status"],
                "compose_reverted",
            )

    def test_queue_starting_during_image_read_undoes_only_our_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "not idle"):
                self.exercise(directory, late_queue_state="playing")
            self.assertEqual((Path(directory) / "compose.yaml").read_bytes(), b"old")

    def test_normal_up_records_started_phase_then_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt, calls = self.exercise(directory, allow_up=True)
            self.assertEqual(receipt["status"], "verified")
            self.assertTrue(receipt["checked"])
            up = next(call for call in calls if "up" in call)
            self.assertEqual(
                up[-7:],
                (
                    "up",
                    "-d",
                    "--no-deps",
                    "--no-build",
                    "--pull",
                    "never",
                    "music-assistant",
                ),
            )
            self.assertEqual(
                (Path(directory) / "compose.yaml").read_bytes(), b"patched"
            )

    def test_up_failure_retains_recoverable_phase_and_does_not_revert_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "recreation began"):
                self.exercise(directory, allow_up=True, fail_up=True)
            self.assertEqual(
                (Path(directory) / "compose.yaml").read_bytes(), b"patched"
            )
            self.assertEqual(
                json.loads((Path(directory) / "receipt.json").read_text())["status"],
                "up_started",
            )


class ReviewRecoveryTests(unittest.TestCase):
    """Regressions for stopped recovery, complete config identity and durable phases."""

    def partial(self, directory: Path, *, running: bool) -> tuple:
        prior = RollbackTests().receipt("compose_updated")
        prior["before"].update(
            {
                "started_at": "first-start",
                "running": True,
                "entrypoint": [],
                "command": [],
                "mounts": [],
            }
        )
        path = directory / "install.json"
        path.write_text(json.dumps(prior))
        path.chmod(0o600)
        current = {**prior["before"], "started_at": "later-start", "running": running}
        raw = {
            "Id": "original-container",
            "Image": deploy.IMAGE_ID,
            "State": {
                "Running": running,
                "Restarting": False,
                "Pid": 1 if running else 0,
                "Status": "running" if running else "exited",
            },
        }
        return prior, path, current, raw

    def test_same_original_container_restarted_still_allows_config_only_backout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prior, path, current, raw = self.partial(Path(directory), running=True)
            with (
                patch.object(deploy, "runtime", return_value=current),
                patch.object(deploy, "verify_running_config"),
                patch.object(
                    deploy,
                    "frontend_hashes",
                    return_value=prior["original_frontend_hashes"],
                ),
            ):
                _, _, config_only = deploy.frontend_plan(
                    "rollback",
                    SimpleNamespace(install_receipt=path),
                    b"patched-compose",
                    ({"current": True}, {"restored": True}, "a" * 64, "b" * 64),
                    raw,
                )
            self.assertTrue(config_only)

    def test_stopped_original_container_requires_up_instead_of_config_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prior, path, current, raw = self.partial(Path(directory), running=False)
            with (
                patch.object(deploy, "runtime", return_value=current),
                patch.object(deploy, "verify_running_config"),
                patch.object(deploy, "frontend_hashes") as frontend,
            ):
                hashes, _, config_only = deploy.frontend_plan(
                    "rollback",
                    SimpleNamespace(install_receipt=path),
                    b"patched-compose",
                    ({"current": True}, {"restored": True}, "a" * 64, "b" * 64),
                    raw,
                )
            self.assertEqual(hashes, prior["original_frontend_hashes"])
            self.assertFalse(config_only)
            frontend.assert_not_called()

    def test_pending_full_service_config_drift_is_refused(self) -> None:
        fixtures = RuntimeIdentityTests()
        raw = fixtures.raw()
        raw["Config"]["Labels"]["com.docker.compose.config-hash"] = "b" * 64
        image = {
            "Id": deploy.IMAGE_ID,
            "Config": {"Entrypoint": [], "Cmd": [], "Env": []},
        }

        def command(*args: str, **_kwargs: object) -> bytes:
            if args[:3] == ("docker", "image", "inspect"):
                return json.dumps([image]).encode()
            return ("music-assistant " + "b" * 64 + "\n").encode()

        with (
            patch.object(deploy, "run", side_effect=command),
            self.assertRaisesRegex(RuntimeError, "service configuration"),
        ):
            deploy.verify_running_config(fixtures.config(), raw, expected_hash="a" * 64)

    def test_modified_original_frontend_is_not_recorded_as_image_baseline(self) -> None:
        with (
            patch.object(deploy, "verify_running_config"),
            patch.object(
                deploy, "release_hashes", return_value={"index.html": "b" * 64}
            ),
            patch.object(
                deploy, "frontend_hashes", return_value={"index.html": "a" * 64}
            ),
            patch.object(
                deploy,
                "run",
                return_value=("C " + deploy.TARGET + "/index.html\n").encode(),
            ),
            self.assertRaisesRegex(RuntimeError, "Original frontend"),
        ):
            deploy.frontend_plan(
                "install", SimpleNamespace(), b"", ({}, {}, "a" * 64, "b" * 64), {}
            )

    def test_interrupted_receipt_replace_preserves_previous_complete_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text('{"status":"preflight"}\n')
            with (
                patch.object(
                    deploy.os, "replace", side_effect=OSError("interrupted rename")
                ),
                self.assertRaisesRegex(OSError, "interrupted rename"),
            ):
                deploy.save_json(path, {"status": "patch_started"})
            self.assertEqual(json.loads(path.read_text()), {"status": "preflight"})
            self.assertEqual(list(Path(directory).iterdir()), [path])


class StoppedRecoveryTests(unittest.TestCase):
    """A network outage never substitutes for Docker proof that MA has stopped."""

    def exercise_preflight(
        self, directory: Path, *, running: bool
    ) -> tuple[dict, bytes]:
        compose = directory / "compose.yaml"
        original = deploy.patch_compose(BASE, "install").encode()
        compose.write_bytes(original)
        fixtures = RuntimeIdentityTests()
        base = fixtures.config()
        installed = copy.deepcopy(base)
        installed["services"]["music-assistant"]["volumes"].append(deploy.VOLUME)
        raw = fixtures.raw()
        raw["Id"] = "installed-container"
        raw["Config"]["Labels"]["com.docker.compose.project.config_files"] = str(
            compose
        )
        raw["Config"]["Labels"]["com.docker.compose.config-hash"] = "b" * 64
        raw["Mounts"].append(
            {
                "Type": "bind",
                "Source": deploy.VOLUME["source"],
                "Destination": deploy.TARGET,
                "RW": False,
            }
        )
        raw["State"].update(
            {
                "Running": running,
                "Pid": 123 if running else 0,
                "Status": "running" if running else "exited",
            }
        )
        prior = RollbackTests().receipt()
        prior_path = directory / "install.json"
        prior_path.write_text(json.dumps(prior))
        prior_path.chmod(0o600)
        args = SimpleNamespace(
            action="rollback",
            expected_compose_sha=deploy.digest(original),
            install_receipt=prior_path,
        )
        ha = {"State": {"Running": True, "StartedAt": "fresh-ha-start"}}
        image = {
            "Id": deploy.IMAGE_ID,
            "Config": {"Entrypoint": [], "Cmd": [], "Env": []},
        }
        with (
            patch.object(deploy, "COMPOSE", compose),
            patch.object(deploy, "effective", side_effect=[installed, base]),
            patch.object(deploy, "service_hash", side_effect=["b" * 64, "a" * 64]),
            patch.object(
                deploy,
                "inspect",
                side_effect=lambda name: raw if name == deploy.MA else ha,
            ),
            patch.object(
                deploy, "run", return_value=json.dumps([image]).encode()
            ) as command,
            patch.object(
                deploy, "snapshot", side_effect=RuntimeError("MA API unreachable")
            ) as snapshot,
        ):
            if running:
                with (
                    patch.object(deploy, "MaintenanceLock"),
                    patch.object(deploy, "replace_exact") as replace,
                    self.assertRaisesRegex(RuntimeError, "API unreachable"),
                ):
                    deploy.apply_change(args, directory, lambda _: None)
                replace.assert_not_called()
                self.assertEqual(compose.read_bytes(), original)
                self.assertTrue(
                    all("up" not in call.args for call in command.call_args_list)
                )
                snapshot.assert_called_once()
                return {}, original
            receipt, before, _, _ = deploy.preflight(args, directory)
            snapshot.assert_not_called()
            self.assertEqual(before, original)
            return receipt, original

    def test_running_but_unreachable_ma_does_not_patch_or_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.exercise_preflight(Path(directory), running=True)

    def test_stopped_overlay_rollback_has_no_invented_queue_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt, _ = self.exercise_preflight(Path(directory), running=False)
            self.assertTrue(receipt["stopped_recovery"])
            self.assertFalse(receipt["configuration_only"])
            self.assertIsNone(receipt["state_before"])
            self.assertEqual(receipt["ha_started_at_before"], "fresh-ha-start")
            self.assertEqual(
                receipt["queue_preservation"], "unobservable_before_stopped_recovery"
            )
            self.assertEqual(receipt["compose_service_hash_proposed"], "a" * 64)

    def test_restarting_or_live_process_never_counts_as_stopped(self) -> None:
        stopped = {"Running": False, "Restarting": False, "Pid": 0, "Status": "exited"}
        deploy.require_stopped({"State": stopped})
        for changed in (
            {"Restarting": True},
            {"Pid": 123},
            {"Running": True},
            {"Status": "dead"},
        ):
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(RuntimeError, "not proven stopped"),
            ):
                deploy.require_stopped({"State": {**stopped, **changed}})

    def test_ma_starting_during_stopped_recovery_is_refused_before_up(self) -> None:
        raw = RuntimeIdentityTests().raw()
        receipt = {
            "stopped_recovery": True,
            "ha_started_at_before": "same-ha",
            "before": {},
            "state_before": None,
        }
        ha = {"State": {"Running": True, "StartedAt": "same-ha"}}
        with (
            patch.object(
                deploy,
                "inspect",
                side_effect=lambda name: ha if name == deploy.HA else raw,
            ),
            self.assertRaisesRegex(RuntimeError, "not proven stopped"),
        ):
            deploy.require_unchanged_before(receipt)

    def test_recovered_api_requires_idle_inventory_and_fresh_ha_start(self) -> None:
        raw = RuntimeIdentityTests().raw()
        state = copy.deepcopy(IDLE)
        state["queues"][0]["items"] = 17
        receipt = {
            "stopped_recovery": True,
            "state_before": None,
            "ha_started_at_before": "unchanged",
            "before": deploy.runtime(raw),
            "compose_service_hash_proposed": "a" * 64,
        }
        hashes = {"index.html": "a" * 64}
        with (
            patch.object(deploy, "inspect", return_value=raw),
            patch.object(deploy, "verify_running_config"),
            patch.object(deploy, "snapshot", return_value=state),
            patch.object(deploy, "frontend_hashes", return_value=hashes),
            patch.object(deploy, "served_hashes", return_value=hashes),
        ):
            result = deploy.verify_after(receipt, hashes, {})
            self.assertEqual(result["state_after"]["queues"][0]["items"], 17)
            state["queues"][0]["state"] = "playing"
            with self.assertRaisesRegex(RuntimeError, "not idle"):
                deploy.verify_after(receipt, hashes, {})
            state["queues"][0]["state"] = "idle"
            state["ha_started_at"] = "restarted"
            with self.assertRaisesRegex(RuntimeError, "HA restarted"):
                deploy.verify_after(receipt, hashes, {})

    def test_patch_intent_receipt_recovers_only_exact_recorded_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prior, path, current, raw = ReviewRecoveryTests().partial(
                Path(directory), running=True
            )
            prior["status"] = "patch_started"
            path.write_text(json.dumps(prior))
            with (
                patch.object(deploy, "runtime", return_value=current),
                patch.object(deploy, "verify_running_config"),
                patch.object(
                    deploy,
                    "frontend_hashes",
                    return_value=prior["original_frontend_hashes"],
                ),
            ):
                args = SimpleNamespace(install_receipt=path)
                _, _, config_only = deploy.frontend_plan(
                    "rollback",
                    args,
                    b"patched-compose",
                    ({}, {}, "a" * 64, "b" * 64),
                    raw,
                )
                self.assertTrue(config_only)
                with self.assertRaisesRegex(
                    RuntimeError, "Partial-install Compose differs"
                ):
                    deploy.frontend_plan(
                        "rollback",
                        args,
                        b"unrelated-compose",
                        ({}, {}, "a" * 64, "b" * 64),
                        raw,
                    )
                prior["status"] = "preflight"
                path.write_text(json.dumps(prior))
                with self.assertRaisesRegex(RuntimeError, "was not applied"):
                    deploy.frontend_plan(
                        "rollback",
                        args,
                        b"patched-compose",
                        ({}, {}, "a" * 64, "b" * 64),
                        raw,
                    )

    def test_only_cache_changes_leave_the_original_frontend_pristine(self) -> None:
        output = (
            "\n".join(
                [
                    "C " + deploy.TARGET,
                    "A " + deploy.TARGET + "/__pycache__",
                    "A " + deploy.TARGET + "/__pycache__/__init__.cpython-314.pyc",
                    "C /unrelated/runtime-file",
                ]
            )
            + "\n"
        )
        with patch.object(deploy, "run", return_value=output.encode()):
            deploy.require_image_frontend({"index.html": "a" * 64})


if __name__ == "__main__":
    unittest.main()
