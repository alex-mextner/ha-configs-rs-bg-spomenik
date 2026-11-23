# Copyright (c) 2026 Alex Mextner
"""Accepted user controls preserve existing authorization and private IDs."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "scripts/music-assistant-reconnect")
)
from typing import Never

import ha_recovery_intent as intent


class IntentTests(unittest.TestCase):
    """Exercise the scoped event contract without household effects."""

    def test_stop_pause_and_next_keep_queue_target_without_other_arguments(
        self,
    ) -> None:
        for name in ("stop", "pause", "next"):
            payload = intent.intent_payload(
                "player_queues/" + name,
                {"queue_id": "group", "uri": "https://secret?token=secret"},
                lambda _: SimpleNamespace(available=True, enabled=True),
            )
            self.assertEqual(payload["queue_ids"], ["group"])
            self.assertEqual(payload["player_ids"], [])
            self.assertNotIn("secret", str(payload))

    def test_native_announcement_and_non_control_commands_produce_no_intent(
        self,
    ) -> None:
        for command in (
            "players/cmd/play_announcement",
            "players/all",
            "player_queues/items",
        ):
            self.assertIsNone(
                intent.intent_payload(command, {"player_id": "studio"}, lambda _: None)
            )

    def test_missing_or_disabled_targets_produce_no_intent(self) -> None:
        for state in (
            None,
            SimpleNamespace(available=True, enabled=False),
        ):
            self.assertIsNone(
                intent.intent_payload(
                    "players/cmd/stop",
                    {"player_id": "studio"},
                    lambda _, state=state: state,
                )
            )

    def test_authorized_offline_stop_still_cancels_recovery(self) -> None:
        payload = intent.intent_payload(
            "players/cmd/stop",
            {"player_id": "studio"},
            lambda _: SimpleNamespace(available=False, enabled=True),
        )
        self.assertEqual(payload["player_ids"], ["studio"])

    def test_existing_permission_denial_is_not_converted_to_accepted_intent(
        self,
    ) -> None:
        def denied(_) -> Never:
            msg = "denied"
            raise PermissionError(msg)

        with self.assertRaises(PermissionError):
            intent.intent_payload("players/cmd/stop", {"player_id": "private"}, denied)


if __name__ == "__main__":
    unittest.main()
