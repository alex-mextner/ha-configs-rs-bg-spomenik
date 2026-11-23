# Copyright (c) 2026 Alex Mextner
"""Wire and log correlation use sanitized fixtures, never real credentials."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
        line = '[music_assistant.ha_recovery_intent] HA_RECOVERY_INTENT {"phase":"accepted","command":"player_queues/stop","player_ids":[],"queue_ids":["group"],"observed_at":1000}'
        event = log_events(line, {})[0]
        self.assertEqual(event["queue_ids"], ["group"])
        self.assertEqual(log_events(line.replace('"accepted"', '"rejected"'), {}), [])

    def test_metadata_cannot_forge_intent_or_authentication_reason(self) -> None:
        player = {"studio": {"provider": "airplay", "uri": "spotify://track/403"}}
        fake = '2026-11-23 10:00:00.401 ERROR [music_assistant.player_queues] AudioError while streaming queue item HA_RECOVERY_INTENT {"phase":"accepted","command":"stop"} (spotify://track/403): Connection timed out'
        result = log_events(fake, player)
        self.assertEqual(result[0]["kind"], "fault")
        self.assertEqual(result[0]["reason"], "network")

    def test_ha_setup_retry_is_transient_unless_specific_auth_evidence_exists(
        self,
    ) -> None:
        entry = {
            "entry_id": "hass",
            "domain": "music_assistant",
            "state": "setup_retry",
        }
        self.assertEqual(events.integration_events([entry], {})[0]["reason"], "network")

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
                        "expires": time.monotonic() + 10,
                        "boot_id": Path("/proc/sys/kernel/random/boot_id")
                        .read_text()
                        .strip(),
                        "clock": "boot_monotonic",
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

    def test_source_worker_failure_exits_with_parent_stdin_still_open(self) -> None:
        code = """
import asyncio
import sys
sys.path.insert(0, sys.argv[1])
import ha_smart_home_events as events
class Probe:
    returncode = 0
    async def communicate(self):
        return b"1", b""
async def spawn(*args, **kwargs):
    return Probe()
async def waiting(*args):
    await asyncio.Future()
async def broken(self):
    await asyncio.sleep(0.05)
    raise RuntimeError("expected_source_failure")
