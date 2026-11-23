# Copyright (c) 2026 Alex Mextner
"""Regression coverage for smart-home health, repair dispatch and speech policy."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/host"))
import ha_smart_home_probe as probe  # noqa: E402 - standalone script import path.
import ha_smart_home_recovery as recovery  # noqa: E402 - standalone script import path.


def snapshot(now, failures=()) -> dict:
    return {
        "observed_at": now,
        "complete": True,
        "checks": [
            {"id": "ha.api", "ok": "ha.api" not in failures},
            {"id": "ma.api", "ok": "ma.api" not in failures},
            {"id": "ma.provider.hass", "ok": "ma.provider.hass" not in failures},
        ],
        "speech": {"present": True, "observed_at": now},
    }


class Backend:
    """Record external effects for focused state-machine regression tests."""

    def __init__(self) -> None:
        self.status = "absent"
        self.launches = []
        self.speeches = []
        self.reloads = 0
        self.speech_fails = False
        self.launch_fails = False

    def reconcile(self, incident) -> dict:
        return {"status": self.status}

    def publish(self, incident, evidence) -> None:
        pass

    def launch(self, incident) -> dict:
        self.launches.append(incident["id"])
        if self.launch_fails:
            raise TimeoutError
        self.status = "running"
        return {"status": "scheduled"}

    def announce(self, incident, evidence) -> dict:
        self.speeches.append(incident["id"])
        if self.speech_fails:
            raise TimeoutError
        return {"status": "accepted"}

    def reconnect(self) -> dict:
        self.reloads += 1
        return {"status": "requested"}


class RecoveryTests(unittest.TestCase):
    """Verify durable incidents and bounded repair dispatch."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.backend = Backend()
        self.controller = recovery.Controller(self.path, self.backend, debounce=120)

    def tick(self, now, failures=("ha.api",)) -> dict:
        return self.controller.tick(snapshot(now, failures), now)

    def test_persistent_fault_dispatches_once_and_restart_reconciles(self) -> None:
        self.assertEqual(self.tick(1000)["status"], "debouncing")
        self.tick(1119)
        self.assertEqual(self.backend.launches, [])
        self.tick(1120)
        self.assertEqual(len(self.backend.launches), 1)
        self.controller = recovery.Controller(self.path, self.backend, debounce=120)
        self.tick(1200)
        self.assertEqual(len(self.backend.launches), 1)
        self.assertEqual(len(self.backend.speeches), 1)

    def test_two_fresh_good_samples_close_even_if_agent_still_running(self) -> None:
        self.tick(1000)
        self.tick(1120)
        self.assertEqual(self.tick(1130, ())["status"], "confirming_recovery")
        self.assertEqual(self.tick(1190, ())["status"], "healthy")
        state = json.loads(self.path.read_text())
        self.assertIsNone(state["active"])

    def test_repeated_same_good_observation_cannot_confirm_recovery(self) -> None:
        self.tick(1000)
        good = snapshot(1120)
        self.controller.tick(good, 1120)
        self.assertNotEqual(self.controller.tick(good, 1130)["status"], "healthy")

    def test_stale_or_incomplete_probe_cannot_close_incident(self) -> None:
        self.tick(1000)
        self.tick(1120)
        stale = snapshot(1000)
        self.assertNotEqual(self.controller.tick(stale, 1500)["status"], "healthy")
        incomplete = snapshot(1501)
        incomplete["complete"] = False
        self.assertNotEqual(self.controller.tick(incomplete, 1501)["status"], "healthy")

    def test_speech_failure_does_not_block_dispatch_or_repeat(self) -> None:
        self.backend.speech_fails = True
        self.tick(1000)
        self.tick(1120)
        self.tick(1300)
        self.assertEqual(len(self.backend.launches), 1)
        self.assertEqual(len(self.backend.speeches), 1)

    def test_ambiguous_dispatch_is_reconciled_before_retry(self) -> None:
        self.backend.launch_fails = True
        self.tick(1000)
        self.tick(1120)
        self.backend.status = "unknown"
        self.tick(2000)
        self.assertEqual(len(self.backend.launches), 1)
        self.backend.status = "running"
        self.tick(2500)
        self.assertEqual(len(self.backend.launches), 1)

    def test_dead_agent_retries_same_incident_after_backoff(self) -> None:
        self.tick(1000)
        self.tick(1120)
        self.backend.status = "finished"
        self.tick(1200)
        self.assertEqual(len(self.backend.launches), 1)
        self.tick(1800)
        self.assertEqual(len(self.backend.launches), 2)
        self.assertEqual(self.backend.launches[0], self.backend.launches[1])

    def test_one_provider_reconnect_then_escalation(self) -> None:
        self.tick(1000, ("ma.provider.hass",))
        self.assertEqual(
            self.tick(1120, ("ma.provider.hass",))["status"], "reconnect_requested"
        )
        self.assertEqual(self.backend.reloads, 1)
        self.tick(1180, ("ma.provider.hass",))
        self.assertEqual(len(self.backend.launches), 1)
        self.tick(2000, ("ma.provider.hass",))
        self.assertEqual(self.backend.reloads, 1)

    def test_ssh_failure_becomes_monitor_incident(self) -> None:
        failed = recovery.failed_observation(1000, "ssh_unreachable")
        self.controller.tick(failed, 1000)
        self.controller.tick(recovery.failed_observation(1120, "ssh_unreachable"), 1120)
        self.assertEqual(len(self.backend.launches), 1)

    def test_global_speech_cooldown_across_distinct_incidents(self) -> None:
        self.tick(1000)
        self.tick(1120)
        self.tick(1180, ())
        self.tick(1240, ())
        self.backend.status = "absent"
        self.tick(1300)
        self.tick(1420)
        self.assertEqual(len(self.backend.speeches), 1)

    def test_corrupt_state_preserved_and_live_worker_prevents_new_dispatch(
        self,
    ) -> None:
        self.path.write_text("{broken")
        self.backend.status = "running"
        self.tick(1000)
        self.tick(1120)
        self.assertEqual(self.backend.launches, [])
        self.assertEqual(len(list(self.path.parent.glob("state.json.corrupt-*"))), 1)

    def test_missing_required_check_is_not_healthy(self) -> None:
        value = snapshot(1000)
        value["checks"] = [{"id": "ha.api", "ok": True}]
        self.assertEqual(self.controller.tick(value, 1000)["status"], "debouncing")

    def test_suffixed_hass_player_provider_gets_one_reconnect(self) -> None:
        for now in (1000, 1120):
            value = snapshot(now)
            value["checks"].append(
                {
                    "id": "ma.provider.hass_players--abc",
                    "domain": "hass_players",
                    "ok": False,
                }
            )
            result = self.controller.tick(value, now)
        self.assertEqual(result["status"], "reconnect_requested")
        self.assertEqual(self.backend.reloads, 1)

    def test_previous_incident_worker_blocks_a_second_systemd_unit(self) -> None:
        backend = recovery.SystemBackend(Path(self.temp.name))
        own = {"id": "a" * 20}
        replies = [
            SimpleNamespace(returncode=4, stdout="LoadState=not-found\n"),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [{"unit": "ha-smart-home-repair-" + "b" * 20 + ".service"}]
                ),
            ),
        ]
        with patch.object(recovery.subprocess, "run", side_effect=replies):
            result = backend.reconcile(own)
        self.assertEqual(result["status"], "running")
        self.assertIn("b" * 20, result["other_worker"])

    def test_valid_json_with_malformed_active_incident_is_quarantined(self) -> None:
        identity = "a" * 20
        for broken in (
            [],
            {
                "id": identity,
                "first_seen": "bad",
                "next_attempt_at": 0,
                "attempts": 0,
                "actions": [],
            },
        ):
            self.path.write_text(
                json.dumps(
                    {"version": 1, "active": identity, "incidents": {identity: broken}}
                )
            )
            self.backend.status = "running"
            result = self.tick(1000)
            self.assertEqual(result["status"], "debouncing")
        self.assertEqual(self.backend.launches, [])


