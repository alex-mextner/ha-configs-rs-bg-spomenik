"""A returning owned video must retain later human repeat intent across restart."""

import time
from unittest.mock import patch

import test_rotation as fixtures
import test_rotation_recovery as recovery
import test_rotation_stop_boundaries as stop_fixtures


class RestorationReturnCases(fixtures.FixtureBase):
    multi = fixtures.RotationCases.multi
    selection = recovery.RecoveryCases.selection
    looped_pending = stop_fixtures.StopBoundaryCases.looped_pending

    def return_during_restoration(self, controller, boundary):
        expected = self.kodi.file
        self.kodi.file, self.kodi.speed = None, 0
        controller._owner._idle_since = time.monotonic() - 3
        persist = controller._owner._persist
        native = controller._rpc
        returned = []

        def resume():
            returned.append(boundary)
            self.kodi.file, self.kodi.speed, self.kodi.repeat = expected, 1, "one"

        def store(receipt):
            persist(receipt)
            if receipt["phase"] == "restoring" and boundary == "persist":
                resume()

        def callback(method, params, deadline):
            result = native(method, params, deadline)
            if (
                boundary == "readback"
                and method == "Player.SetRepeat"
                and params["repeat"] == "off"
            ):
                resume()
            return result

        with (
            patch.object(controller._owner, "_persist", side_effect=store),
            patch.object(controller, "_rpc", side_effect=callback),
        ):
            proof = controller.observe()
        self.assertEqual(returned, [boundary])
        self.assertTrue(proof["owned"])
        self.assertEqual(self.kodi.file, expected)
        self.assertEqual(self.kodi.repeat, "one")
        return expected

    def assert_human_repeat_survives(self, controller, expected, operation):
        self.kodi.calls.clear()
        self.kodi.repeat = "off"
        getattr(controller, operation)()
        self.assertEqual(self.kodi.file, expected)
        self.assertEqual(self.kodi.repeat, "off")
        self.assertIsNone(self.selection()["pending"])
        self.assertIsNone(self.selection()["active"])
        controller.close()
        recovered = self.multi()
        recovered.observe()
        self.assertEqual(self.kodi.file, expected)
        self.assertEqual(self.kodi.repeat, "off")
        self.assertFalse(
            [
                call
                for call in self.kodi.calls
                if call[0] in {"Player.Stop", "Player.SetRepeat"}
            ]
        )

    def test_pending_return_during_durable_restore_survives_restart(self):
        controller = self.looped_pending()
        expected = self.return_during_restoration(controller, "persist")
        self.assertIsNotNone(self.selection()["pending"])
        controller.close()
        recovered = self.multi()
        self.assert_human_repeat_survives(recovered, expected, "observe")

    def test_confirmed_return_during_durable_restore_survives_explicit_stop(self):
        controller = self.multi()
        controller.start("confirmed-before-return")
        expected = self.return_during_restoration(controller, "persist")
        self.assertIsNotNone(self.selection()["active"])
        self.assert_human_repeat_survives(controller, expected, "stop")

    def test_pending_return_during_restore_readback_survives_restart(self):
        controller = self.looped_pending()
        expected = self.return_during_restoration(controller, "readback")
        self.assertIsNotNone(self.selection()["pending"])
        controller.close()
        recovered = self.multi()
        self.assert_human_repeat_survives(recovered, expected, "observe")
