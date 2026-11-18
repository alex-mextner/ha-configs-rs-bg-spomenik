# Copyright (c) 2026 Alex Mextner
# ruff: noqa: S101 -- this module contains executable test assertions only.
"""Recovery contract: deterministic first aid before a single durable OMP launch."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

SOURCE = (
    Path(__file__).resolve().parents[2] / "scripts/host/ha_plant_sensor_recovery.py"
)
if SOURCE.exists():
    spec = importlib.util.spec_from_file_location("plant_recovery", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
else:
    module = None


def native_title_slot() -> dict:
    """Match session-title-slot.ts: a 256-byte title record precedes the header."""
    title = {
        "type": "title",
        "v": 1,
        "title": "Repair sensor",
        "updatedAt": "2026-11-18T00:00:00Z",
        "pad": "",
    }
    encoded = json.dumps(title, separators=(",", ":")) + "\n"
    title["pad"] = " " * (256 - len(encoded.encode()))
    return title


def snapshot(
    reason: str = "saturated",
    *,
    valid: bool = False,
    started: object = 1000.0,
    bridge: bool = True,
) -> dict:
    """Build one source snapshot with an open reliability incident."""
    return {
        "care": {
            "state": "ok" if valid else "sensor_error",
            "attributes": {
                "reliability_valid": valid,
                "reliability_reason": reason,
                "reliability_incident_started_at": started,
                "last_sample_at": 2000,
            },
        },
        "bridge_ok": bridge,
        "bridge_measurement_ok": bridge,
        "raw": {"state": "394", "attributes": {"sampled_at": "2026-11-18T00:34:04Z"}},
        "dry_adc": "67",
        "wet_adc": "221",
    }


class Backend:
    """Record external effects while exercising real durable controller state."""

    def __init__(self) -> None:
        """Prepare deterministic external outcomes."""
        self.actions = []
        self.fresh = snapshot()
        self.launch_error = False
        self.reconciled = {"status": "unknown"}

    def refresh(self) -> dict:
        """Return the next physical probe result."""
        self.actions.append("refresh")
        return self.fresh

    def restart_bridge(self) -> dict:
        """Record the one allowed service restart."""
        self.actions.append("restart")
        return {"status": "requested"}

    def launch(self, incident: dict, evidence: dict) -> dict:
        """Return a launch receipt or lose its reply."""
        self.actions.append("launch")
        if self.launch_error:
            message = "response lost"
            raise TimeoutError(message)
        return {"host_id": "plant-repair-" + incident["id"], "status": "started"}

    def reconcile(self, incident: dict) -> dict:
        """Return independent identity reconciliation."""
        self.actions.append("reconcile")
        return self.reconciled


class RecoveryTests(unittest.TestCase):
    """Verify bounded recovery and crash-safe launch identity."""

    def setUp(self) -> None:
        assert module is not None, "No durable plant sensor recovery controller exists"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.backend = Backend()
        self.controller = module.Controller(self.path, self.backend)

    def unit_response(
        self, identity: str, *, active: bool = False
    ) -> subprocess.CompletedProcess:
        """Use the real systemctl show fields for a manager-owned repair unit."""
        state = "active" if active else "inactive"
        pid = 123 if active else 0
        fields = f"LoadState=loaded\nActiveState={state}\nMainPID={pid}\nControlGroup=/user.slice/omp.slice/ha-plant-omp-{identity}.service\n"
        return subprocess.CompletedProcess([], 0, fields)

    def session_directory(self, identity: str, *, final: bool = False) -> Path:
        """Write one valid native root conversation, optionally with a normal exit."""
        directory = Path(self.tmp.name) / "incidents" / identity
        (directory / "sessions").mkdir(parents=True)
        entries = [
            native_title_slot(),
            {"type": "session", "id": "saved"},
            {
                "type": "message",
                "message": {"role": "user", "content": "Repair sensor"},
            },
        ]
        if final:
            entries.extend(
                [
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Recorded diagnosis"}],
                            "stopReason": "stop",
                        },
                    },
                    {
                        "type": "custom",
                        "customType": "session_exit",
                        "data": {"kind": "normal", "pendingToolCalls": []},
                    },
                ]
            )
        (directory / "sessions/main.jsonl").write_text(
            "\n".join(json.dumps(entry, separators=(",", ":")) for entry in entries)
            + "\n"
        )
        return directory

    def test_unresolved_cap_gets_two_real_refreshes_then_one_omp(self) -> None:
        self.controller.tick(snapshot(), 2000)
        self.controller.tick(snapshot(), 2299)
        assert self.backend.actions == ["refresh"]
        self.controller.tick(snapshot(), 2300)
        assert self.backend.actions == ["refresh", "refresh"]
        self.controller.tick(snapshot(), 2600)
        assert self.backend.actions == ["refresh", "refresh", "launch"]
        module.Controller(self.path, self.backend).tick(snapshot(), 3200)
        assert self.backend.actions.count("launch") == 1
        assert "restart" not in self.backend.actions

    def test_stable_recovery_cancels_escalation(self) -> None:
        self.controller.tick(snapshot(), 2000)
        self.controller.tick(snapshot(valid=True), 2400)
        self.controller.tick(snapshot(valid=True), 3600)
        assert self.backend.actions == ["refresh"]

    def test_bridge_restarted_once_only_after_failed_physical_reads(self) -> None:
        failed = snapshot(reason="stale", bridge=False)
        self.backend.fresh = failed
        self.controller.tick(failed, 2000)
        self.controller.tick(failed, 2300)
        self.controller.tick(failed, 2600)
        self.controller.tick(failed, 2900)
        self.controller.tick(failed, 3200)
        assert self.backend.actions == [
            "refresh",
            "refresh",
            "restart",
            "refresh",
            "launch",
        ]

    def test_launch_timeout_reconciles_instead_of_spawning_again(self) -> None:
        self.backend.launch_error = True
        self.controller.tick(snapshot(), 2000)
        self.controller.tick(snapshot(), 2300)
        self.controller.tick(snapshot(), 2600)
        module.Controller(self.path, self.backend).tick(snapshot(), 3200)
        assert self.backend.actions.count("launch") == 1
        assert "reconcile" in self.backend.actions

    def test_failed_liveness_check_does_not_reuse_a_stale_started_receipt(self) -> None:
        self.controller.tick(snapshot(), 2000)
        self.controller.tick(snapshot(), 2300)
        self.controller.tick(snapshot(), 2600)
        with patch.object(self.backend, "reconcile", side_effect=TimeoutError):
            result = self.controller.tick(snapshot(), 3200)
        assert result["status"] == "launch_pending"
        assert result["launch"]["status"] == "unknown"
        assert result["launch"]["last_known_status"] == "started"
        assert self.backend.actions.count("launch") == 1

    def test_transport_failure_does_not_consume_confirmed_first_aid_budget(
        self,
    ) -> None:
        with patch.object(self.backend, "refresh", side_effect=TimeoutError):
            for now in (2000, 2300, 2600):
                self.controller.tick(snapshot(), now)
        state = json.loads(self.path.read_text())
        incident = state["incidents"][state["active"]]
        assert incident["refresh_attempts"] == 0
        assert "launch" not in self.backend.actions
        self.controller.tick(snapshot(), 2900)
        assert self.backend.actions == ["refresh"]

    def test_host_records_real_exit_without_inspection_calls(self) -> None:
        directory = Path(self.tmp.name)
        with (
            patch.object(module.subprocess, "Popen") as spawn,
            patch.object(module.subprocess, "run", side_effect=TimeoutError) as inspect,
        ):
            process = spawn.return_value
            process.__enter__.return_value = process
            process.pid = 321
            process.wait.return_value = 0
            result = module.run_omp_process(
                directory, ["omp", "--print", "authorized prompt"]
            )
        assert result["status"] == "process_exited"
        assert result["exit_code"] == 0
        assert inspect.call_count == 0
        assert spawn.call_count == 1

    def test_reason_change_does_not_create_new_incident(self) -> None:
        self.controller.tick(snapshot(), 2000)
        self.controller.tick(snapshot(reason="unchanged"), 2300)
        self.controller.tick(snapshot(reason="saturated"), 2600)
        assert self.backend.actions == ["refresh", "refresh", "launch"]

    def test_invalid_contract_never_launches_or_restarts(self) -> None:
        for value in (None, float("nan"), float("inf"), 10000):
            result = self.controller.tick(snapshot(started=value), 2000)
            assert result["status"] == "invalid_contract"
        assert self.backend.actions == []

    def test_missing_integration_contract_does_not_act(self) -> None:
        result = self.controller.tick(
            {"care": {"state": "unavailable", "attributes": {}}}, 2000
        )
        assert result["status"] == "waiting_for_integration"
        assert self.backend.actions == []

    def test_definitely_absent_launch_retries_same_incident_after_backoff(self) -> None:
        self.backend.launch_error = True
        self.controller.tick(snapshot(), 2000)
        self.controller.tick(snapshot(), 2300)
        first = self.controller.tick(snapshot(), 2600)
        assert first["status"] == "launch_pending"
        self.backend.reconciled = {"status": "not_started"}
        self.backend.launch_error = False
        module.Controller(self.path, self.backend).tick(snapshot(), 3000)
        assert self.backend.actions.count("launch") == 2
        state = json.loads(self.path.read_text())
        assert len(state["incidents"]) == 1

    def test_crash_after_claim_before_launch_is_reconciled_then_dispatched(
        self,
    ) -> None:
        self.controller.tick(snapshot(), 2000)
        self.controller.tick(snapshot(), 2300)
        state = json.loads(self.path.read_text())
        incident = state["incidents"][state["active"]]
        incident["launch_claimed_at"] = 2600
        incident["launch_receipt"] = {"status": "pending"}
        self.path.write_text(json.dumps(state))
        self.backend.reconciled = {"status": "not_started"}
        result = module.Controller(self.path, self.backend).tick(snapshot(), 3000)
        assert result["status"] == "escalated"
        assert self.backend.actions.count("launch") == 1

    def test_unavailable_care_does_not_close_existing_incident(self) -> None:
        self.controller.tick(snapshot(), 2000)
        restored = snapshot(valid=True)
        restored["care"]["state"] = "unavailable"
        result = self.controller.tick(restored, 2400)
        assert result["status"] == "waiting_for_integration"
        state = json.loads(self.path.read_text())
        assert state["active"] is not None

    def test_refresh_cannot_close_incident_using_unavailable_restored_attributes(
        self,
    ) -> None:
        fresh = snapshot(valid=True)
        fresh["care"]["state"] = "unavailable"
        self.backend.fresh = fresh
        self.controller.tick(snapshot(), 2000)
        state = json.loads(self.path.read_text())
        assert state["active"] is not None

    def test_live_omp_is_not_terminated_when_result_file_appears(self) -> None:
        identity = "1" * 20
        directory = self.session_directory(identity, final=True)
        (directory / "result.md").write_text("A draft report is not process exit")
        (directory / "host_receipt.json").write_text(
            json.dumps(
                {
                    "status": "started",
                    "boot_id": "current",
                    "runner_pid": 123,
                    "omp_pid": 456,
                }
            )
        )
        with (
            patch.object(
                module.subprocess,
                "run",
                return_value=self.unit_response(identity, active=True),
            ),
            patch.object(module, "process_in_unit", return_value=True, create=True),
            patch.object(module, "boot_id", return_value="current"),
        ):
            result = module.SystemBackend(Path(self.tmp.name)).reconcile(
                {"id": identity}
            )
        assert result["status"] == "started"

    def test_failed_empty_launch_is_retryable_after_all_identities_are_absent(
        self,
    ) -> None:
        identity = "2" * 20
        directory = Path(self.tmp.name) / "incidents" / identity
        directory.mkdir(parents=True)
        failure = None
        with patch.object(module.subprocess, "Popen", side_effect=FileNotFoundError):
            try:
                module.run_omp_process(directory, ["missing-test-wrapper"])
            except FileNotFoundError as error:
                failure = error
        assert failure is not None
        assert (
            json.loads((directory / "host_receipt.json").read_text())["status"]
            == "spawn_failed"
        )
        with patch.object(
            module.subprocess, "run", return_value=self.unit_response(identity)
        ):
            result = module.SystemBackend(Path(self.tmp.name)).reconcile(
                {"id": identity}
            )
        assert result["status"] == "not_started"
        assert result["retry_unit"] == "ha-plant-omp-" + identity + ".service"

    def test_reboot_can_resume_exactly_one_root_session_once(self) -> None:
        identity = "3" * 20
        directory = self.session_directory(identity)
        (directory / "result.md").write_text("Draft report before final validation")
        (directory / "host_receipt.json").write_text(
            json.dumps({"status": "started", "boot_id": "old-boot"})
        )
        with (
            patch.object(
                module.subprocess, "run", return_value=self.unit_response(identity)
            ),
            patch.object(module, "boot_id", return_value="new-boot"),
        ):
            result = module.SystemBackend(Path(self.tmp.name)).reconcile(
                {"id": identity}
            )
        assert result["status"] == "resume_available"
        assert result["resume_session"] == str(directory / "sessions/main.jsonl")

    def test_reboot_does_not_resume_completed_or_explicitly_stopped_turn(self) -> None:
        session = Path(self.tmp.name) / "main.jsonl"
        initial = [
            native_title_slot(),
            {"type": "session", "id": "saved"},
            {
                "type": "message",
                "message": {"role": "user", "content": "Repair sensor"},
            },
        ]
        endings = [
            {
                "type": "custom",
                "customType": "session_exit",
                "data": {"kind": "normal"},
            },
            {
                "type": "custom",
                "customType": "session_exit",
                "data": {"kind": "signal"},
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Finished"}],
                    "stopReason": "stop",
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "id": "t1"}],
                    "stopReason": "aborted",
                },
            },
        ]
        for ending in endings:
            session.write_text(
                "\n".join(
                    json.dumps(entry, separators=(",", ":"))
                    for entry in [*initial, ending]
                )
                + "\n"
            )
            assert not module.session_unfinished(session)

    def test_failed_resume_dispatch_can_retry_the_same_pending_claim(self) -> None:
        identity = "5" * 20
        directory = Path(self.tmp.name) / "incidents" / identity
        (directory / "sessions").mkdir(parents=True)
        session = directory / "sessions/main.jsonl"
        entries = [
            native_title_slot(),
            {"type": "session", "id": "saved"},
            {
                "type": "message",
                "message": {"role": "user", "content": "Repair sensor"},
            },
        ]
        session.write_text(
            "\n".join(json.dumps(entry, separators=(",", ":")) for entry in entries)
            + "\n"
        )
        (directory / "host_receipt.json").write_text(
            json.dumps({"status": "started", "boot_id": "old-boot"})
        )
        backend = module.SystemBackend(Path(self.tmp.name))
        receipt = {"status": "resume_available", "resume_session": str(session)}
        failed = None
        with (
            patch.object(backend, "reconcile", return_value=receipt),
            patch.object(
                module.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, "systemd-run"),
            ),
            patch.object(module, "boot_id", return_value="new-boot"),
        ):
            try:
                backend.launch({"id": identity, "actions": []}, snapshot())
            except subprocess.CalledProcessError as error:
                failed = error
        assert failed is not None
        responses = [self.unit_response(identity)]
        with (
            patch.object(module.subprocess, "run", side_effect=responses),
            patch.object(module, "boot_id", return_value="new-boot"),
        ):
            result = backend.reconcile({"id": identity})
        assert result["status"] == "resume_available"
        assert result["resume_session"] == str(session)

    def test_finished_transcript_and_report_need_successful_process_exit(self) -> None:
        identity = "6" * 20
        directory = self.session_directory(identity, final=True)
        (directory / "result.md").write_text("Final measured diagnosis")
        backend = module.SystemBackend(Path(self.tmp.name))
        receipt_path = directory / "host_receipt.json"
        with patch.object(
            module.subprocess, "run", return_value=self.unit_response(identity)
        ):
            receipt_path.write_text(json.dumps({"status": "started", "boot_id": "old"}))
            assert backend.reconcile({"id": identity})["status"] != "completed"
            receipt_path.write_text(
                json.dumps({"status": "process_exited", "exit_code": 1})
            )
            assert backend.reconcile({"id": identity})["status"] == "failed"
            receipt_path.write_text(
                json.dumps({"status": "process_exited", "exit_code": 0})
            )
            assert backend.reconcile({"id": identity})["status"] == "completed"

    def test_same_boot_missing_process_does_not_override_user_stop(self) -> None:
        identity = "4" * 20
        directory = self.session_directory(identity)
        (directory / "host_receipt.json").write_text(
            json.dumps({"status": "started", "boot_id": "same-boot"})
        )
        with (
            patch.object(
                module.subprocess, "run", return_value=self.unit_response(identity)
            ),
            patch.object(module, "boot_id", return_value="same-boot"),
        ):
            result = module.SystemBackend(Path(self.tmp.name)).reconcile(
                {"id": identity}
            )
        assert result["status"] == "interrupted"

    def test_slow_confirmed_refresh_keeps_five_minutes_after_completion(self) -> None:
        self.backend.fresh["request_completed_at"] = 2076
        self.controller.tick(snapshot(), 2000)
        self.controller.tick(snapshot(), 2300)
        assert self.backend.actions == ["refresh"]
        self.controller.tick(snapshot(), 2376)
        assert self.backend.actions == ["refresh", "refresh"]

    def test_late_recovery_gets_its_own_confirmation_grace(self) -> None:
        self.controller.tick(snapshot(), 2000)
        self.controller.tick(snapshot(), 2300)
        result = self.controller.tick(snapshot(reason="recovering"), 10000)
        assert result["status"] == "confirming_recovery"
        assert "launch" not in self.backend.actions

    def test_first_aid_evidence_survives_later_plain_observations(self) -> None:
        self.backend.fresh["physical_reading"] = {"adc_raw": 394, "sampled_at": "fresh"}
        self.controller.tick(snapshot(), 2000)
        self.controller.tick(snapshot(), 2300)
        self.controller.tick(snapshot(), 2600)
        state = json.loads(self.path.read_text())
        incident = state["incidents"][state["active"]]
        assert (
            incident["actions"][0]["measurement"]["physical_reading"]["adc_raw"] == 394
        )

    def test_existing_root_conversation_requires_explicit_resume_claim(self) -> None:
        identity = "7" * 20
        self.session_directory(identity)
        with (
            patch.object(module, "inside_host_unit", return_value=True, create=True),
            patch.object(module.subprocess, "Popen") as spawn,
        ):
            result = module.host_incident(Path(self.tmp.name), identity)
        assert result["status"] == "interrupted"
        assert spawn.call_count == 0

    def test_fresh_probe_rejects_non_object_bridge_payloads_and_tolerates_helper_404(
        self,
    ) -> None:
        for payload in (None, []):

            def response(
                url: str, *_args: object, _payload: object = payload, **_kwargs: object
            ) -> object:
                if url.endswith(("/reading/fresh", "/healthz")):
                    return _payload
                if "input_number." in url:
                    error = HTTPError(url, 404, "missing helper", {}, None)
                    error.close()
                    raise error
                if "/services/" in url:
                    return []
                return {"state": "sensor_error", "attributes": {}}

            with (
                patch.object(module, "read_ha_token", return_value="unused"),
                patch.object(module, "request_json", side_effect=response),
            ):
                result = module.probe(fresh=True)
            assert result["bridge_measurement_ok"] is False
            assert result["care"]["state"] == "sensor_error"
            assert result["dry_adc"] is None
            assert result["entity_errors"]["dry_adc"] == "HTTPError"

    def test_unsafe_log_directory_uses_a_private_fallback(self) -> None:
        preferred = Path(self.tmp.name) / "logs"
        target = Path(self.tmp.name) / "foreign"
        target.mkdir()
        preferred.symlink_to(target, target_is_directory=True)
        selected = module.private_log_directory(
            preferred, fallback_root=Path(self.tmp.name)
        )
        assert selected != preferred
        assert selected.stat().st_mode & 0o077 == 0
        assert list(target.iterdir()) == []
        repeated = module.private_log_directory(
            preferred, fallback_root=Path(self.tmp.name)
        )
        assert repeated == selected

    def test_remote_permanent_failures_are_distinct_from_temporary_outages(
        self,
    ) -> None:
        missing = subprocess.CalledProcessError(2, "ssh")
        unreachable = subprocess.CalledProcessError(255, "ssh")
        denied = subprocess.CalledProcessError(
            1, "ssh", output=json.dumps({"error": "HTTPError", "http_status": 401})
        )
        unavailable = subprocess.CalledProcessError(
            1, "ssh", output=json.dumps({"error": "HTTPError", "http_status": 503})
        )
        assert module.operation_failure(missing)["status"] == "blocked"
        assert module.operation_failure(denied)["status"] == "blocked"
        assert module.operation_failure(unreachable)["status"] == "deferred"
        assert module.operation_failure(unavailable)["status"] == "deferred"

    def test_native_title_slot_and_legacy_header_are_both_readable(self) -> None:
        directory = self.session_directory("a" * 20)
        path = directory / "sessions/main.jsonl"
        lines = path.read_text().splitlines(keepends=True)
        assert len(lines[0].encode()) == 256
        assert module.session_unfinished(path)
        path.write_text("".join(lines[1:]))
        assert module.session_unfinished(path)
        path.write_text(lines[0] + lines[2])
        assert module.session_tail(path) is None

    def test_recorded_child_exit_without_transcript_is_terminal(self) -> None:
        identity = "b" * 20
        directory = Path(self.tmp.name) / "incidents" / identity
        directory.mkdir(parents=True)
        backend = module.SystemBackend(Path(self.tmp.name))
        for code in (0, 1):
            (directory / "host_receipt.json").write_text(
                json.dumps({"status": "process_exited", "exit_code": code})
            )
            with patch.object(
                module.subprocess, "run", return_value=self.unit_response(identity)
            ):
                result = backend.reconcile({"id": identity})
            assert result["status"] == "failed"
            assert result["exit_code"] == code
        self.controller.tick(snapshot(), 2000)
        self.controller.tick(snapshot(), 2300)
        self.controller.tick(snapshot(), 2600)
        self.backend.reconciled = result
        for now in (3200, 7000):
            assert self.controller.tick(snapshot(), now)["status"] == "repair_failed"
        assert self.backend.actions.count("launch") == 1

    def test_started_process_without_saved_conversation_does_not_relaunch(self) -> None:
        identity = "c" * 20
        directory = Path(self.tmp.name) / "incidents" / identity
        directory.mkdir(parents=True)
        (directory / "host_receipt.json").write_text(
            json.dumps({"status": "started", "omp_pid": 456})
        )
        with patch.object(
            module.subprocess, "run", return_value=self.unit_response(identity)
        ):
            result = module.SystemBackend(Path(self.tmp.name)).reconcile(
                {"id": identity}
            )
        assert result["status"] == "interrupted"

    def test_failed_started_receipt_after_spawn_cannot_repeat_the_attempt(self) -> None:
        identity = "f" * 20
        directory = Path(self.tmp.name) / "incidents" / identity
        directory.mkdir(parents=True)
        write_json = module.atomic_json

        def fail_started_receipt(path: Path, value: dict) -> None:
            if value.get("status") == "started":
                message = "injected receipt write failure"
                raise OSError(message)
            write_json(path, value)

        process = MagicMock()
        process.pid = 456
        process.__enter__.return_value = process
        process.__exit__.return_value = False
        failure = None
        with (
            patch.object(module, "atomic_json", side_effect=fail_started_receipt),
            patch.object(module.subprocess, "Popen", return_value=process) as spawn,
        ):
            try:
                module.run_omp_process(directory, ["mocked-native-wrapper"])
            except OSError as error:
                failure = error
        assert failure is not None
        assert spawn.call_count == 1
        assert (
            json.loads((directory / "host_receipt.json").read_text())["status"]
            == "launching"
        )
        with patch.object(
            module.subprocess, "run", return_value=self.unit_response(identity)
        ) as run:
            result = module.SystemBackend(Path(self.tmp.name)).launch(
                {"id": identity, "actions": []}, snapshot()
            )
        assert result["status"] == "interrupted"
        assert run.call_count == 1
        assert run.call_args.args[0][:3] == ["/usr/bin/systemctl", "--user", "show"]

    def test_pre_session_cancellation_marker_alone_blocks_launch(self) -> None:
        identity = "e" * 20
        directory = Path(self.tmp.name) / "incidents" / identity
        directory.mkdir(parents=True)
        (directory / "cancelled").touch()
        with patch.object(
            module.subprocess, "run", return_value=self.unit_response(identity)
        ) as run:
            result = module.SystemBackend(Path(self.tmp.name)).launch(
                {"id": identity, "actions": []}, snapshot()
            )
        assert result["status"] == "interrupted"
        assert run.call_count == 1
        assert run.call_args.args[0][:3] == ["/usr/bin/systemctl", "--user", "show"]
        assert not (directory / "host_receipt.json").exists()

    def test_pre_session_cancellation_is_persisted_and_never_dispatched(self) -> None:
        identity = "d" * 20
        directory = Path(self.tmp.name) / "incidents" / identity
        directory.mkdir(parents=True)
        (directory / "cancelled").touch()
        with (
            patch.object(module, "inside_host_unit", return_value=True),
            patch.object(module.subprocess, "Popen") as spawn,
        ):
            result = module.host_incident(Path(self.tmp.name), identity)
        assert result["status"] == "cancelled"
        assert spawn.call_count == 0
        assert (
            json.loads((directory / "host_receipt.json").read_text())["status"]
            == "cancelled"
        )
        with patch.object(
            module.subprocess, "run", return_value=self.unit_response(identity)
        ) as run:
            result = module.SystemBackend(Path(self.tmp.name)).launch(
                {"id": identity, "actions": []}, snapshot()
            )
        assert result["status"] == "interrupted"
        assert run.call_count == 1

    def test_remote_probe_transport_status_and_ssh_auth_are_preserved(self) -> None:
        refused = subprocess.CalledProcessError(
            1, "ssh", output=json.dumps({"status": "deferred", "error": "URLError"})
        )
        configuration = subprocess.CalledProcessError(
            1, "ssh", output=json.dumps({"status": "blocked", "error": "RuntimeError"})
        )
        ssh_auth = subprocess.CalledProcessError(
            255, "ssh", stderr="ultra@odroid: Permission denied (publickey)."
        )
        auth_limit = subprocess.CalledProcessError(
            255, "ssh", stderr="Received disconnect: Too many authentication failures"
        )
        optional_identity = subprocess.CalledProcessError(
            255,
            "ssh",
            stderr="no such identity: missing-key: No such file\nConnection reset by peer",
        )
        for error, expected in (
            (refused, "deferred"),
            (configuration, "blocked"),
            (ssh_auth, "blocked"),
            (auth_limit, "blocked"),
            (optional_identity, "deferred"),
        ):
            with self.subTest(expected=expected, stderr=error.stderr):
                assert module.operation_failure(error)["status"] == expected

    def test_fresh_remote_budget_covers_http_work_and_records_completion(self) -> None:
        backend = module.SystemBackend(Path(self.tmp.name))
        with (
            patch.object(
                backend,
                "_remote",
                return_value=subprocess.CompletedProcess([], 0, json.dumps(snapshot())),
            ) as remote,
            patch.object(module.time, "time", return_value=2076),
        ):
            result = backend.refresh()
        assert remote.call_args.kwargs["timeout"] >= 76 + 8
        assert result["request_completed_at"] == 2076

    def test_short_recovery_window_does_not_launch(self) -> None:
        self.controller.tick(snapshot(), 2000)
        self.controller.tick(snapshot(), 2300)
        self.controller.tick(snapshot(reason="recovering"), 2600)
        assert "launch" not in self.backend.actions
        self.controller.tick(snapshot(reason="recovering", valid=True), 2900)
        assert "launch" not in self.backend.actions


if __name__ == "__main__":
    unittest.main()
