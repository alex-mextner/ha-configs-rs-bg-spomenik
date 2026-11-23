# Copyright (c) 2026 Alex Mextner
"""No-audio acceptance for exact-target cached native announcements."""

import io
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/host"))
import ha_smart_home_probe as probe


class AnnouncementTests(unittest.TestCase):
    """Exercise the scoped event contract without household effects."""

    def setUp(self) -> None:
        self.now = datetime(2026, 11, 23, 12, tzinfo=UTC).timestamp()
        self.action = {
            "clock": "boot_monotonic",
            "player_id": "studio",
            "queue_id": "studio",
            "item_id": "track-1",
            "reason": "network",
            "interrupted_at": self.now - 5,
            "deadline": self.now,
            "not_after": self.now + 3,
        }
        self.player = {
            "player_id": "studio",
            "available": True,
            "enabled": True,
            "playback_state": "idle",
            "volume_muted": False,
            "volume_level": 40,
            "supported_features": ["play_announcement"],
            "group_members": [],
            "type": "player",
            "active_source": "studio",
            "current_media": {"queue_item_id": "track-1", "source_id": "studio"},
        }
        self.queue = {
            "queue_id": "studio",
            "state": "idle",
            "current_item": {"queue_item_id": "track-1"},
        }
        self.config = {"players": {"studio": {}}}

    def plan(self, **extra: object) -> dict | None:
        return probe.announcement_plan(
            self.action,
            {
                "player": self.player,
                "queue": self.queue,
                "intents": extra.get("intents", {}),
            },
            self.config,
            extra.get("now", self.now),
        )

    def test_network_uses_local_clip_and_exact_affected_player(self) -> None:
        plan = self.plan()
        self.assertEqual(plan["player_id"], "studio")
        self.assertEqual(plan["asset"], "network.ogg")
        self.assertFalse(plan["pre_announce"])
        self.assertEqual(plan["volume_level"], 40)

    def test_current_ma_control_volume_is_preserved_not_overridden_by_room_policy(
        self,
    ) -> None:
        self.player["volume_level"] = 52
        self.assertEqual(self.plan()["volume_level"], 52)

    def test_hardware_mute_is_fresh_exact_device_and_unknown_fails_quiet(self) -> None:
        configured = {"physical_id": "M00ZRW3008RAJG", "hardware_mute": True}
        with patch.object(
            probe,
            "hardware_state",
            side_effect=[{"muted": False}, {"muted": True}, None],
        ) as query:
            self.assertFalse(
                probe.active_mute_state(self.player, configured)["volume_muted"]
            )
            self.assertTrue(
                probe.active_mute_state(self.player, configured)["volume_muted"]
            )
            self.assertIsNone(
                probe.active_mute_state(self.player, configured)["volume_muted"]
            )
        self.assertEqual(query.call_count, 3)
        self.assertTrue(
            all(c.args == ("M00ZRW3008RAJG",) for c in query.call_args_list)
        )

    def test_resume_requires_original_idle_queue_and_no_new_user_intent(self) -> None:
        evidence = {"player": self.player, "queue": self.queue, "intents": {}}
        self.assertEqual(
            probe.resume_plan(self.action, evidence, self.config, self.now),
            {"queue_id": "studio"},
        )
        evidence["intents"]["studio"] = self.now
        self.assertIsNone(
            probe.resume_plan(self.action, evidence, self.config, self.now)
        )
        evidence["intents"] = {}
        self.player["active_source"] = "other"
        self.assertIsNone(
            probe.resume_plan(self.action, evidence, self.config, self.now)
        )

    def test_recovered_playback_is_silent(self) -> None:
        self.player["playback_state"] = "playing"
        self.assertIsNone(self.plan())

    def test_new_user_intent_wins_even_if_queue_still_matches(self) -> None:
        self.assertIsNone(self.plan(intents={"studio": self.now - 1}))

    def test_changed_or_missing_queue_item_is_silent(self) -> None:
        self.queue["current_item"] = {"queue_item_id": "other"}
        self.assertIsNone(self.plan())
        self.queue["current_item"] = None
        self.assertIsNone(self.plan())

    def test_unreachable_muted_unknown_and_group_fanout_are_silent(self) -> None:
        for field, value in [
            ("available", False),
            ("volume_muted", True),
            ("volume_muted", None),
            ("type", "group"),
            ("group_members", ["studio", "other"]),
            ("synced_to", "other"),
        ]:
            with self.subTest(field=field, value=value):
                old = self.player.get(field)
                self.player[field] = value
                self.assertIsNone(self.plan())
                self.player[field] = old

    def test_only_deadline_not_presence_or_wall_clock_gates_interrupted_playback(
        self,
    ) -> None:
        self.assertIsNone(self.plan(now=self.now + 4))
        self.config["quiet_hours"] = [0, 24]
        self.assertIsNotNone(self.plan())

    def test_reason_codes_do_not_claim_network_for_auth_or_code(self) -> None:
        for reason, asset in [
            ("auth", "connection.ogg"),
            ("configuration", "connection.ogg"),
            ("code", "service.ogg"),
            ("unknown", "service.ogg"),
        ]:
            self.action["reason"] = reason
            self.assertEqual(self.plan()["asset"], asset)

    def test_lost_submission_response_is_unknown_and_keeps_own_audio_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text("{}")
            output = io.StringIO()
            action = {**self.action, "claim": "incident:studio"}
            with (
                patch.object(probe, "LOCAL_STATE", root),
                patch.object(probe, "CONFIG", config),
                patch.object(probe, "init_log", return_value=root / "log"),
                patch.object(probe, "prepare", return_value={"player_id": "studio"}),
                patch.object(
                    probe, "ma_command", side_effect=TimeoutError("lost response")
                ),
                patch.object(sys, "argv", ["probe", "--execute"]),
                patch.object(sys, "stdin", io.StringIO(json.dumps(action))),
                patch.object(sys, "stdout", output),
            ):
                probe.main()
            self.assertEqual(json.loads(output.getvalue())["status"], "unknown")
            marker = json.loads(
                next((root / "announcements").glob("*.json")).read_text()
            )
            self.assertTrue(marker["active"])
            self.assertEqual(marker["clock"], "boot_monotonic")


if __name__ == "__main__":
    unittest.main()
