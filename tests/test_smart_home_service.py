# Copyright (c) 2026 Alex Mextner
"""Slow native agent startup must not stall event cancellation or deadlines."""

import asyncio
import contextlib
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/host"))
import ha_smart_home_common as common
import ha_smart_home_service as service
from ha_smart_home_recovery import Engine


class ClockMappingTests(unittest.TestCase):
    """Source uptime establishes ordering even when host wall clocks disagree."""

    def test_ahead_and_behind_source_wall_clocks_map_to_received_uptime(self) -> None:
        for wall_offset in (0.025, -0.025, 7200, -7200):
            with self.subTest(wall_offset=wall_offset):
                clock = service.SourceClock()
                event = {
                    "boot_id": "boot",
                    "stream_id": "stream",
                    "observed_at": 1000 + wall_offset,
                    "monotonic": 40,
                    "emitted_monotonic": 40.1,
                }
                normalized = clock.normalize(event, 1000.2, 90.2)
                self.assertAlmostEqual(normalized["observed_at"], 1000.1)
                self.assertEqual(normalized["source_monotonic"], 40)
                self.assertAlmostEqual(normalized["source_offset"], -960.1)

    def test_later_transport_delay_is_not_hidden_by_a_new_arrival_timestamp(
        self,
    ) -> None:
        clock = service.SourceClock()
        base = {
            "boot_id": "boot",
            "stream_id": "stream",
            "observed_at": 9000,
            "monotonic": 40,
            "emitted_monotonic": 40,
        }
        clock.normalize(base, 1000, 90)
        event = clock.normalize(
            {**base, "monotonic": 41, "emitted_monotonic": 41}, 1020, 110
        )
        self.assertEqual(event["observed_at"], 1001)
        self.assertEqual(event["source_offset"], -960)

    def test_source_intent_evidence_uses_the_same_monotonic_mapping(self) -> None:
        clock = service.SourceClock()
        event = clock.normalize(
            {
                "boot_id": "boot",
                "stream_id": "stream",
                "observed_at": 9000,
                "monotonic": 40,
                "emitted_monotonic": 40,
                "intents": {"studio": 39},
            },
            1000,
            90,
        )
        self.assertEqual(event["intents"], {"studio": 999})


class IndependentEffectsTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the scoped event contract without household effects."""

    async def test_stop_is_consumed_while_agent_delivery_is_still_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = service.Runtime(Path(root), Engine({}))
            blocked = asyncio.Event()
            started = asyncio.Event()

            async def waiting(*_args: object, **_kwargs: object) -> dict:
                started.set()
                await blocked.wait()
                return {"status": "delivered", "event_id": "incident:1"}

            event = {
                "event_id": "incident:1",
                "source": "smart-home",
                "phase": "opened",
            }
            with patch.object(service, "command_json", waiting):
                runtime.submit([{"type": "publish", "event": event}])
                await asyncio.wait_for(started.wait(), 1)
                now = time.time()
                runtime.handle(
                    "event",
                    {
                        "kind": "intent",
                        "host": "odroidn2",
                        "boot_id": "boot",
                        "stream_id": "stream",
                        "sequence": 1,
                        "observed_at": now,
                        "monotonic": now,
                        "emitted_monotonic": now,
                        "player_ids": ["studio"],
                        "queue_ids": [],
                    },
                )
                self.assertGreater(runtime.engine.intents["studio"], now)
                self.assertFalse(runtime.effects["incident:1"].done())
                blocked.set()
                await runtime.effects["incident:1"]
            if runtime.deadline:
                runtime.deadline.cancel()

    async def test_audio_and_delivery_are_started_independently_after_durable_claim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = service.Runtime(Path(root), Engine({}))
            blocked, announced = asyncio.Event(), asyncio.Event()

            async def publish(*_args: object, **_kwargs: object) -> dict:
                await blocked.wait()
                return {"status": "pending"}

            async def announce(_action) -> dict:
                self.assertTrue((Path(root) / "state.json").is_file())
                announced.set()
                return {"status": "suppressed"}

            with (
                patch.object(service, "command_json", publish),
                patch.object(runtime, "announce", announce),
            ):
                runtime.submit(
                    [
                        {"type": "publish", "event": {"event_id": "i:1"}},
                        {"type": "announce", "claim": "i:studio"},
                    ]
                )
                await asyncio.wait_for(announced.wait(), 1)
                self.assertFalse(runtime.effects["i:1"].done())
                blocked.set()
                await asyncio.gather(*runtime.effects.values())

    async def test_empty_local_outbox_still_reconciles_shared_spool_at_startup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = service.Runtime(Path(root), Engine({}))
            called = asyncio.Event()

            async def command(argv, *_args: object, **_kwargs: object) -> dict:
                self.assertEqual(argv[1], "reconcile")
                called.set()
                return {"receipts": [], "retry_at": None}

            async def stream() -> None:
                await asyncio.Event().wait()

            with (
                patch.object(service, "command_json", command),
                patch.object(runtime, "stream", stream),
                patch.object(runtime, "watch_subscriber", stream),
            ):
                task = asyncio.create_task(runtime.run())
                await asyncio.wait_for(called.wait(), 1)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def test_durable_pending_broker_receipt_does_not_republish(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = service.Runtime(Path(root), Engine({}))
            action = {"type": "publish", "event": {"event_id": "i:1"}}
            runtime.engine.state["outbox"]["i:1"] = action
            runtime.receipt(
                "i:1",
                action,
                {
                    "status": "pending",
                    "protocol_version": 1,
                    "event_id": "i:1",
                    "receipt_id": None,
                    "durable": True,
                    "source": "smart-home",
                    "digest": "a" * 64,
                    "retry_at": None,
                },
            )
            self.assertEqual(runtime.engine.state["outbox"], {})
            self.assertIsNone(runtime.retry_at)

    async def test_missing_durable_identity_retains_local_publication(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = service.Runtime(Path(root), Engine({}))
            action = {"type": "publish", "event": {"event_id": "i:1"}}
            runtime.engine.state["outbox"]["i:1"] = action
            runtime.receipt(
                "i:1",
                action,
                {
                    "status": "delivered",
                    "event_id": "other",
                    "durable": True,
                    "source": "smart-home",
                    "digest": "a" * 64,
                },
            )
            self.assertIn("i:1", runtime.engine.state["outbox"])
            self.assertIsNotNone(runtime.retry_at)

    def test_startup_vacancy_reconciles_open_work_without_native_ack(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = service.Runtime(Path(root), Engine({}))
            runtime.engine.state["incidents"]["provider:one"] = {
                "id": "known-open",
                "published": True,
                "closed": None,
            }
            event = {
                "protocol_version": 1,
                "source": "smart-home",
                "authority_id": "authority",
                "revision": "revision",
                "native_state": "unknown",
                "loss_pending": False,
            }
            runtime._shared_ready(event)
            self.assertEqual(runtime.engine.state["subscriber_losses"], {})
            event["native_state"] = "vacant"
            with patch.object(runtime, "request_refresh") as refresh:
                runtime._shared_ready(event)
                runtime._shared_ready(event)
            losses = runtime.engine.state["subscriber_losses"]
            self.assertEqual(len(losses), 1)
            self.assertEqual(refresh.call_count, 2)
            loss = next(iter(losses.values()))
            self.assertFalse(loss["native_ack_required"])
            loss["reconciled"] = True
            with patch.object(runtime, "submit") as submit:
                runtime.acknowledge_losses()
            submit.assert_called_once_with([])

    def test_retained_loss_and_unknown_protocol_never_synthesize_startup_loss(
        self,
    ) -> None:
        for version, loss_pending in ((1, True), (1, None), (2, False), (True, False)):
            with (
                self.subTest(version=version, loss_pending=loss_pending),
                tempfile.TemporaryDirectory() as root,
            ):
                runtime = service.Runtime(Path(root), Engine({}))
                runtime.engine.state["incidents"]["provider:one"] = {
                    "id": "known-open",
                    "published": True,
                    "closed": None,
                }
                runtime._shared_ready(
                    {
                        "protocol_version": version,
                        "source": "smart-home",
                        "authority_id": "authority",
                        "revision": "revision",
                        "native_state": "vacant",
                        "loss_pending": loss_pending,
                    }
                )
                self.assertEqual(runtime.engine.state["subscriber_losses"], {})

    def test_unknown_protocol_never_relinquishes_delivery_or_acknowledges_loss(
        self,
    ) -> None:
        receipt = {
            "status": "pending",
            "source": "smart-home",
            "event_id": "i:1",
            "digest": "a" * 64,
            "durable": True,
        }
        for version in (None, True, 2):
            self.assertFalse(
                common.durable_delivery(
                    {**receipt, "protocol_version": version}, "smart-home", "i:1"
                )
            )
        self.assertTrue(
            common.durable_delivery(
                {**receipt, "protocol_version": 1}, "smart-home", "i:1"
            )
        )
        with tempfile.TemporaryDirectory() as root:
            runtime = service.Runtime(Path(root), Engine({}))
            runtime.engine.state["subscriber_losses"]["loss"] = {"reconciled": True}
            runtime._ack_receipt(
                {"event_id": "loss"},
                {
                    "protocol_version": 2,
                    "status": "acknowledged",
                    "source": "smart-home",
                    "event_id": "loss",
                },
            )
            self.assertFalse(
                runtime.engine.state["subscriber_losses"]["loss"]["acknowledged"]
            )

    def test_durable_replace_syncs_directory_after_new_state_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            syncs = []

            def observe(fd: int) -> None:
                syncs.append((stat.S_ISDIR(os.fstat(fd).st_mode), path.exists()))

            with patch.object(common.os, "fsync", observe):
                common.atomic_json(path, {"decision": "persisted-before-ack"})
            self.assertEqual(syncs, [(False, False), (True, True)])

    def test_pending_loss_backoff_does_not_drop_another_inflight_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = service.Runtime(Path(root), Engine({}))
            runtime.engine.state["subscriber_losses"] = {
                key: {"observed_at": 99, "reconciled": False}
                for key in ("first", "second")
            }
            writer = unittest.mock.Mock()
            writer.is_closing.return_value = False
            runtime.source_writer = writer
            with patch.object(service.time, "time", return_value=100):
                runtime.request_refresh()
                self.assertEqual(writer.write.call_count, 2)
                runtime.requested_losses.pop("first")
                runtime._refresh_result({"request_id": "first"})
                runtime.request_refresh()
                self.assertEqual(runtime.refresh_deadline, 105)
                self.assertEqual(writer.write.call_count, 2)
                runtime.engine.state["subscriber_losses"]["first"]["reconciled"] = True
                runtime.request_refresh()
                self.assertEqual(runtime.refresh_deadline, 130)
                for _ in range(10):
                    runtime._refresh_result({"request_id": "second"})
                self.assertEqual(
                    runtime.engine.state["subscriber_losses"]["second"]["retry_at"], 160
                )

    async def test_same_speaker_serializes_announcement_and_resume_and_rechecks_stop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = service.Runtime(Path(root), Engine({}))
            runtime.engine.stream = ("boot", "stream")
            runtime.engine.players["studio"] = {
                "queue_id": "queue",
                "item_id": "track",
                "state": "idle",
                "available": True,
            }
            action = {
                "player_id": "studio",
                "queue_id": "queue",
                "item_id": "track",
                "stream_id": "stream",
                "not_after": time.time() + 30,
                "source_interrupted_at": time.monotonic() - 5,
                "source_deadline": time.monotonic(),
                "source_not_after": time.monotonic() + 30,
            }
            entered, release = asyncio.Event(), asyncio.Event()
            calls = []

            async def command(
                _argv, payload, *_args: object, **_kwargs: object
            ) -> dict:
                calls.append(payload["type"])
                entered.set()
                await release.wait()
                return {"status": "accepted"}

            with patch.object(service, "command_json", command):
                first = asyncio.create_task(
                    runtime.announce({**action, "type": "announce"})
                )
                await asyncio.wait_for(entered.wait(), 1)
                second = asyncio.create_task(
                    runtime.announce({**action, "type": "resume"})
                )
                await asyncio.sleep(0)
                self.assertEqual(calls, ["announce"])

                runtime.engine.intents["studio"] = time.time() + 30
                release.set()
                self.assertEqual((await first)["status"], "accepted")
                self.assertEqual((await second)["status"], "suppressed")
                self.assertEqual(calls, ["announce"])

    async def test_shared_observer_receipt_does_not_drop_live_publish_task(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = service.Runtime(Path(root), Engine({}))
            blocked = asyncio.Event()
            task = asyncio.create_task(blocked.wait())
            runtime.effects["event"] = task
            runtime.engine.state["outbox"]["event"] = {
                "type": "publish",
                "event": {"event_id": "event"},
            }
            runtime._shared_delivery(
                {
                    "protocol_version": 1,
                    "source": "smart-home",
                    "receipt": {
                        "protocol_version": 1,
                        "source": "smart-home",
                        "event_id": "event",
                        "durable": True,
                        "digest": "a" * 64,
                        "status": "pending",
                    },
                }
            )
            self.assertIs(runtime.effects["event"], task)
            self.assertEqual(runtime.engine.state["outbox"], {})
            blocked.set()
            await task

    def test_retention_keeps_unresolved_work_and_drops_old_terminal_claims(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = service.Runtime(Path(root), Engine({}))
            runtime.engine.state["incidents"] = {
                "scope": {"id": "open", "closed": None}
            }
            runtime.engine.state["spoken"] = {
                "old:studio": {"attempted_at": 1},
                "open:studio": {"attempted_at": 1},
            }
            runtime.engine.state["subscriber_losses"] = {
                "known": {"observed_at": 1, "reconciled": False},
                "closed": {"observed_at": 1, "reconciled": True, "acknowledged": True},
            }
            runtime.prune_terminal(service.RECEIPT_RETENTION + 2)
            self.assertEqual(set(runtime.engine.state["spoken"]), {"open:studio"})
            self.assertEqual(set(runtime.engine.state["subscriber_losses"]), {"known"})

    def test_dead_subscriber_can_be_replaced_for_absent_collector_without_ha(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = service.Runtime(Path(root), Engine({}))
            runtime.engine.stream = ("consumer", "local")
            runtime.engine._open("monitor:stream", "network", 1000)
            runtime.engine.due(1005)
            runtime.engine.state["outbox"].clear()
            runtime.engine.state["subscriber_losses"]["loss"] = {
                "observed_at": 1006,
                "reconciled": False,
            }
            with patch.object(runtime, "submit") as submit:
                runtime.reconcile_source_absence()
            actions = [
                action for call in submit.call_args_list for action in call.args[0]
            ]
            revision = next(action for action in actions if action["type"] == "publish")
            self.assertEqual(revision["event"]["scope"], "monitor:stream")
            self.assertEqual(revision["event"]["phase"], "updated")
            self.assertTrue(
                runtime.engine.state["subscriber_losses"]["loss"]["reconciled"]
            )

    async def test_tentative_control_wakes_audio_only_after_origin_is_resolved(
        self,
    ) -> None:
        for user_stop in (False, True):
            with (
                self.subTest(user_stop=user_stop),
                tempfile.TemporaryDirectory() as root,
            ):
                runtime = service.Runtime(Path(root), Engine({}))
                runtime.engine.stream = ("boot", "stream")
                runtime.engine.players["studio"] = {
                    "queue_id": "queue",
                    "item_id": "track",
                    "state": "idle",
                    "available": True,
                }
                runtime.engine.tentative["control"] = {"studio"}
                action = {
                    "type": "resume",
                    "player_id": "studio",
                    "queue_id": "queue",
                    "item_id": "track",
                    "stream_id": "stream",
                    "not_after": time.time() + 10,
                    "source_interrupted_at": 100,
                    "source_deadline": 105,
                    "source_not_after": 115,
                }
                calls = []

                async def command(
                    *_args: object, captured: list = calls, **_kwargs: object
                ) -> dict:
                    captured.append("submitted")
                    return {"status": "accepted"}

                with patch.object(service, "command_json", command):
                    work = asyncio.create_task(runtime.announce(action))
                    await asyncio.sleep(0)
                    self.assertFalse(work.done())
                    self.assertEqual(calls, [])
                    if user_stop:
                        runtime.engine.intents["studio"] = time.time() + 30
                    runtime.engine.tentative.clear()
                    runtime.source_changed.set()
                    result = await asyncio.wait_for(work, 1)
                self.assertEqual(
                    result["status"], "suppressed" if user_stop else "accepted"
                )
                self.assertEqual(calls, [] if user_stop else ["submitted"])

    def test_corrupt_nested_state_is_quarantined_before_effects(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "state.json").write_text('{"schema":2,"incidents":{"a":[]}}')
            self.assertEqual(service.load_state(path), {"schema": 2})
            self.assertEqual(len(list(path.glob("state-quarantined-*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