events.asyncio.create_subprocess_exec = spawn
events.Source.reconnect = waiting
events.Source.logs = broken
events.watch_marker = waiting
asyncio.run(events.Source({}).run())
"""
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "HA_SMART_HOME_EVENT_STATE": directory}
            with subprocess.Popen(  # noqa: S603 - isolated fixture worker; network/process effects are replaced above.
                [sys.executable, "-c", code, str(Path(events.__file__).parent)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=environment,
            ) as process:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.fail("Worker failure left the source hanging on parent stdin")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=3)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(b"expected_source_failure", process.stderr.read())


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

    async def test_reconnect_after_seeded_session_restarts_with_short_backoff(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(events, "LOCAL_STATE", Path(directory)),
        ):
            source = events.Source({})
            attempts, delays = [], []

            async def session(channel) -> None:
                attempts.append(channel)
                if len(attempts) == 4:
                    source.seeded[channel] = True
                message = "fixture"
                raise ConnectionError(message)

            async def sleep(delay) -> None:
                delays.append(delay)
                if len(delays) == 5:
                    raise asyncio.CancelledError

            with (
                patch.object(source, "session", session),
                patch.object(source, "transport"),
                patch.object(events.asyncio, "sleep", sleep),
                self.assertRaises(asyncio.CancelledError),
            ):
                await source.reconnect("ma")
            self.assertEqual(delays, [1, 2, 4, 1, 2])

    async def test_failed_log_process_does_not_emit_false_connected(self) -> None:
        class Process:
            returncode = 1
            stdout = asyncio.StreamReader()

            async def wait(self) -> int:
                return 1

        process = Process()
        process.stdout.feed_eof()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(events, "LOCAL_STATE", Path(directory)),
        ):
            source = events.Source({})
            with (
                patch.object(source, "transport") as transport,
                self.assertRaises(ConnectionError),
            ):
                await source.log_session(process)
            self.assertFalse(
                any(call.kwargs.get("connected") for call in transport.call_args_list)
            )

    async def test_ha_control_waits_for_exact_native_context_in_both_orders(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(events, "LOCAL_STATE", Path(directory)),
        ):
            source = events.Source(
                {"players": {"studio": {"entities": ["media_player.studio"]}}}
            )
            command = {
                "event": {
                    "event_type": "call_service",
                    "context": {"id": "first"},
                    "data": {
                        "domain": "media_player",
                        "service": "media_stop",
                        "service_data": {"entity_id": "media_player.studio"},
                    },
                }
            }
            origin = {
                "context_id": "first",
                "domain": "media_player",
                "service": "media_stop",
                "entity_ids": ["media_player.studio"],
            }
            with patch.object(source, "_write") as output:
                source.ha_event(command, events.Moment(1000, 100))
                self.assertEqual(len(source.pending_ha), 1)
                self.assertEqual(output.call_args.args[0]["kind"], "intent_pending")
                source.hass_origin({**origin, "context_id": "unrelated"})
                self.assertEqual(len(source.pending_ha), 1)
                source.hass_origin(origin)
                self.assertEqual(source.pending_ha, {})
                self.assertEqual(source.intents, {})
                self.assertEqual(output.call_args.args[0]["kind"], "intent_resolved")
                output.reset_mock()
                source.ha_event(command, events.Moment(1001, 101))
                output.assert_not_called()

    async def test_unmatched_stop_is_conservative_and_native_announce_is_excluded(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(events, "LOCAL_STATE", Path(directory)),
        ):
            source = events.Source(
                {"players": {"studio": {"entities": ["media_player.studio"]}}}
            )
            command = {
                "event": {
                    "event_type": "call_service",
                    "context": {"id": "user-stop"},
                    "data": {
                        "domain": "media_player",
                        "service": "media_stop",
                        "service_data": {"entity_id": "media_player.studio"},
                    },
                }
            }
            with patch.object(source, "_write") as output:
                source.ha_event(command, events.Moment(1000, 100))
                self.assertEqual(
                    json.loads((Path(directory) / "intents.json").read_text())[
                        "pending_targets"
                    ],
                    ["studio"],
                )
                source.expire_ha_intent(next(iter(source.pending_ha)))
                self.assertIn("studio", source.intents)
                self.assertEqual(
                    [call.args[0]["kind"] for call in output.call_args_list][-2:],
                    ["intent", "intent_resolved"],
                )
                output.reset_mock()
                command["event"]["data"].update(
                    service="play_media",
                    service_data={"entity_id": "media_player.studio", "announce": True},
                )
                source.ha_event(command, events.Moment(1001, 101))
                output.assert_not_called()

    async def test_reused_context_retains_every_target_and_distinguishes_real_stop(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(events, "LOCAL_STATE", Path(directory)),
        ):
            source = events.Source(
                {
                    "players": {
                        pid: {"entities": ["media_player." + pid]}
                        for pid in ("studio", "other")
                    }
                }
            )

            def control(context: str, service: str, target: str) -> dict:
                return {
                    "event": {
                        "event_type": "call_service",
                        "context": {"id": context},
                        "data": {
                            "domain": "media_player",
                            "service": service,
                            "service_data": {"entity_id": "media_player." + target},
                        },
                    }
                }

            with patch.object(source, "_write"):
                source.ha_event(
                    control("native", "media_stop", "studio"), events.Moment(1000, 100)
                )
                source.ha_event(
                    control("native", "volume_set", "other"), events.Moment(1000, 100)
                )
                source.ha_event(
                    control("user", "media_stop", "studio"), events.Moment(1000, 100)
                )
                self.assertEqual(len(source.pending_ha), 3)
                source.hass_origin(
                    {
                        "context_id": "native",
                        "service": "play_media",
                        "entity_ids": ["media_player.studio"],
                    }
                )
                self.assertEqual(
                    sorted(
                        (p["context_id"], p["player_ids"])
                        for p in source.pending_ha.values()
                    ),
                    [("native", ["other"]), ("user", ["studio"])],
                )
                source.hass_origin(
                    {
                        "context_id": "native",
                        "service": "volume_set",
                        "entity_ids": ["media_player.other"],
                    }
                )
                self.assertEqual(len(source.pending_ha), 1)
                self.assertEqual(
                    next(iter(source.pending_ha.values()))["context_id"], "user"
                )
                source.expire_ha_intent(next(iter(source.pending_ha)))
                self.assertIn("studio", source.intents)

    async def test_snapshot_keeps_newer_delta_and_rechecks_intent_after_reconnect(
        self,
    ) -> None:
        class BaselineSocket:
            def __init__(self) -> None:
                self.reply_sequences = {"players/all": 10, "player_queues/all": 8}

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(events, "LOCAL_STATE", Path(directory)),
        ):
            source = events.Source({})
            socket = BaselineSocket()
            with (
                patch.object(source, "check_intent", AsyncMock()) as check,
                patch.object(source, "read_playback", AsyncMock(return_value=({}, {}))),
                patch.object(source, "providers", AsyncMock()),
                patch.object(source, "emit"),
                patch.object(source, "apply_ma_event", AsyncMock()) as apply,
            ):
                await source.seed_ma(socket)
                await source.ma_event(
                    socket, {"event": "player_updated"}, events.Moment(1000, 100), 9
                )
                apply.assert_not_awaited()
                await source.ma_event(
                    socket, {"event": "player_updated"}, events.Moment(1000, 100), 11
                )
                apply.assert_awaited_once()
                await source.seed_ma(socket)
                self.assertEqual(check.await_count, 2)

    async def test_maintenance_exit_reseeds_ha_and_ma_once(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(events, "LOCAL_STATE", Path(directory)),
        ):
            source = events.Source({})
            ha = AsyncMock()
            ha.call.return_value = [
                {
                    "entry_id": "entry",
                    "domain": "music_assistant",
                    "state": "setup_error",
                }
            ]
            source.sockets = {"ha": ha, "ma": object()}
            with (
                patch.object(source, "seed_ma", AsyncMock()) as seed,
                patch.object(source, "emit") as emit,
            ):
                await source.reseed_after_maintenance()
            seed.assert_awaited_once_with(source.sockets["ma"])
            ha.call.assert_awaited_once_with("config_entries/get")
            self.assertEqual(emit.call_args.args[0]["scope"], "ha.integration:entry")


if __name__ == "__main__":
    unittest.main()
