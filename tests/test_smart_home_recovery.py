# Copyright (c) 2026 Alex Mextner
"""Playback interruption contracts: no physical players or audio are touched."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "scripts/host"
sys.path.insert(0, str(ROOT))
import ha_smart_home_recovery as recovery  # noqa: E402 - standalone sibling import.


def player(identity="studio", **overrides: object) -> dict:
    return {
        "id": identity,
        "provider": "airplay",
        "state": "playing",
        "available": True,
        "enabled": True,
        "queue_id": identity,
        "item_id": "track-1",
        "elapsed": 30,
        "duration": 240,
        "updated_at": 1000,
        "members": [],
        **overrides,
    }


class InterruptionTests(unittest.TestCase):
    """Exercise the scoped event contract without household effects."""

    def setUp(self) -> None:
        self.engine = recovery.Engine({})
        self.seq = 0
        self.send("snapshot", players=[player(), player("livingroom", state="idle")])

    def send(self, kind, at=1000, **payload: object) -> list:
        self.seq += 1
        event = {
            "kind": kind,
            "host": "odroidn2",
            "boot_id": "boot",
            "stream_id": "stream",
            "sequence": self.seq,
            "observed_at": at,
            "monotonic": at - 900,
            **payload,
        }
        return self.engine.feed(event, at)

    def broken(self, **extra: object) -> list:
        return self.send("player", player=player(available=False, **extra))

    def test_tentative_control_holds_audio_without_canceling_native_restore(
        self,
    ) -> None:
        self.broken()
        self.send("intent_pending", at=1001, context_id="native", player_ids=["studio"])
        actions = self.engine.due(1005)
        self.assertEqual([a["type"] for a in actions], ["publish"])
        self.assertIn("studio", self.engine.pending)
        actions = self.send("intent_resolved", at=1005.1, context_id="native")
        self.assertEqual([a["type"] for a in actions], ["announce"])
        self.assertIn("studio", self.engine.restore)

    def test_stop_while_origin_unresolved_cancels_and_new_epoch_drops_stale_hold(
        self,
    ) -> None:
        self.broken()
        self.send("intent_pending", at=1001, context_id="user", player_ids=["studio"])
        self.send("intent", at=1002, player_ids=["studio"], queue_ids=[])
        self.assertEqual(self.send("intent_resolved", at=1003, context_id="user"), [])
        self.assertEqual(self.engine.pending, {})
        self.assertEqual(self.engine.restore, {})
        self.send("intent_pending", at=1004, context_id="old", player_ids=["studio"])
        self.send("snapshot", at=1005, stream_id="new", players=[player()], queues=[])
        self.assertFalse(self.engine.intent_pending("studio"))

    def test_persistent_network_notifies_and_speaks_only_after_five_seconds(
        self,
    ) -> None:
        actions = self.broken()
        self.assertEqual(actions, [])
        self.assertEqual(self.engine.due(1004.999), [])
        speech = self.engine.due(1005)
        self.assertEqual(speech[0]["event"]["phase"], "opened")
        self.assertEqual(
            [
                (a["type"], a["player_id"], a["reason"])
                for a in speech
                if a["type"] == "announce"
            ],
            [("announce", "studio", "network")],
        )
        self.assertEqual(self.engine.due(1006), [])

    def test_quick_network_recovery_cancels_deadline(self) -> None:
        self.broken()
        self.send("player", at=1003, player=player(updated_at=1003))
        self.assertEqual(self.engine.due(1006), [])
        self.assertEqual(self.engine.state["outbox"], {})

    def test_reconnect_snapshot_closes_known_interruption_before_network_deadline(
        self,
    ) -> None:
        self.broken()
        self.send("snapshot", at=1003, stream_id="reconnected", players=[player()])
        self.assertEqual(self.engine.due(1005), [])
        self.assertEqual(self.engine.state["outbox"], {})
        self.assertIsNotNone(self.engine.state["incidents"]["player:studio"]["closed"])

    def test_reconnect_snapshot_reports_recovery_of_already_published_interruption(
        self,
    ) -> None:
        self.broken()
        self.engine.due(1005)
        actions = self.send(
            "snapshot", at=1006, stream_id="reconnected", players=[player()]
        )
        self.assertEqual([a["event"]["phase"] for a in actions], ["recovered"])

    def test_reconnected_idle_player_resumes_exact_prior_queue_once(self) -> None:
        self.broken(state="idle")
        self.engine.due(1005)
        actions = self.send("player", at=1006, player=player(state="idle"))
        resumed = [a for a in actions if a["type"] == "resume"]
        self.assertEqual(
            [(a["queue_id"], a["item_id"]) for a in resumed], [("studio", "track-1")]
        )
        self.assertEqual(self.engine.due(1007), [])

    def test_user_stop_while_offline_prevents_reconnect_resume(self) -> None:
        self.broken(state="idle")
        self.engine.due(1005)
        self.send("intent", at=1006, player_ids=["studio"], queue_ids=[])
        self.assertEqual(self.send("player", at=1007, player=player(state="idle")), [])
        self.assertEqual(self.engine.due(1008), [])

    def test_new_independent_interruption_is_not_suppressed_by_global_cooldown(
        self,
    ) -> None:
        self.broken()
        self.engine.due(1005)
        self.send("player", at=1006, player=player())
        self.send("player", at=1010, player=player(available=False))
        self.assertEqual(
            [a["type"] for a in self.engine.due(1015)], ["publish", "announce"]
        )

    def test_user_pause_idle_and_track_transition_are_silent(self) -> None:
        for state in ["paused", "idle", "buffering"]:
            self.assertEqual(self.send("player", player=player(state=state)), [])
        self.assertEqual(self.engine.due(1020), [])

    def test_user_stop_during_failure_cancels_pending_notice(self) -> None:
        self.broken()
        self.send(
            "intent", at=1002, player_ids=["studio"], queue_ids=[], command="media_stop"
        )
        self.assertEqual(self.engine.due(1005), [])

    def test_offline_player_followed_by_queue_idle_remains_interrupted(self) -> None:
        self.broken()
        self.send(
            "queue",
            at=1002,
            queue={"id": "studio", "state": "idle", "item_id": "track-1"},
        )
        self.assertEqual(
            [a["player_id"] for a in self.engine.due(1005) if a["type"] == "announce"],
            ["studio"],
        )

    def test_changed_queue_item_cancels_notice(self) -> None:
        self.broken()
        self.send(
            "queue",
            at=1001,
            queue={"id": "studio", "state": "playing", "item_id": "track-2"},
        )
        self.assertEqual(self.engine.due(1005), [])

    def test_queue_source_change_with_same_item_cancels_notice(self) -> None:
        self.broken()
        self.send(
            "player", at=1001, player=player(queue_id="other-group", available=False)
        )
        self.assertFalse(any(a["type"] == "announce" for a in self.engine.due(1005)))

    def test_natural_track_end_is_not_an_interruption(self) -> None:
        self.send("snapshot", players=[player(elapsed=238)])
        self.assertEqual(
            self.send("player", player=player(state="idle", elapsed=240)), []
        )
        self.assertEqual(self.engine.due(1006), [])

    def test_real_device_loss_near_track_end_remains_a_failure(self) -> None:
        self.send("snapshot", players=[player(elapsed=238)])
        self.broken()
        self.assertTrue(self.engine.due(1005))

    def test_later_auth_evidence_upgrades_pending_network_cause_immediately(
        self,
    ) -> None:
        self.broken()
        actions = self.send(
            "fault",
            at=1001,
            scope="provider:airplay",
            provider="airplay",
            reason="auth",
        )
        speech = [a for a in actions if a["type"] == "announce"]
        self.assertEqual([a["reason"] for a in speech], ["auth"])
        self.assertEqual(self.engine.due(1005), [])

    def test_late_queue_provider_upgrades_only_matching_interrupted_item(self) -> None:
        self.broken()
        self.send(
            "fault",
            at=1001,
            scope="provider:yandex_music--account",
            provider="yandex_music--account",
            domain="yandex_music",
            reason="auth",
        )
        self.assertEqual(
            self.send(
                "queue",
                at=1001.1,
                queue={
                    "id": "livingroom",
                    "item_id": "track-1",
                    "source_provider": "yandex_music--account",
                },
            ),
            [],
        )
        actions = self.send(
            "queue",
            at=1001.2,
            queue={
                "id": "studio",
                "item_id": "track-1",
                "source_provider": "yandex_music--account",
            },
        )
        self.assertEqual(
            [(a["player_id"], a["reason"]) for a in actions if a["type"] == "announce"],
            [("studio", "auth")],
        )
        self.assertEqual(self.engine.due(1005), [])

    def test_exact_music_provider_failure_and_recovery_use_retained_queue(self) -> None:
        self.send("snapshot", players=[player(source_provider="yandex_music--account")])
        self.send(
            "fault",
            scope="provider:yandex_music--account",
            provider="yandex_music--account",
            reason="auth",
        )
        actions = self.send(
            "player",
            at=1001,
            player=player(state="idle", source_provider="yandex_music--account"),
        )
        self.assertEqual(
            [a["reason"] for a in actions if a["type"] == "announce"], ["auth"]
        )
        actions = self.send(
            "fault",
            at=1006,
            scope="provider:yandex_music--account",
            provider="yandex_music--account",
            recovered=True,
        )
        self.assertEqual(
            [(a["queue_id"], a["item_id"]) for a in actions if a["type"] == "resume"],
            [("studio", "track-1")],
        )

    def test_snapshot_discards_obsolete_provider_classification(self) -> None:
        self.send("fault", scope="provider:airplay", provider="airplay", reason="auth")
        self.send("snapshot", players=[player()])
        self.assertEqual(self.broken(), [])
        self.assertEqual(
            [a["reason"] for a in self.engine.due(1005) if a["type"] == "announce"],
            ["network"],
        )

    def test_idle_player_and_reconnected_snapshot_never_speak(self) -> None:
        self.assertEqual(
            self.send(
                "player", player=player("livingroom", state="idle", available=False)
            ),
            [],
        )
        self.send("snapshot", players=[player(available=False)])
        self.assertEqual(self.engine.due(1010), [])

    def test_config_fault_dispatches_immediately_but_speaks_only_after_interruption(
        self,
    ) -> None:
        actions = self.send(
            "fault", scope="provider:airplay", provider="airplay", reason="auth"
        )
        self.assertEqual([a["type"] for a in actions], ["publish"])
        self.assertEqual(self.engine.due(1000), [])
        actions = self.send("player", player=player(state="idle", available=False))
        self.assertEqual(
            [
                (a["type"], a.get("player_id"))
                for a in actions
                if a["type"] == "announce"
            ],
            [("announce", "studio")],
        )

    def test_unrelated_provider_configuration_error_is_silent(self) -> None:
        self.send("fault", scope="provider:other", provider="other", reason="code")
        self.assertEqual(self.engine.due(1010), [])

    def test_own_announcement_is_neither_recovery_nor_new_interruption(self) -> None:
        self.broken()
        self.engine.due(1005)
        actions = self.send(
            "player", at=1006, player=player(announcement=True, item_id="our-clip")
        )
        self.assertEqual(actions, [])
        self.assertIsNone(self.engine.state["incidents"]["player:studio"]["closed"])

    def test_audio_source_error_after_idle_correlates_without_a_socket_outage(
        self,
    ) -> None:
        self.send("player", player=player(state="idle"))
        self.send(
            "fault",
            at=1001,
            scope="player:studio",
            provider="airplay",
            reason="network",
            playback_fault=True,
            player_ids=["studio"],
        )
        speech = [a for a in self.engine.due(1005) if a["type"] == "announce"]
        self.assertEqual([a["player_id"] for a in speech], ["studio"])

    def test_monitor_transport_loss_does_not_claim_playback_interrupted(self) -> None:
        self.send("transport", channel="ma", connected=False, reason="network")
        self.assertFalse(any(a["type"] == "announce" for a in self.engine.due(1010)))

    def test_group_queue_does_not_redirect_notice_to_leader(self) -> None:
        self.send(
            "snapshot",
            players=[
                player(queue_id="group", members=["studio", "livingroom"]),
                player("livingroom", queue_id="group"),
            ],
        )
        self.send("player", player=player(queue_id="group", available=False))
        self.assertEqual(
            [x["player_id"] for x in self.engine.due(1005) if x["type"] == "announce"],
            ["studio"],
        )

    def test_maintenance_suppresses_new_incidents(self) -> None:
        self.send("maintenance", active=True)
        self.assertEqual(self.broken(), [])
        self.assertEqual(self.engine.due(1010), [])

    def test_log_fault_clears_on_fresh_playing_and_does_not_taint_later_idle(
        self,
    ) -> None:
        self.send(
            "fault",
            scope="player:studio",
            provider="airplay",
            reason="network",
            player_ids=["studio"],
            playback_fault=True,
        )
        self.send("player", at=1001, player=player())
        self.send("player", at=1100, player=player(state="idle"))
        self.assertEqual(self.engine.due(1110), [])
        self.assertNotIn("player:studio", self.engine.faults)

    def test_idle_log_player_removal_is_not_a_configuration_incident(self) -> None:
        actions = self.send(
            "fault",
            scope="player:livingroom",
            reason="unknown",
            player_ids=["livingroom"],
            playback_fault=True,
        )
        self.assertEqual(actions, [])
        self.assertEqual(self.engine.due(1010), [])

    def test_speech_window_is_carried_in_source_monotonic_time(self) -> None:
        self.broken()
        action = next(a for a in self.engine.due(1005) if a["type"] == "announce")
        self.assertEqual(action["source_interrupted_at"], 100)
        self.assertEqual(action["source_deadline"], 105)
        self.assertEqual(action["source_not_after"], 108)

    def test_duplicate_stale_or_missing_sequence_never_announces(self) -> None:
        self.broken()
        self.assertEqual(
            self.engine.feed({"kind": "player", "observed_at": 900}, 1000), []
        )
        self.send("transport", channel="stream", connected=False, reason="network")
        self.assertFalse(any(a["type"] == "announce" for a in self.engine.due(1010)))

    def test_durable_incident_survives_restart_without_duplicate_speech(self) -> None:
        self.broken()
        self.engine.due(1005)
        fresh = recovery.Engine(self.engine.state)
        self.assertEqual(fresh.due(1100), [])

    def test_dead_subscriber_gets_new_revision_only_if_fresh_fault_remains(
        self,
    ) -> None:
        self.broken(state="idle")
        opened = self.engine.due(1005)[0]["event"]["event_id"]
        self.engine.state["subscriber_losses"]["loss"] = {"reconciled": False}
        evidence = {
            "request_id": "loss",
            "observed_at": 1006,
            "boot_id": "boot",
            "stream_id": "stream",
            "status": "fresh",
            "players": [player(state="idle", available=False)],
        }
        actions = self.engine.reconcile_subscriber_loss(evidence, 1006)
        self.assertEqual([a["event"]["phase"] for a in actions], ["updated"])
        self.assertNotEqual(actions[0]["event"]["event_id"], opened)
        self.assertEqual(self.engine.reconcile_subscriber_loss(evidence, 1006), [])

    def test_dead_subscriber_with_fresh_recovered_player_does_not_open_new_work(
        self,
    ) -> None:
        self.broken()
        self.engine.due(1005)
        self.engine.state["subscriber_losses"]["loss"] = {"reconciled": False}
        evidence = {
            "request_id": "loss",
            "observed_at": 1006,
            "boot_id": "boot",
            "stream_id": "stream",
            "status": "fresh",
            "players": [player()],
        }
        self.assertEqual(
            [
                a["event"]["phase"]
                for a in self.engine.reconcile_subscriber_loss(evidence, 1006)
            ],
            ["recovered"],
        )

    def test_indeterminate_loss_remains_pending_without_reissuing_decided_scope(
        self,
    ) -> None:
        self.broken()
        self.engine.due(1005)
        self.send(
            "fault", at=1005, scope="provider:radio", provider="radio", reason="code"
        )
        self.engine.state["subscriber_losses"]["loss"] = {"reconciled": False}
        evidence = {
            "request_id": "loss",
            "observed_at": 1006,
            "boot_id": "boot",
            "stream_id": "stream",
            "status": "fresh",
            "players": [player(available=False)],
            "faults": [],
        }
        first = self.engine.reconcile_subscriber_loss(evidence, 1006)
        self.assertEqual([a["event"]["scope"] for a in first], ["player:studio"])
        self.assertFalse(self.engine.state["subscriber_losses"]["loss"]["reconciled"])
        self.assertEqual(self.engine.reconcile_subscriber_loss(evidence, 1006), [])
        evidence["faults"] = [{"scope": "provider:radio", "recovered": False}]
        last = self.engine.reconcile_subscriber_loss(evidence, 1006)
        self.assertEqual([a["event"]["scope"] for a in last], ["provider:radio"])
        self.assertTrue(self.engine.state["subscriber_losses"]["loss"]["reconciled"])

    def test_subscriber_loss_during_intentional_maintenance_waits_for_fresh_postmaintenance_evidence(
        self,
    ) -> None:
        self.send("fault", scope="ma.provider.hass", reason="configuration")
        self.engine.state["subscriber_losses"]["loss"] = {"reconciled": False}
        self.send("maintenance", at=1001, active=True)
        evidence = {
            "request_id": "loss",
            "observed_at": 1002,
            "boot_id": "boot",
            "stream_id": "stream",
            "status": "fresh",
            "players": [],
            "maintenance": "active",
            "faults": [{"scope": "ma.provider.hass", "recovered": False}],
        }
        self.assertEqual(self.engine.reconcile_subscriber_loss(evidence, 1002), [])
        self.assertFalse(self.engine.state["subscriber_losses"]["loss"]["reconciled"])
        self.send("maintenance", at=1003, active=False)
        evidence.update(observed_at=1004, maintenance="absent")
        actions = self.engine.reconcile_subscriber_loss(evidence, 1004)
        self.assertEqual([a["event"]["phase"] for a in actions], ["updated"])


if __name__ == "__main__":
    unittest.main()
