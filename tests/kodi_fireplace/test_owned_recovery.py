"""Repeat ownership during native transitions and expired durable writes."""

import unittest
from unittest.mock import patch

import test_owned_playback as fixtures


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

        with patch.object(self.module.time, "monotonic", side_effect=lambda: now[0]):
            with patch.object(
                self.controller, "_persist", side_effect=store_then_expire
            ):
                with self.assertRaises(self.module.FireplaceError):
                    self.controller.start()
        self.assertFalse(self.receipt.exists())
        self.assertNotIn("Player.SetRepeat", [method for method, _ in self.kodi.calls])
        self.assertEqual(self.kodi.repeat, "off")
