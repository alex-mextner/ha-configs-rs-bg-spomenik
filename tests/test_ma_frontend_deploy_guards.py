# Copyright (c) 2026 Alex Mextner.
"""Regressions for observed MA queue persistence and deployment review findings."""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import patch

from scripts import ma_frontend_deploy as deploy

PLAYERS = [
    {
        "player_id": "studio",
        "available": True,
        "playback_state": "idle",
        "active_source": "studio",
    }
]
QUEUES = [
    {
        "queue_id": "studio",
        "state": "idle",
        "items": 2,
        "current_index": 0,
        "elapsed_time": 42,
        "current_item": {"queue_item_id": "second"},
        "next_item": {"queue_item_id": "first"},
    }
]
STATE = {
    "ha_started_at": "unchanged",
    "players": PLAYERS,
    "queues": [
        {
            **QUEUES[0],
            "current_item": "second",
            "next_item": "first",
            "queue_item_ids": ["second", "first"],
        }
    ],
}


class SnapshotCollectorTests(unittest.TestCase):
    """Execute the actual embedded collector with only its external client replaced."""

    def collect(self, **options: Any) -> tuple[dict, list]:
        calls = []
        players = copy.deepcopy(PLAYERS)
        queues = copy.deepcopy(QUEUES)
        count = options.get("count", 2)
        queues[0]["items"] = count
        page = options.get(
            "page", [{"queue_item_id": "second"}, {"queue_item_id": "first"}]
        )
        reads = {"players/all": 0, "player_queues/all": 0}

        class Client:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *_: object) -> bool:
                return False

            async def send_command(self, command: str, **kwargs: Any) -> list:
                calls.append((command, kwargs))
                if command == "player_queues/items":
                    if options.get("pages"):
                        return copy.deepcopy(options["pages"][kwargs["offset"]])
                    return copy.deepcopy(page)
                if command not in reads:
                    raise AssertionError(command)
                reads[command] += 1
                value = copy.deepcopy(players if command == "players/all" else queues)
                if reads[command] > 1 and options.get("drift") == command:
                    key = "available" if command == "players/all" else "current_index"
                    value[0][key] = False if key == "available" else 1
                return value

        module = SimpleNamespace(
            MusicAssistantClient=lambda *_args, **_kwargs: Client()
        )
        config = {
            "data": {
                "entries": [{"domain": "music_assistant", "data": {"token": "fake"}}]
            }
        }
        output = io.StringIO()
        with (
            patch.dict("sys.modules", {"music_assistant_client": module}),
            patch.object(Path, "read_text", return_value=json.dumps(config)),
            redirect_stdout(output),
        ):
            exec(  # noqa: S102 -- execute the trusted embedded collector under test
                compile(deploy.MA_STATE_CODE, "<MA_STATE_CODE>", "exec"), {}
            )
        return json.loads(output.getvalue()), calls

    def test_collects_actual_player_state_and_preserves_ordered_queue_ids(self) -> None:
        state, _ = self.collect()
        self.assertEqual(state["players"], PLAYERS)
        self.assertEqual(state["queues"][0].get("queue_item_ids"), ["second", "first"])

    def test_pages_are_bounded_by_the_advertised_count(self) -> None:
        first = [{"queue_item_id": f"item-{i}"} for i in range(500)]
        state, calls = self.collect(
            count=501, pages={0: first, 500: [{"queue_item_id": "last"}]}
        )
        item_calls = [
            kwargs for command, kwargs in calls if command == "player_queues/items"
        ]
        self.assertEqual(
            item_calls,
            [
                {"queue_id": "studio", "limit": 500, "offset": 0},
                {"queue_id": "studio", "limit": 1, "offset": 500},
            ],
        )
        self.assertEqual(
            state["queues"][0].get("queue_item_ids", [])[-2:], ["item-499", "last"]
        )

    def test_empty_queue_has_an_explicit_empty_order_without_fetching_pages(
        self,
    ) -> None:
        state, calls = self.collect(count=0)
        self.assertEqual(state["queues"][0].get("queue_item_ids"), [])
        self.assertFalse(any(command == "player_queues/items" for command, _ in calls))

    def test_rejects_incomplete_pages_and_invalid_ids(self) -> None:
        for page in (
            [],
            [{"queue_item_id": "only-one"}],
            [{}, {}],
            [{"queue_item_id": ""}, {"queue_item_id": "ok"}],
            [{"queue_item_id": 1}, {"queue_item_id": "ok"}],
        ):
            with self.subTest(page=page), self.assertRaises(RuntimeError):
                self.collect(page=page)

    def test_rejects_invalid_advertised_count(self) -> None:
        for count in (-1, True, "2"):
            with self.subTest(count=count), self.assertRaises(RuntimeError):
                self.collect(count=count)

    def test_rejects_header_or_player_drift_during_collection(self) -> None:
        for command in ("players/all", "player_queues/all"):
            with self.subTest(command=command), self.assertRaises(RuntimeError):
                self.collect(drift=command)


