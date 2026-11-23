# Copyright (c) 2026 Alex Mextner
"""Wire and log correlation use sanitized fixtures, never real credentials."""

import asyncio
import hashlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/host"))
import ha_smart_home_events as events
from ha_smart_home_common import normalize_player
from ha_smart_home_events import Socket, log_events, provider_events


class WireTests(unittest.TestCase):
    """Exercise the scoped event contract without household effects."""

    def test_group_queue_keeps_member_identity_and_uri_is_not_exported(self) -> None:
        raw = {
            "player_id": "studio",
            "provider": "universal_player",
            "active_source": "group",
            "current_media": {
                "uri": "secret://track?token=secret",
                "queue_item_id": "track1",
            },
            "group_members": ["studio", "other"],
            "playback_state": "playing",
        }
        clean = normalize_player(raw, {"group": {"item_id": "track1"}}, 1000)
        self.assertEqual((clean["id"], clean["queue_id"]), ("studio", "group"))
        self.assertNotIn("token", str(clean))

    def test_real_current_stream_error_matches_only_its_player_and_redacts_uri(
        self,
    ) -> None:
        players = {
            "studio": {
                "provider": "airplay",
                "uri": "spotify://track/123?token=secret",
            },
            "other": {"provider": "airplay", "uri": "spotify://track/456"},
        }
        result = log_events(
            "AudioError while streaming queue item Song (spotify://track/123?token=secret): Connection timed out",
            players,
        )
        self.assertEqual(result[0]["player_ids"], ["studio"])
        self.assertEqual(result[0]["reason"], "network")
        self.assertNotIn("secret", str(result))

    def test_background_prefetch_and_unrelated_error_do_not_blame_playing_speaker(
        self,
    ) -> None:
        players = {"studio": {"provider": "airplay", "uri": "spotify://track/123"}}
        self.assertEqual(
            log_events("Audio analysis error spotify://track/123 timeout", players), []
        )
        self.assertEqual(
            log_events(
                "AudioError while streaming queue item Other (spotify://track/999): timeout",
                players,
            ),
            [],
        )

    def test_intent_log_is_minimal_and_rejected_command_is_not_accepted(self) -> None:
        line = 'HA_RECOVERY_INTENT {"phase":"accepted","command":"player_queues/stop","player_ids":[],"queue_ids":["group"],"observed_at":1000}'
        event = log_events(line, {})[0]
        self.assertEqual(event["queue_ids"], ["group"])
        self.assertEqual(log_events(line.replace('"accepted"', '"rejected"'), {}), [])

    def test_disabled_provider_and_error_free_unloaded_provider_do_not_report_config_fault(
        self,
    ) -> None:
        configs = [
            {
                "instance_id": "hass",
                "domain": "hass",
                "enabled": False,
                "last_error": "bad config",
            },
            {"instance_id": "airplay", "enabled": True, "last_error": None},
        ]
        self.assertFalse(any(not e.get("recovered") for e in provider_events(configs)))

    def test_enabled_suffixed_provider_has_precise_reason(self) -> None:
        result = provider_events(
            [
                {
                    "instance_id": "hass_players--42",
                    "domain": "hass_players",
                    "enabled": True,
                    "last_error": "Authentication failed",
                }
            ]
        )
        self.assertEqual(result[0]["scope"], "provider:hass_players--42")
        self.assertEqual(result[0]["reason"], "auth")

    def test_malformed_provider_is_not_reported_as_healthy(self) -> None:
        result = provider_events([{"instance_id": "hass", "enabled": "yes"}])
        self.assertFalse(result[0]["recovered"])
        self.assertEqual(result[0]["reason"], "unknown")

    def test_only_exact_owned_resume_correlation_is_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "controls").mkdir()
            tag = "ha-recovery:" + "a" * 32
            (
                root / "controls" / (hashlib.sha256(tag.encode()).hexdigest() + ".json")
            ).write_text(
                json.dumps(
                    {
                        "origin_tag": tag,
                        "queue_id": "studio",
                        "expires": time.time() + 10,
                    }
                )
            )
            payload = {
                "command": "player_queues/resume",
                "origin_tag": tag,
                "queue_ids": ["studio"],
            }
            with patch.object(events, "LOCAL_STATE", root):
                self.assertTrue(events.Source.own_control(payload))
                self.assertFalse(
                    events.Source.own_control(
                        {**payload, "command": "player_queues/stop"}
                    )
                )
                self.assertFalse(
                    events.Source.own_control({**payload, "origin_tag": "unclaimed"})
                )

    def test_aliases_for_one_physical_speaker_do_not_duplicate_a_fault(self) -> None:
        same = {"id": "studio", "provider": "airplay", "uri": "spotify://same"}
        result = log_events(
            "AudioError while streaming queue item Song (spotify://same): timeout",
            {"studio": same, "airplay-child": same},
        )
        self.assertEqual([e["player_ids"] for e in result], [["studio"]])


class SocketReceiptTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the scoped event contract without household effects."""

    async def test_string_message_ids_and_partial_results_resolve_the_request(
        self,
    ) -> None:
        incoming = asyncio.Queue()

        class Wire:
            """Exercise the scoped event contract without household effects."""

            async def send(self, raw: str) -> None:
                msg = json.loads(raw)
                for partial, items in [(True, [1]), (False, [2])]:
                    await incoming.put(
                        json.dumps(
                            {
                                "message_id": str(msg["message_id"]),
                                "result": items,
                                "partial": partial,
                            }
                        )
                    )

            def __aiter__(self) -> object:
                return self

            async def __anext__(self) -> str:
                return await incoming.get()

        socket = Socket(Wire(), "ma")
        reader = asyncio.create_task(socket.read())
        try:
            result = await asyncio.wait_for(socket.call("players/all"), 1)
            self.assertEqual(result, [1, 2])
        finally:
            reader.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await reader


if __name__ == "__main__":
    unittest.main()
