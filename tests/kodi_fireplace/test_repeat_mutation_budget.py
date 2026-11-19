"""Give the one repeat mutation the established bounded mutation transport."""

from __future__ import annotations

import json
import threading
from typing import Any

import test_native_startup_budget as base


class RepeatMutationBudgetCases(base.NativeStartupBudgetCases):
    """Exercise delayed repeat acknowledgements through the production adapter."""

    def setUp(self) -> None:
        super().setUp()
        self.repeat_delay = 0.0
        self.repeat_sent = None
        self.on_repeat = lambda: None

    def jsonrpc(self, method, params, *, timeout: float) -> dict[str, Any]:
        if (
            method == "Player.SetRepeat"
            and params["repeat"] == "one"
            and self.repeat_delay
        ):
            self.transports.append((method, timeout, self.clock.now))
            self.repeat_sent = self.clock.now
            self.on_repeat()
            self.clock.now += min(self.repeat_delay, timeout)
            self.kodi.rpc(method, params, self.clock.now + timeout)
            if self.repeat_delay > timeout:
                message = "private delayed repeat acknowledgement"
                raise TimeoutError(message)
            return {"ok": True, "body": {"result": "OK"}}
        result = super().jsonrpc(method, params, timeout=timeout)
        if method == "Player.Stop" and self.repeat_delay:
            self.opened_at = None
        return result

    def repeat_calls(self) -> list[tuple]:
        return [call for call in self.transports if call[0] == "Player.SetRepeat"]

    def test_three_second_repeat_ack_confirms_before_scene_returns(self) -> None:
        self.visible_after = 0
        self.repeat_delay = 3.0
        self.assert_playing(self.controller())
        self.assertEqual(len(self.repeat_calls()), 1)
        self.assertEqual(self.repeat_calls()[0][1], 8.0)

    def test_repeat_transport_is_clipped_to_original_deadline(self) -> None:
        self.repeat_delay = 10.0
        with self.assertRaises(base.fixtures.fireplace.RpcTransportError):
            self.bridge_rpc(
                "Player.SetRepeat",
                {"playerid": 1, "repeat": "one"},
                self.clock.now + 2.5,
            )
        self.assertEqual(len(self.repeat_calls()), 1)
        self.assertEqual(self.repeat_calls()[0][1], 2.5)
        self.assertEqual(self.clock.now, 102.5)

    def test_ambiguous_repeat_keeps_armed_receipt_without_replaying(self) -> None:
        self.visible_after = 0
        self.repeat_delay = 20.0
        owner = base.fixtures.fireplace.OwnedPlayback(
            self.bridge_rpc,
            self.root / "owner.json",
            media_path=self.media,
            expected_sha256=self.sha,
            auto_monitor=False,
            startup_timeout=45,
        )
        self.addCleanup(owner.close)
        with (
            base.patch.object(owner._closing, "wait", side_effect=self.wait_tick),
            self.assertRaises(base.fixtures.fireplace.RpcTransportError),
        ):
            owner.start()
        receipt = json.loads((self.root / "owner.json").read_text())
        self.assertEqual(receipt["phase"], "armed")
        self.assertEqual(len(self.repeat_calls()), 1)
        self.assertEqual(self.repeat_calls()[0][1], 8.0)
        self.assert_one_open()

    def test_stop_during_repeat_ack_keeps_eight_second_bound(self) -> None:
        self.visible_after = 0
        self.repeat_delay = 7.0
        controller = self.controller()
        outcomes = []
        times = []
        stopper = None

        def stop() -> None:
            times.append(self.clock.now)
            try:
                outcomes.append(controller.stop())
            except base.fixtures.fireplace.FireplaceError as error:
                outcomes.append(type(error).__name__)
            times.append(self.clock.now)

        def start_stop() -> None:
            nonlocal stopper
            stopper = threading.Thread(target=stop)
            stopper.start()
            self.assertTrue(controller._cancelled.wait(1))

        self.on_repeat = start_stop
        try:
            with self.assertRaises(base.fixtures.fireplace.FireplaceError):
                controller.start("cancel-in-flight-repeat")
        finally:
            if stopper is not None:
                stopper.join(timeout=2)
        self.assertIsNotNone(stopper)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(times), 2)
        self.assertLessEqual(times[-1] - times[0], 8.2)
        self.assert_one_open()
        self.assertEqual(
            sum(
                params.get("repeat") == "one"
                for method, params in self.kodi.calls
                if method == "Player.SetRepeat"
            ),
            1,
        )

    def test_repeat_ack_diagnostic_contains_only_safe_fields(self) -> None:
        self.bridge_rpc(
            "Player.SetRepeat", {"playerid": 1, "repeat": "one"}, self.clock.now + 100
        )
        repeat_events = [
            event for event in self.events if "Player.SetRepeat" in json.dumps(event)
        ]
        self.assertTrue(
            any("acknowledged" in json.dumps(event) for event in repeat_events)
        )
        self.assertNotIn("private", json.dumps(repeat_events))
        self.assertNotIn(str(self.media), json.dumps(repeat_events))
