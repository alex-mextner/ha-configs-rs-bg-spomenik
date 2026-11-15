"""Observable cancellation and repeat intent at the final native dispatch boundary."""

from unittest.mock import patch

import test_rotation as fixtures
import test_rotation_recovery as recovery


class DispatchBoundaryCases(fixtures.FixtureBase):
    multi = fixtures.RotationCases.multi
    selection = recovery.RecoveryCases.selection

    def stalled_controller(self, after_rpc=None):
        prepared = []

        def prepare(source, _deadline):
            prepared.append(source["id"])
            return {"file": source["path"]}

        controller = self.multi(operation_timeout=8, resolver=prepare)
        native = controller._rpc

        def callback(method, params, deadline):
            result = native(method, params, deadline)
            if method == "Player.GetProperties" and self.kodi.file == str(
                self.root / "0.webm"
            ):
                result["time"] = {
                    "hours": 0,
                    "minutes": 0,
                    "seconds": 0,
                    "milliseconds": 0,
                }
            if after_rpc is not None:
                after_rpc(controller, method, params)
            return result

        controller._rpc = callback
        return controller, prepared

    def assert_repeat_supersedes_fallback(self, controller, prepared):
        with self.assertRaisesRegex(
            fixtures.fireplace.FireplaceError, "playback_changed"
        ):
            controller.start("repeat-superseded")
        self.assertEqual(prepared, ["local-0"])
        self.assertEqual(
            sum(method == "Player.Open" for method, _ in self.kodi.calls), 1
        )
        self.assertEqual(self.kodi.repeat, "all")
        self.assertIsNone(self.selection()["last_successful_source_id"])
        self.assertIsNone(self.selection()["pending"])
        controller.observe()
        self.assertEqual(self.kodi.repeat, "all")

    def test_repeat_change_before_cleanup_preserves_the_matching_video(self):
        controller, prepared = self.stalled_controller()
        stop = controller._owner.stop

        def human_repeat(**options):
            self.kodi.repeat = "all"
            return stop(**options)

        with patch.object(controller._owner, "stop", side_effect=human_repeat):
            self.assert_repeat_supersedes_fallback(controller, prepared)
        self.assertEqual(self.kodi.file, str(self.root / "0.webm"))
        self.assertFalse([call for call in self.kodi.calls if call[0] == "Player.Stop"])

    def test_repeat_change_after_stop_cannot_be_adopted_even_if_it_matches_prior(self):
        self.kodi.repeat = "all"

        def human_repeat(_controller, method, _params):
            if method == "Player.Stop":
                self.kodi.repeat = "all"

        controller, prepared = self.stalled_controller(human_repeat)
        self.assert_repeat_supersedes_fallback(controller, prepared)
        self.assertIsNone(self.kodi.file)

    def test_repeat_change_after_restoration_cannot_be_adopted_for_fallback(self):
        def human_repeat(_controller, method, params):
            if method == "Player.SetRepeat" and params["repeat"] == "off":
                self.kodi.repeat = "all"

        controller, prepared = self.stalled_controller(human_repeat)
        self.assert_repeat_supersedes_fallback(controller, prepared)
        self.assertIsNone(self.kodi.file)

    def test_cancellation_during_each_final_fallback_read_prevents_dispatch(self):
        for intent in ("stop", "close"):
            for boundary in ("Player.GetActivePlayers", "Player.GetProperties"):
                with self.subTest(intent=intent, boundary=boundary):
                    self.kodi.calls.clear()
                    injected = []

                    def cancel(controller, method, _params):
                        if (
                            method == boundary
                            and controller._fallback_repeat is not None
                            and controller._pending is not None
                            and controller._pending.get("phase") == "opening"
                            and not injected
                        ):
                            injected.append(method)
                            event = (
                                controller._cancelled
                                if intent == "stop"
                                else controller._closing
                            )
                            event.set()

                    controller, prepared = self.stalled_controller(cancel)
                    with self.assertRaisesRegex(
                        fixtures.fireplace.FireplaceError, "cancelled"
                    ):
                        controller.start("cancelled-during-validation")
                    self.assertEqual(injected, [boundary])
                    self.assertEqual(prepared, ["local-0", "local-1"])
                    self.assertEqual(
                        sum(method == "Player.Open" for method, _ in self.kodi.calls), 1
                    )
                    pending = self.selection()["pending"]
                    self.assertTrue(pending is None or pending["phase"] == "prepared")
                    self.assertIsNone(self.kodi.file)
                    controller.close()

    def test_actual_bridge_rejection_before_open_leaves_no_uncertain_journal(self):
        from test_manifest_http import bridge

        controller = self.multi()
        now = [100.0]
        self.kodi.started = now[0]
        dispatched = []
        expired = []

        def transport(method, params, *, timeout):
            dispatched.append(method)
            return {
                "ok": True,
                "body": {"result": self.kodi.rpc(method, params, timeout)},
            }

        def callback(method, params, deadline):
            if method == "Player.Open":
                expired.append(method)
                now[0] = deadline
            return bridge._fireplace_rpc(method, params, deadline)

        controller._rpc = callback
        with (
            patch.object(
                fixtures.fireplace.time, "monotonic", side_effect=lambda: now[0]
            ),
            patch.object(bridge, "_fireplace_module", fixtures.fireplace),
            patch.object(bridge, "kodi_jsonrpc", side_effect=transport),
        ):
            with self.assertRaisesRegex(
                fixtures.fireplace.FireplaceError, "sources_unavailable"
            ):
                controller.start("undispatched-open")
        self.assertEqual(expired, ["Player.Open"])
        self.assertNotIn("Player.Open", dispatched)
        self.assertIsNone(self.selection()["pending"])
        self.assertEqual(controller.status()["state"], "error")
        self.assertIsNone(self.kodi.file)
        self.assertEqual(controller.stop()["state"], "idle")

    def test_repeat_supersession_during_pending_monitor_never_stops_the_video(self):
        from test_rotation_stop_boundaries import StopBoundaryCases

        controller = StopBoundaryCases.looped_pending(self)
        expected = self.kodi.file
        self.kodi.repeat = "off"
        proof = controller.observe()
        self.assertFalse(proof["owned"])
        self.assertIsNone(self.selection()["pending"])
        self.assertEqual(self.kodi.file, expected)
        self.assertEqual(self.kodi.repeat, "off")
        controller.observe()
        self.assertFalse([call for call in self.kodi.calls if call[0] == "Player.Stop"])

    def test_expiry_after_opening_journal_is_definitively_undispatched(self):
        controller = self.multi()
        now = [100.0]
        self.kodi.started = now[0]
        store = controller._store
        expired = []

        def journal_then_expire():
            store()
            if (
                controller._pending is not None
                and controller._pending["phase"] == "opening"
            ):
                expired.append(True)
                now[0] = 102.0

        with (
            patch.object(
                fixtures.fireplace.time, "monotonic", side_effect=lambda: now[0]
            ),
            patch.object(controller, "_store", side_effect=journal_then_expire),
        ):
            with self.assertRaisesRegex(
                fixtures.fireplace.FireplaceError, "sources_unavailable"
            ):
                controller.start("expired-before-validation")
        self.assertEqual(expired, [True])
        self.assertFalse([call for call in self.kodi.calls if call[0] == "Player.Open"])
        self.assertIsNone(self.selection()["pending"])
        self.assertEqual(controller.status()["state"], "error")

    def test_initial_repeat_arm_preserves_positive_supersession_through_failure(self):
        def human_repeat(_controller, method, params):
            if method == "Player.SetRepeat" and params["repeat"] == "one":
                self.kodi.repeat = "all"

        controller, prepared = self.stalled_controller(human_repeat)
        self.assert_repeat_supersedes_fallback(controller, prepared)
        self.assertEqual(self.kodi.file, str(self.root / "0.webm"))
        self.assertFalse([call for call in self.kodi.calls if call[0] == "Player.Stop"])

    def test_owned_replacement_does_not_rearm_after_repeat_supersession(self):
        controller = self.multi()
        first = controller.start("first")
        native = controller._rpc
        self.kodi.calls.clear()

        def human_repeat(method, params, deadline):
            result = native(method, params, deadline)
            if method == "Player.Open":
                self.kodi.repeat = "all"
            return result

        controller._rpc = human_repeat
        with self.assertRaisesRegex(
            fixtures.fireplace.FireplaceError, "playback_changed"
        ):
            controller.start("superseded-replacement")
        self.assertEqual(self.kodi.file, str(self.root / "1.webm"))
        self.assertEqual(self.kodi.repeat, "all")
        self.assertIsNone(self.selection()["pending"])
        self.assertEqual(
            self.selection()["last_successful_source_id"], first["source_id"]
        )
        controller.observe()
        self.assertFalse(
            [
                call
                for call in self.kodi.calls
                if call[0] in {"Player.Stop", "Player.SetRepeat"}
            ]
        )

    def test_stop_delivers_supersession_without_a_failing_followup_read(self):
        stopped = []

        def human_takeover(_controller, method, _params):
            if method == "Player.Stop":
                stopped.append(True)
                self.kodi.file = str(self.root / "0.webm")
                self.kodi.speed = 1
                self.kodi.repeat = "all"

        controller, prepared = self.stalled_controller(human_takeover)
        read = controller._owner._read
        final_reads = []

        def fail_after_supersession(deadline):
            if stopped and controller._owner._owner is None:
                final_reads.append(True)
                raise OSError("This final native read must not discard supersession")
            return read(deadline)

        with patch.object(
            controller._owner, "_read", side_effect=fail_after_supersession
        ):
            with self.assertRaisesRegex(
                fixtures.fireplace.FireplaceError, "playback_changed"
            ):
                controller.start("superseded-stop")
        self.assertEqual(stopped, [True])
        self.assertEqual(final_reads, [])
        self.assertEqual(prepared, ["local-0"])
        self.assertIsNone(self.selection()["pending"])
        controller.observe()
        self.assertEqual(self.kodi.file, str(self.root / "0.webm"))
        self.assertEqual(self.kodi.repeat, "all")
        self.assertEqual(
            sum(method == "Player.Stop" for method, _ in self.kodi.calls), 1
        )

    def test_repeat_change_during_durable_arm_is_returned_before_any_repeat_mutation(
        self,
    ):
        controller, prepared = self.stalled_controller()
        persist = controller._owner._persist

        def human_repeat(receipt):
            persist(receipt)
            if receipt["phase"] == "armed":
                self.kodi.repeat = "all"

        with patch.object(controller._owner, "_persist", side_effect=human_repeat):
            self.assert_repeat_supersedes_fallback(controller, prepared)
        self.assertEqual(self.kodi.file, str(self.root / "0.webm"))
        self.assertFalse(
            [
                call
                for call in self.kodi.calls
                if call[0] in {"Player.Stop", "Player.SetRepeat"}
            ]
        )
