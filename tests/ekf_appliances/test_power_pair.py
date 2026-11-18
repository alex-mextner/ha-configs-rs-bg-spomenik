"""Paired power state machine: real feedback only, no appliance network I/O."""

from __future__ import annotations

import asyncio
import unittest

from homeassistant.exceptions import HomeAssistantError

from custom_components.ekf_appliances.power_pair import PowerEndpoint, PowerPair


class PowerPairTests(unittest.IsolatedAsyncioTestCase):
    """Verify paired-power intent and feedback ordering without appliance I/O."""

    async def asyncSetUp(self) -> None:
        self.states = {"display": False, "player": False}
        self.calls = []
        self.hold = {}
        self.failed_endpoints = set()

        def endpoint(name) -> PowerEndpoint:
            async def command(target) -> None:
                self.calls.append((name, target))
                if name in self.hold:
                    await self.hold[name].wait()
                if name in self.failed_endpoints:
                    error_message = "Disconnected"
                    raise HomeAssistantError(error_message)
                self.states[name] = target
                self.pair.observe()

            return PowerEndpoint(lambda: self.states[name], command)

        self.pair = PowerPair(
            "Test", endpoint("display"), endpoint("player"), timeout=0.3
        )
        self.pair.start()
        await asyncio.sleep(0)

    async def asyncTearDown(self) -> None:
        await self.pair.close()

    async def settle(self) -> None:
        for _ in range(12):
            await asyncio.sleep(0)

    async def physical(self, side, value) -> None:
        self.states[side] = value
        self.pair.observe()
        await self.settle()

    async def test_external_display_on_and_off_follow_to_player(self) -> None:
        await self.physical("display", value=True)
        self.assertEqual(self.states, {"display": True, "player": True})
        await self.physical("display", value=False)
        self.assertEqual(self.states, {"display": False, "player": False})
        self.assertEqual(self.calls, [("player", True), ("player", False)])

    async def test_external_player_on_and_off_follow_to_display(self) -> None:
        await self.physical("player", value=True)
        await self.physical("player", value=False)
        self.assertEqual(self.calls, [("display", True), ("display", False)])

    async def test_explicit_power_drives_both_without_waiting_for_display_boot(
        self,
    ) -> None:
        self.hold["display"] = asyncio.Event()
        pending = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        self.assertEqual(set(self.calls), {("display", True), ("player", True)})
        self.assertTrue(self.states["player"])
        self.assertFalse(self.states["display"])
        self.hold["display"].set()
        await pending
        self.assertEqual(self.pair.target, None)

    async def test_duplicate_request_and_echo_do_not_duplicate_commands(self) -> None:
        self.hold["display"] = asyncio.Event()
        a = asyncio.create_task(self.pair.request(target=True))
        b = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        self.pair.observe()
        self.hold["display"].set()
        await asyncio.gather(a, b)
        await self.pair.request(target=True)
        self.assertEqual(self.calls.count(("display", True)), 1)
        self.assertEqual(self.calls.count(("player", True)), 1)

    async def test_unknown_or_recovered_same_state_never_powers_off(self) -> None:
        await self.pair.request(target=True)
        self.calls.clear()
        await self.physical("player", None)
        await self.physical("player", value=True)
        await self.physical("display", None)
        await self.physical("display", value=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.states, {"display": True, "player": True})

    async def test_recovery_changed_from_last_valid_state_is_an_actual_transition(
        self,
    ) -> None:
        await self.pair.request(target=True)
        self.calls.clear()
        await self.physical("player", None)
        await self.physical("player", value=False)
        self.assertEqual(self.calls, [("display", False)])

    async def test_startup_uses_display_and_waits_for_known_player(self) -> None:
        await self.pair.close()
        self.states = {"display": False, "player": None}
        self.pair.start()
        await self.settle()
        self.assertEqual(self.calls, [])
        await self.physical("player", value=True)
        self.assertEqual(self.calls, [("player", False)])
        self.assertEqual(self.states, {"display": False, "player": False})

    async def test_first_valid_feedback_cannot_override_an_explicit_startup_request(
        self,
    ) -> None:
        await self.pair.close()
        self.states = {"display": None, "player": False}
        self.pair.start()
        request = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        self.assertIs(self.pair.target, expr2=True)
        self.states["display"] = False
        self.pair.observe()
        await request
        self.assertEqual(self.states, {"display": True, "player": True})

    async def test_unknown_projector_never_receives_automatic_toggle(self) -> None:
        self.states["display"] = None
        self.pair.observe()
        with self.assertRaises(HomeAssistantError):
            await self.pair.request(target=True)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.states["display"])
        self.assertIn("confirm", self.pair.last_error.lower())

    async def test_failed_service_does_not_report_success_or_retry(self) -> None:
        self.failed_endpoints.add("display")
        with self.assertRaises(HomeAssistantError):
            await self.pair.request(target=True)
        await self.settle()
        self.assertEqual(self.calls.count(("display", True)), 1)
        self.assertIsNotNone(self.pair.last_error)

    async def assert_empty_command_failure(self, failure) -> None:
        async def fail_without_message(target) -> None:
            self.calls.append(("display", target))
            raise failure

        self.pair.endpoints["display"] = PowerEndpoint(
            lambda: self.states["display"], fail_without_message
        )
        with self.assertRaises(HomeAssistantError):
            await self.pair.request(target=True)
        await self.settle()
        self.assertTrue(self.pair.last_error)
        self.assertEqual(self.calls.count(("display", True)), 1)
        self.assertIsNone(self.pair.target)

    async def test_message_less_service_error_is_a_failure(self) -> None:
        await self.assert_empty_command_failure(HomeAssistantError())

    async def test_message_less_command_timeout_is_a_failure(self) -> None:
        await self.assert_empty_command_failure(TimeoutError())

    async def test_cancelled_endpoint_command_is_a_failure(self) -> None:
        await self.assert_empty_command_failure(asyncio.CancelledError())

    async def test_latest_off_wins_over_delayed_old_on_feedback(self) -> None:
        self.hold["display"] = asyncio.Event()
        old = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        latest = asyncio.create_task(self.pair.request(target=False))
        await self.settle()
        self.hold["display"].set()
        with self.assertRaises(HomeAssistantError):
            await old
        await latest
        self.assertEqual(self.states, {"display": False, "player": False})
        self.assertEqual(self.calls.count(("display", True)), 1)
        self.assertEqual(self.calls.count(("display", False)), 1)
        self.assertIsNone(self.pair.target)

    async def test_late_on_feedback_cannot_reverse_a_completed_off(self) -> None:
        async def delayed_player_feedback(target) -> None:
            self.calls.append(("player", target))
            if not target:
                self.states["player"] = False
                self.pair.observe()

        self.pair.endpoints["player"] = PowerEndpoint(
            lambda: self.states["player"], delayed_player_feedback
        )
        old = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        await self.pair.request(target=False)
        with self.assertRaises(HomeAssistantError):
            await old
        completed_revision = self.pair.intent_revision
        self.assertEqual(self.states, {"display": False, "player": False})

        await self.physical("player", value=True)
        self.assertEqual(self.states, {"display": False, "player": False})
        self.assertEqual(
            self.calls,
            [
                ("display", True),
                ("player", True),
                ("display", False),
                ("player", False),
            ],
        )
        self.assertIs(self.pair.last_intent, expr2=False)
        self.assertEqual(self.pair.intent_revision, completed_revision)
        self.assertIsNone(self.pair.target)

        # The consumed acknowledgement cannot suppress a later real ON.
        await self.physical("player", value=True)
        self.assertEqual(self.states, {"display": True, "player": True})
        self.assertIs(self.pair.last_intent, expr2=True)

    async def test_expired_unconfirmed_feedback_does_not_mask_a_new_physical_on(
        self,
    ) -> None:
        async def no_player_feedback(target) -> None:
            self.calls.append(("player", target))

        self.pair.endpoints["player"] = PowerEndpoint(
            lambda: self.states["player"], no_player_feedback
        )
        self.pair.timeout = 0.02
        old = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        await self.pair.request(target=False)
        with self.assertRaises(HomeAssistantError):
            await old
        await asyncio.sleep(0.03)
        await self.physical("player", value=True)
        self.assertEqual(self.states, {"display": True, "player": True})
        self.assertIs(self.pair.last_intent, expr2=True)

    async def test_late_on_reasserts_off_while_the_display_is_still_stopping(
        self,
    ) -> None:
        async def delayed_player_feedback(target) -> None:
            self.calls.append(("player", target))
            if not target:
                self.states["player"] = False
                self.pair.observe()

        self.pair.endpoints["player"] = PowerEndpoint(
            lambda: self.states["player"], delayed_player_feedback
        )
        old = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        # The native read can advance before its queued state observer runs.
        self.states["player"] = True
        self.hold["display"] = asyncio.Event()
        latest = asyncio.create_task(self.pair.request(target=False))
        await self.settle()
        await self.physical("player", value=True)
        self.hold["display"].set()
        with self.assertRaises(HomeAssistantError):
            await old
        await latest
        self.assertEqual(self.states, {"display": False, "player": False})
        self.assertEqual(self.calls.count(("player", False)), 2)
        self.assertEqual(self.calls.count(("display", True)), 1)

    async def test_physical_player_off_during_tv_boot_is_latest_intent(self) -> None:
        self.hold["display"] = asyncio.Event()
        old = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        await self.physical("player", value=False)
        self.hold["display"].set()
        with self.assertRaises(HomeAssistantError):
            await old
        await self.settle()
        self.assertEqual(self.states, {"display": False, "player": False})

    async def test_cancelled_caller_cannot_cancel_the_shared_power_transition(
        self,
    ) -> None:
        self.hold["display"] = asyncio.Event()
        first = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        self.assertEqual(self.calls, [("display", True), ("player", True)])
        self.assertIs(self.pair.target, expr2=True)
        self.hold["display"].set()
        await second
        self.assertEqual(self.states, {"display": True, "player": True})
        self.assertIsNone(self.pair.target)

    async def test_latest_on_supersedes_an_unexecuted_off_without_extra_commands(
        self,
    ) -> None:
        self.hold["display"] = asyncio.Event()
        first_on = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        old_off = asyncio.create_task(self.pair.request(target=False))
        await self.settle()
        latest_on = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        self.hold["display"].set()
        with self.assertRaises(HomeAssistantError):
            await first_on
        with self.assertRaises(HomeAssistantError):
            await old_off
        await latest_on
        self.assertEqual(self.calls, [("display", True), ("player", True)])
        self.assertEqual(self.states, {"display": True, "player": True})
        self.assertIsNone(self.pair.last_error)

    async def test_intent_revision_survives_completion_and_ignores_unchanged_feedback(
        self,
    ) -> None:
        initial = self.pair.intent_revision
        self.assertIsNone(self.pair.last_intent)
        await self.pair.request(target=False)
        completed = self.pair.intent_revision
        self.assertNotEqual(completed, initial)
        self.assertIs(self.pair.last_intent, expr2=False)
        self.assertIsNone(self.pair.target)
        self.pair.observe()
        await self.settle()
        self.assertEqual(self.pair.intent_revision, completed)
        await self.pair.request(target=False)
        self.assertNotEqual(self.pair.intent_revision, completed)
        self.assertIs(self.pair.last_intent, expr2=False)
        self.assertEqual(self.calls, [])

    async def test_coalesced_request_records_a_new_intent_without_duplicate_commands(
        self,
    ) -> None:
        self.hold["display"] = asyncio.Event()
        first = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        first_revision = self.pair.intent_revision
        second = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        latest_revision = self.pair.intent_revision
        self.assertNotEqual(first_revision, latest_revision)
        self.assertIs(self.pair.last_intent, expr2=True)
        self.assertEqual(self.calls, [("display", True), ("player", True)])
        self.hold["display"].set()
        await asyncio.gather(first, second)
        self.assertEqual(self.pair.intent_revision, latest_revision)
        self.assertIsNone(self.pair.target)

    async def test_new_controller_has_a_distinct_intent_revision_before_any_command(
        self,
    ) -> None:
        other = PowerPair("New", *self.pair.endpoints.values())
        self.assertNotEqual(self.pair.intent_revision, other.intent_revision)
        self.assertIsNone(other.last_intent)
        await other.close()

    async def test_completed_newer_off_rejects_a_queued_old_wake_without_io(
        self,
    ) -> None:
        wake_snapshot = self.pair.intent_revision
        await self.pair.request(target=False)
        latest = self.pair.intent_revision
        self.assertIsNone(self.pair.target)
        with self.assertRaisesRegex(HomeAssistantError, "superseded"):
            await self.pair.wake_if_current(wake_snapshot)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.pair.intent_revision, latest)
        self.assertIs(self.pair.last_intent, expr2=False)

    async def test_guarded_wake_accepts_current_revision_once(self) -> None:
        self.hold["display"] = asyncio.Event()
        snapshot = self.pair.intent_revision
        first = asyncio.create_task(self.pair.wake_if_current(snapshot))
        await self.settle()
        with self.assertRaisesRegex(HomeAssistantError, "superseded"):
            await self.pair.wake_if_current(snapshot)
        self.assertEqual(self.calls, [("display", True), ("player", True)])
        self.hold["display"].set()
        await first

    async def test_guarded_wake_rejects_unknown_display_without_changing_intent(
        self,
    ) -> None:
        self.states["display"] = None
        snapshot = self.pair.intent_revision
        with self.assertRaisesRegex(HomeAssistantError, "unknown"):
            await self.pair.wake_if_current(snapshot)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.pair.intent_revision, snapshot)
        self.assertIsNone(self.pair.last_intent)

    async def test_guarded_wake_rejects_a_previous_controller_incarnation(self) -> None:
        old_snapshot = self.pair.intent_revision
        other = PowerPair("New", *self.pair.endpoints.values())
        other.start()
        try:
            with self.assertRaisesRegex(HomeAssistantError, "superseded"):
                await other.wake_if_current(old_snapshot)
            self.assertEqual(self.calls, [])
            self.assertIsNone(other.last_intent)
        finally:
            await other.close()

    async def test_repeated_pending_off_records_intent_without_restarting_physical_work(
        self,
    ) -> None:
        await self.pair.request(target=True)
        self.calls.clear()
        self.hold["display"] = asyncio.Event()
        first = asyncio.create_task(self.pair.request(target=False))
        await self.settle()
        first_revision = self.pair.intent_revision
        second = asyncio.create_task(self.pair.request(target=False))
        await self.settle()
        self.assertNotEqual(self.pair.intent_revision, first_revision)
        self.assertEqual(self.calls, [("display", False), ("player", False)])
        self.hold["display"].set()
        await asyncio.gather(first, second)
        self.assertIs(self.pair.last_intent, expr2=False)
        self.assertIsNone(self.pair.target)

    async def test_close_cancels_waits_without_an_appliance_command(self) -> None:
        self.hold["display"] = asyncio.Event()
        old = asyncio.create_task(self.pair.request(target=True))
        await self.settle()
        calls = list(self.calls)
        await self.pair.close()
        await asyncio.gather(old, return_exceptions=True)
        self.assertEqual(self.calls, calls)


if __name__ == "__main__":
    unittest.main()
