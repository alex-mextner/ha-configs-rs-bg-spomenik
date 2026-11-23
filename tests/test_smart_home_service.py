# Copyright (c) 2026 Alex Mextner
"""Slow native agent startup must not stall event cancellation or deadlines."""

import asyncio
import contextlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/host"))
import ha_smart_home_service as service
from ha_smart_home_recovery import Engine


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
                "source": "smart-home",
                "authority_id": "authority",
                "revision": "revision",
                "native_state": "unknown",
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

    def test_corrupt_nested_state_is_quarantined_before_effects(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "state.json").write_text('{"schema":2,"incidents":{"a":[]}}')
            self.assertEqual(service.load_state(path), {"schema": 2})
            self.assertEqual(len(list(path.glob("state-quarantined-*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