class ProbeTests(unittest.TestCase):
    """Verify service health classification and speech policy."""

    def test_disabled_provider_ignored_and_enabled_missing_provider_failed(
        self,
    ) -> None:
        checks = probe.provider_checks(
            [
                {
                    "domain": "hass",
                    "instance_id": "hass",
                    "enabled": True,
                    "last_error": None,
                },
                {
                    "domain": "hass_players",
                    "instance_id": "hass_players",
                    "enabled": False,
                    "last_error": "old",
                },
            ],
            [],
        )
        self.assertEqual(
            checks,
            [
                {
                    "id": "ma.provider.hass",
                    "domain": "hass",
                    "ok": False,
                    "reason": "not_loaded",
                }
            ],
        )

    def test_provider_exception_is_safe_code_not_arbitrary_raw_log(self) -> None:
        checks = probe.provider_checks(
            [
                {
                    "domain": "hass",
                    "instance_id": "hass",
                    "enabled": True,
                    "last_error": {"message": "secret value"},
                }
            ],
            [],
        )
        self.assertNotIn("secret value", json.dumps(checks))
        self.assertFalse(checks[0]["ok"])

    def test_missing_expected_provider_is_visible_even_with_other_providers(
        self,
    ) -> None:
        checks = probe.provider_checks(
            [{"domain": "sendspin", "instance_id": "sendspin", "enabled": True}], []
        )
        self.assertEqual(
            {c["id"] for c in checks if not c["ok"]},
            {"ma.provider.hass", "ma.provider.hass_players"},
        )

    def test_current_unchanged_home_and_available_tracker_are_valid_presence(
        self,
    ) -> None:
        states = [
            {
                "entity_id": "person.ultra",
                "state": "home",
                "last_changed": "2000-01-01",
                "attributes": {"source": "device_tracker.phone"},
            },
            {"entity_id": "device_tracker.phone", "state": "home", "attributes": {}},
            {
                "entity_id": probe.DEFAULTS["speaker"],
                "state": "idle",
                "attributes": {"is_volume_muted": False, "volume_level": 0.4},
            },
        ]
        self.assertTrue(probe.speech_state(states, probe.DEFAULTS, 1000)["present"])
        states[1]["state"] = "unavailable"
        self.assertFalse(probe.speech_state(states, probe.DEFAULTS, 1000)["present"])

    def test_night_stale_presence_and_mute_suppress_speech(self) -> None:
        current = {
            "present": True,
            "observed_at": 1000,
            "speaker": {"state": "idle", "muted": False, "volume": 0.4},
        }
        self.assertEqual(probe.speech_allowed(current, 1000, local_hour=12), None)
        self.assertEqual(
            probe.speech_allowed(current, 1000, local_hour=23), "quiet_hours"
        )
        self.assertEqual(
            probe.speech_allowed(current, 1400, local_hour=12), "stale_presence"
        )
        current["speaker"]["muted"] = True
        self.assertEqual(probe.speech_allowed(current, 1000, local_hour=12), "muted")

    def test_native_speech_uses_playback_dependent_temporary_volume(self) -> None:
        current = {
            "present": True,
            "observed_at": 1000,
            "speaker": {"state": "playing", "muted": False, "volume": 0.4},
        }
        self.assertEqual(probe.speech_allowed(current, 1000, local_hour=12), None)
        current["playback_active"] = True
        self.assertEqual(
            probe.announcement_payload(probe.DEFAULTS, current)["extra"],
            {"volume_level": 0.6},
        )
        current["playback_active"] = False
        self.assertEqual(
            probe.announcement_payload(probe.DEFAULTS, current)["extra"],
            {"volume_level": 0.4},
        )


if __name__ == "__main__":
    unittest.main()
