# Copyright (c) 2026 Alex Mextner
"""Plant detection keeps its domain policy and delegates only agent ownership."""

import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/host"))
import ha_plant_repair_subscriber as shared
import ha_plant_sensor_recovery as plant


class PlantSubscriberTests(unittest.TestCase):
    """Exercise immutable delivery and legacy-worker migration without sensors."""

    def test_successful_native_loss_ack_is_not_repeated_by_healthy_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "subscriber-loss.json").write_text(
                json.dumps({"source": "plant-sensors", "event_id": "loss-1"})
            )
            backend = shared.SharedBackend(types.SimpleNamespace(root=root))
            response = {
                "protocol_version": 1,
                "status": "acknowledged",
                "source": "plant-sensors",
                "event_id": "loss-1",
            }
            with patch.object(
                shared.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, json.dumps(response)),
            ) as run:
                backend.acknowledge_closed({"status": "healthy"})
                backend.acknowledge_closed({"status": "healthy"})
            self.assertEqual(run.call_count, 1)
            self.assertTrue(
                json.loads((root / "subscriber-loss.json").read_text())["acknowledged"]
            )

    def test_watch_malformed_records_do_not_block_and_cleanup_is_bounded(self) -> None:
        class Process:
            def __init__(self) -> None:
                self.terminated = False
                self.stdout = ["\n", "[1,2]\n", "not-json\n"]

            def poll(self) -> None:
                return None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout=None) -> int:
                self.timeout = timeout
                return 0

        process = Process()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(plant, "STATE_ROOT", Path(directory)),
            patch.object(plant, "configure_logging"),
            patch.object(shared.subprocess, "Popen", return_value=process),
            self.assertRaisesRegex(RuntimeError, "shared_subscription_watch_ended"),
        ):
            shared.watch()
        self.assertTrue(process.terminated)
        self.assertEqual(process.timeout, 2)

    def test_retries_reuse_identical_envelope_and_use_only_shared_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            local = types.SimpleNamespace(
                root=Path(directory),
                remote_host="odroidn2",
                reconcile=lambda _: {"status": "not_started"},
            )
            backend = shared.SharedBackend(local)
            incident = {"id": "a" * 20, "started_at": 1000, "actions": []}
            receipt = {
                "status": "delivered",
                "event_id": "plant:" + incident["id"] + ":opened",
                "subscriber_id": "same-native",
            }
            with patch.object(
                shared.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, json.dumps(receipt)),
            ) as run:
                first = backend.launch(incident, {"bridge_ok": False})
                raw = backend.event_path(incident).read_bytes()
                second = backend.launch(incident, {"bridge_ok": True})
            self.assertEqual(first["status"], "pending")
            self.assertEqual(second["subscriber_id"], "same-native")
            self.assertEqual(backend.event_path(incident).read_bytes(), raw)
            self.assertTrue(
                all(
                    c.args[0][1:4] == ["publish", "--source", "plant-sensors"]
                    for c in run.call_args_list
                )
            )

    def test_existing_legacy_worker_is_reconciled_without_parallel_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            local = types.SimpleNamespace(
                root=Path(directory),
                remote_host="odroidn2",
                reconcile=lambda _: {"status": "started", "unit": "legacy"},
            )
            with patch.object(shared.subprocess, "run") as run:
                result = shared.SharedBackend(local).launch({"id": "a" * 20}, {})
            self.assertEqual(result["unit"], "legacy")
            run.assert_not_called()

    def test_positive_shared_loss_creates_new_immutable_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = types.SimpleNamespace(
                root=root,
                remote_host="odroidn2",
                reconcile=lambda _: {"status": "not_started"},
            )
            backend = shared.SharedBackend(local)
            incident = {
                "id": "a" * 20,
                "started_at": 1000,
                "actions": [],
                "launch_receipt": {"subscriber_id": "dead"},
            }
            (root / "subscriber-loss.json").write_text(
                json.dumps(
                    {
                        "source": "plant-sensors",
                        "reason": "process_exited",
                        "subscriber_id": "dead",
                        "event_id": "loss-1",
                    }
                )
            )
            with patch.object(
                shared.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, '{"status":"pending"}'),
            ):
                backend.launch(incident, {"bridge_ok": False})
            event = json.loads(backend.event_path(incident).read_text())
            self.assertEqual(event["phase"], "updated")
            self.assertEqual(event["payload"]["subscriber_loss"], "loss-1")

    def test_durable_handoff_uses_read_only_status_on_later_domain_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            local = types.SimpleNamespace(
                root=Path(directory),
                remote_host="odroidn2",
                reconcile=lambda _: {"status": "not_started"},
            )
            backend = shared.SharedBackend(local)
            incident = {"id": "a" * 20, "started_at": 1000, "actions": []}
            event_id = None
            commands = []

            def command(argv, **_kwargs: object) -> subprocess.CompletedProcess:
                nonlocal event_id
                commands.append(argv[1])
                if argv[1] == "publish":
                    event_id = json.loads(
                        Path(argv[argv.index("--event-file") + 1]).read_text()
                    )["event_id"]
                receipt = {
                    "protocol_version": 1,
                    "status": "pending",
                    "source": "plant-sensors",
                    "event_id": event_id,
                    "durable": True,
                    "digest": "a" * 64,
                    "receipt_id": None,
                }
                return subprocess.CompletedProcess(argv, 0, json.dumps(receipt))

            with patch.object(shared.subprocess, "run", command):
                backend.launch(incident, {"bridge_ok": False})
                backend.reconcile(incident)
            self.assertEqual(commands, ["publish", "status"])

    def test_initial_authoritative_vacancy_rechecks_only_previous_shared_work(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {
                "active": "a",
                "incidents": {
                    "a": {
                        "id": "a",
                        "launch_receipt": {
                            "protocol_version": 1,
                            "source": "plant-sensors",
                            "durable": True,
                            "subscriber_id": "prior",
                            "subscription_status": "delivered",
                        },
                    }
                },
            }
            (root / "state.json").write_text(json.dumps(state))
            ready = {
                "protocol_version": 1,
                "type": "ready",
                "source": "plant-sensors",
                "native_state": "unknown",
                "authority_id": "authority",
                "revision": "revision",
                "loss_pending": False,
            }
            self.assertIsNone(shared.startup_loss(ready, root))
            ready["native_state"] = "vacant"
            loss = shared.startup_loss(ready, root)
            self.assertEqual(loss["subscriber_id"], "prior")
            self.assertFalse(loss["native_ack_required"])
            self.assertEqual(shared.startup_loss(ready, root), loss)
            for version, pending in ((1, True), (1, None), (2, False)):
                self.assertIsNone(
                    shared.startup_loss(
                        {**ready, "protocol_version": version, "loss_pending": pending},
                        root,
                    )
                )
            state["incidents"]["a"]["launch_receipt"] = {
                "status": "started",
                "unit": "legacy",
            }
            (root / "state.json").write_text(json.dumps(state))
            self.assertIsNone(shared.startup_loss(ready, root))


if __name__ == "__main__":
    unittest.main()