class PreservedStateTests(unittest.TestCase):
    """Distinguish derived startup fields from actual playback and queue loss."""

    def test_external_playback_is_not_idle_when_all_queues_are_idle(self) -> None:
        for playback in ("playing", "paused", "unknown"):
            state = copy.deepcopy(STATE)
            state["players"][0].update(playback_state=playback, active_source="airplay")
            with (
                self.subTest(playback=playback),
                self.assertRaisesRegex(RuntimeError, "not idle"),
            ):
                deploy.require_idle(state)

    def test_empty_player_inventory_cannot_establish_idle(self) -> None:
        state = copy.deepcopy(STATE)
        state["players"] = []
        with self.assertRaisesRegex(RuntimeError, "player inventory is empty"):
            deploy.require_idle(state)

    def test_pre_up_still_rejects_derived_state_changes(self) -> None:
        state = copy.deepcopy(STATE)
        state["queues"][0]["next_item"] = None
        with self.assertRaisesRegex(RuntimeError, "Queue"):
            deploy.require_same_state(STATE, state)

    def verify(self, state: dict, **options: object) -> dict:
        runtime = {"entrypoint": ["/wrapper"], "command": []}
        receipt = {
            "before": runtime,
            "state_before": STATE,
            "compose_service_hash_proposed": "a" * 64,
            **options,
        }
        hashes = {"index.html": "b" * 64}
        with (
            patch.object(deploy, "inspect", return_value={}),
            patch.object(deploy, "verify_running_config"),
            patch.object(deploy, "runtime", return_value=runtime),
            patch.object(deploy, "snapshot", return_value=state),
            patch.object(deploy, "frontend_hashes", return_value=hashes),
            patch.object(deploy, "served_hashes", return_value=hashes),
        ):
            return deploy.verify_after(receipt, hashes, {})

    def test_restart_allows_only_recomputed_current_next_and_active_source(
        self,
    ) -> None:
        state = copy.deepcopy(STATE)
        state["queues"][0].update(current_item=None, next_item=None)
        state["players"][0]["active_source"] = None
        try:
            result = self.verify(state)
        except RuntimeError as exc:
            self.fail(f"Derived startup state was treated as data loss: {exc}")
        self.assertEqual(result["state_after"], state)
        self.assertEqual(STATE["queues"][0]["queue_item_ids"], ["second", "first"])

    def test_config_only_backout_keeps_exact_state_comparison(self) -> None:
        state = copy.deepcopy(STATE)
        state["queues"][0]["next_item"] = None
        with self.assertRaisesRegex(RuntimeError, "Queue"):
            self.verify(state, configuration_only=True)

    def test_restart_rejects_queue_order_loss_resume_change_and_roster_drift(
        self,
    ) -> None:
        changes = [
            ("queues", "queue_item_ids", ["first", "second"]),
            ("queues", "queue_item_ids", []),
            ("queues", "items", 1),
            ("queues", "current_index", 1),
            ("queues", "elapsed_time", 0),
            ("players", "available", False),
            ("players", "player_id", "different"),
            ("players", "playback_state", "paused"),
        ]
        for kind, key, value in changes:
            state = copy.deepcopy(STATE)
            state[kind][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                self.verify(state)


class ArtifactGuardTests(unittest.TestCase):
    """Reject unverifiable rollback baselines and mutable release payloads."""

    def test_install_rejects_an_unusable_original_hash_map_before_release_work(
        self,
    ) -> None:
        for hashes in (
            {},
            {"index.html": "bad"},
            {"index.html": "a" * 64, "../x": "b" * 64},
        ):
            with (
                self.subTest(hashes=hashes),
                patch.object(deploy, "verify_running_config"),
                patch.object(deploy, "frontend_hashes", return_value=hashes),
                patch.object(deploy, "require_image_frontend"),
                patch.object(deploy, "release_hashes"),
                self.assertRaisesRegex(RuntimeError, "Original hash"),
            ):
                deploy.frontend_plan(
                    "install", SimpleNamespace(), b"", ({}, {}, "", ""), {}
                )

    def release(self, root: Path) -> None:
        package = root / deploy.PACKAGE
        package.mkdir()
        (package / "index.html").write_bytes(b"entry")
        name = deploy.PACKAGE + "/index.html"
        manifest = {
            "source_sha": deploy.SOURCE,
            "workflow_sha": deploy.WORKFLOW,
            "workflow_run_id": deploy.RUN,
            "base_frontend_version": "2.17.297",
            "files": [{"path": name, "size": 5, "sha256": deploy.digest(b"entry")}],
        }
        data = json.dumps(manifest).encode()
        (root / "manifest.json").write_bytes(data)
        (root / "SHA256SUMS").write_text(
            f"{deploy.digest(data)}  manifest.json\n{deploy.digest(b'entry')}  {name}\n"
        )
        for path in root.rglob("*"):
            path.chmod(0o555 if path.is_dir() else 0o444)
        root.chmod(0o555)

    def test_release_refuses_group_or_world_writes_at_every_tree_level(self) -> None:
        for relative, mode in (
            (".", 0o775),
            (deploy.PACKAGE, 0o775),
            (deploy.PACKAGE + "/index.html", 0o646),
        ):
            with (
                self.subTest(relative=relative),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                self.release(root)
                (root / relative).chmod(mode)
                with (
                    patch.object(deploy, "RELEASE", root),
                    patch.object(deploy, "FILE_COUNT", 1),
                    patch.object(
                        deploy,
                        "MANIFEST_SHA",
                        deploy.digest((root / "manifest.json").read_bytes()),
                    ),
                    self.assertRaisesRegex(RuntimeError, "ownership or permissions"),
                ):
                    deploy.release_hashes()

    def test_release_requires_the_operator_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.release(root)
            different_uid = os.getuid() + 1
            with (
                patch.object(deploy, "RELEASE", root),
                patch.object(deploy, "FILE_COUNT", 1),
                patch.object(
                    deploy,
                    "MANIFEST_SHA",
                    deploy.digest((root / "manifest.json").read_bytes()),
                ),
                patch.object(deploy.os, "getuid", return_value=different_uid),
                self.assertRaisesRegex(RuntimeError, "ownership or permissions"),
            ):
                deploy.release_hashes()

    def test_read_only_owned_release_still_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.release(root)
            with (
                patch.object(deploy, "RELEASE", root),
                patch.object(deploy, "FILE_COUNT", 1),
                patch.object(
                    deploy,
                    "MANIFEST_SHA",
                    deploy.digest((root / "manifest.json").read_bytes()),
                ),
            ):
                self.assertEqual(
                    deploy.release_hashes(), {"index.html": deploy.digest(b"entry")}
                )

    def test_service_worker_does_not_substitute_for_a_missing_entry_script(
        self,
    ) -> None:
        payload = {
            "": b'<html><link href="assets/style.css"></html>',
            "sw.js": b"worker",
            "manifest.webmanifest": b"{}",
            "assets/style.css": b"css",
        }
        requested = []

        class Connection:
            def request(self, _method: str, path: str, **_kwargs: object) -> None:
                requested.append(path.removeprefix("/"))

            def getresponse(self) -> SimpleNamespace:
                return SimpleNamespace(
                    status=200, read=lambda _: payload[requested[-1]]
                )

            def close(self) -> None:
                pass

        hashes = {
            name or "index.html": deploy.digest(data) for name, data in payload.items()
        }
        with (
            patch.object(deploy, "HTTPConnection", return_value=Connection()),
            self.assertRaisesRegex(RuntimeError, "No entry JavaScript"),
        ):
            deploy.served_hashes(hashes)


class CommandOutputTests(unittest.TestCase):
    """A failed Compose process leaves private diagnostics available for recovery."""

    def test_failed_command_keeps_both_output_streams_in_private_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "compose-up.log"
            command = "import os; os.write(1, b'out\\n'); os.write(2, b'err\\n'); raise SystemExit(7)"
            with self.assertRaisesRegex(RuntimeError, "failed with exit 7"):
                deploy.run(sys.executable, "-c", command, output=output, timeout=None)
            self.assertEqual(output.read_bytes(), b"out\nerr\n")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_unlogged_command_still_returns_only_its_stdout(self) -> None:
        command = "import os; os.write(1, b'output'); os.write(2, b'private stderr')"
        self.assertEqual(deploy.run(sys.executable, "-c", command), b"output")
