"""Repeat ownership during native transitions and expired durable writes."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

import test_owned_playback as fixtures
from test_manifest_http import bridge


class OwnerRecoveryCases(unittest.TestCase):
    """Exercise real ownership with bounded native-player transitions."""

    setUp = fixtures.HostFireplaceTests.setUp
    tearDown = fixtures.HostFireplaceTests.tearDown
    create = fixtures.HostFireplaceTests.create

    def test_short_loop_gap_keeps_repeat_and_recovers_the_same_video(self) -> None:
        self.controller.start()
        self.kodi.active, self.kodi.file, self.kodi.speed = None, None, 0
        self.kodi.calls.clear()
        proof = self.controller.observe()
        self.assertTrue(proof["owned"])
        self.assertEqual(self.kodi.repeat, "one")
        self.assertEqual(self.kodi.changes(), [])
        self.kodi.active, self.kodi.file, self.kodi.speed = 1, str(self.media), 1
        proof = self.controller.observe()
        self.assertEqual(proof["state"], "playing")
        self.assertTrue(proof["owned"])

    def test_confirmed_absence_eventually_restores_the_previous_repeat(self) -> None:
        self.kodi.repeat = "all"
        now = [99.0]
        with patch.object(self.module.time, "monotonic", side_effect=lambda: now[0]):
            self.controller.start()
            self.kodi.active, self.kodi.file, self.kodi.speed = None, None, 0
            now[0] = 100.0
            first = self.controller.observe()
            self.assertTrue(first["owned"])
            now[0] = 102.5
            second = self.controller.observe()
        self.assertFalse(second["owned"])
        self.assertEqual(self.kodi.repeat, "all")

    def test_expired_arm_drops_a_receipt_before_any_repeat_mutation(self) -> None:
        now = [100.0]
        persist = self.controller._persist

        def store_then_expire(receipt) -> None:
            persist(receipt)
            now[0] = 102.0

        with (
            patch.object(self.module.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(self.controller, "_persist", side_effect=store_then_expire),
            self.assertRaises(self.module.FireplaceError),
        ):
            self.controller.start()
        self.assertFalse(self.receipt.exists())
        self.assertNotIn("Player.SetRepeat", [method for method, _ in self.kodi.calls])
        self.assertEqual(self.kodi.repeat, "off")

    def test_expiry_after_the_final_read_drops_an_undispatched_arm(self) -> None:
        now = [100.0]
        read = self.controller._read

        def read_then_expire(deadline) -> Any:
            current = read(deadline)
            if self.controller._owner is not None:
                now[0] = 102.0
            return current

        with (
            patch.object(self.module.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(self.controller, "_read", side_effect=read_then_expire),
            self.assertRaises(self.module.FireplaceError),
        ):
            self.controller.start()
        self.assertFalse(self.receipt.exists())
        self.assertNotIn("Player.SetRepeat", [method for method, _ in self.kodi.calls])
        self.assertEqual(self.kodi.repeat, "off")

    def test_empty_stop_is_not_evidence_of_an_exact_player_stop(self) -> None:
        self.controller.start()
        self.kodi.active, self.kodi.file, self.kodi.speed = None, None, 0
        self.kodi.calls.clear()
        proof = self.controller.stop()
        self.assertFalse(proof["stopped"])
        self.assertFalse([call for call in self.kodi.calls if call[0] == "Player.Stop"])

    def test_actual_bridge_expiry_before_setrepeat_drops_the_unmutated_arm(
        self,
    ) -> None:

        now = [100.0]
        dispatched = []

        def transport(method, params, *, timeout) -> dict[str, Any]:
            dispatched.append(method)
            result = self.kodi.rpc(method, params, timeout)
            return {"ok": True, "body": {"result": result}}

        def callback(method, params, deadline) -> Any:
            if method == "Player.SetRepeat":
                now[0] = deadline
            return bridge._fireplace_endpoints.rpc(method, params, deadline)

        self.controller._rpc_callback = callback
        with (
            patch.object(self.module.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(bridge._fireplace_http, "_fireplace_module", self.module),
            patch.object(bridge._fireplace_endpoints, "_rpc", side_effect=transport),
            self.assertRaises(self.module.FireplaceError),
        ):
            self.controller.start()
        self.assertNotIn("Player.SetRepeat", dispatched)
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.kodi.repeat, "off")

    def test_foreign_repeat_unknown_does_not_supersede_an_unchanged_video_mode(
        self,
    ) -> None:
        self.controller.start()
        reads = []

        def transition(method, _params) -> None:
            if method != "Player.GetActivePlayers":
                return
            reads.append(True)
            if len(reads) == 2:
                self.kodi.active, self.kodi.file, self.kodi.speed = 0, "/human/audio", 1
            elif len(reads) == 3:
                self.kodi.active, self.kodi.file, self.kodi.speed = (
                    1,
                    str(self.media),
                    1,
                )

        self.kodi.before_call = transition
        proof = self.controller.stop()
        self.kodi.before_call = None
        self.assertEqual(len(reads), 3)
        self.assertTrue(proof["owned"])
        self.assertFalse(proof.get("repeat_superseded") is True)
        self.assertFalse(proof["stopped"])
        self.assertTrue(self.receipt.exists())
        self.assertEqual(self.kodi.repeat, "one")
        self.controller.stop()
        self.assertEqual(self.kodi.repeat, "off")
        self.assertFalse(self.receipt.exists())

    def test_final_stop_read_reconfirms_a_returning_restoration_candidate(self) -> None:
        self.controller.start()
        self.kodi.active, self.kodi.file, self.kodi.speed = None, None, 0
        self.controller._idle_since = self.module.time.monotonic() - 3
        persist = self.controller._persist

        def defer_restoration(receipt) -> None:
            persist(receipt)
            if receipt["phase"] == "restoring":
                self.kodi.active, self.kodi.file, self.kodi.speed = 0, "/human/audio", 1

        with patch.object(self.controller, "_persist", side_effect=defer_restoration):
            self.controller.observe()
        self.assertEqual(self.controller._owner["phase"], "restoring")
        self.kodi.active, self.kodi.file, self.kodi.speed = None, None, 0
        self.kodi.calls.clear()
        reads = []

        def return_after_foreign_observation(method, _params) -> None:
            if method != "Player.GetActivePlayers":
                return
            reads.append(True)
            if len(reads) == 2:
                self.kodi.active, self.kodi.file, self.kodi.speed = 0, "/human/audio", 1
            elif len(reads) == 3:
                self.kodi.active, self.kodi.file, self.kodi.speed = (
                    1,
                    str(self.media),
                    1,
                )

        self.kodi.before_call = return_after_foreign_observation
        proof = self.controller.stop()
        self.kodi.before_call = None
        self.assertEqual(len(reads), 3)
        self.assertTrue(proof["owned"])
        self.assertFalse(proof["stopped"])
        self.kodi.repeat = "off"
        released = self.controller.stop()
        self.assertTrue(released.get("repeat_superseded") is True)
        self.assertEqual(self.kodi.file, str(self.media))
        self.assertFalse([call for call in self.kodi.calls if call[0] == "Player.Stop"])
