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


class PlantSubscriberTests(unittest.TestCase):
    """Exercise immutable delivery and legacy-worker migration without sensors."""

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
            self.assertEqual(first["status"], "started")
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


if __name__ == "__main__":
    unittest.main()
