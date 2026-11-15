"""Bounded fireplace repeat ownership against the real host controller."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts/host/ha_kodi_fireplace.py"


class SimulatedCrash(BaseException):
    """Exercise simulated crash behavior with deterministic native fixtures."""


class Kodi:
    """Model kodi state and controlled responses for regression checks."""

    def __init__(self) -> None:
        self.active = None
        self.file = None
        self.speed = 0
        self.repeat = "off"
        self.calls = []
        self.available = True
        self.ignore_repeat = False
        self.crash_after_repeat = False
        self.open_foreign = False
        self.open_uncertain = False
        self.preferred_audio = False
        self.before_call = None

    def rpc(self, method, params, deadline) -> Any:
        self.calls.append((method, dict(params)))
        if self.before_call:
            self.before_call(method, params)
        if not self.available:
            error_message = "transport unavailable"
            raise OSError(error_message)
        handlers = {
            "Player.GetActivePlayers": self._active_players,
            "Player.GetItem": self._item,
            "Player.GetProperties": self._properties,
            "Player.Open": self._open,
            "Player.SetRepeat": self._repeat,
            "Player.PlayPause": self._pause,
            "Player.Stop": self._stop,
        }
        if method not in handlers:
            raise AssertionError(method)
        return handlers[method](params)

    def _active_players(self, params) -> list[Any]:
        if self.active is None:
            return []
        return [
            {"playerid": self.active, "type": "video" if self.active == 1 else "audio"}
        ]

    def _item(self, params) -> dict[str, Any]:
        if self.active != 1:
            error_message = "no video"
            raise RuntimeError(error_message)
        return {"item": {"file": self.file}}

    def _properties(self, params) -> dict[str, Any]:
        if self.active == 0 or (self.active is None and self.preferred_audio):
            error_message = "video playlist is not selected"
            raise RuntimeError(error_message)
        values = {"repeat": self.repeat, "speed": self.speed}
        return {key: values[key] for key in params["properties"]}

    def _open(self, params) -> str:
        self.active, self.speed = 1, 1
        self.file = (
            "/private/other-film.mkv" if self.open_foreign else params["item"]["file"]
        )
        if self.open_uncertain:
            error_message = "Open outcome is uncertain"
            raise TimeoutError(error_message)
        return "OK"

    def _repeat(self, params) -> str:
        if not self.ignore_repeat:
            self.repeat = params["repeat"]
        if self.crash_after_repeat:
            self.crash_after_repeat = False
            raise SimulatedCrash
        return "OK"

    def _pause(self, params) -> dict[str, Any]:
        self.speed = 1 if params["play"] else 0
        return {"speed": self.speed}

    def _stop(self, params) -> str:
        if self.active != 1:
            error_message = "must never stop foreign audio"
            raise AssertionError(error_message)
        self.active, self.file, self.speed = None, None, 0
        return "OK"

    def changes(self) -> list[Any]:
        return [
            call
            for call in self.calls
            if call[0]
            in {"Player.Open", "Player.Stop", "Player.SetRepeat", "Player.PlayPause"}
        ]


class HostFireplaceTests(unittest.TestCase):
    """Exercise host fireplace tests behavior with deterministic native fixtures."""

    def setUp(self) -> None:
        self.assertTrue(MODULE.exists(), "Fixed fireplace repeat controller is missing")
        spec = importlib.util.spec_from_file_location("ha_kodi_fireplace", MODULE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "fireplace.webm"
        self.media.write_bytes(b"immutable fixture")
        self.sha = hashlib.sha256(self.media.read_bytes()).hexdigest()
        self.receipt = self.root / "owner.json"
        self.kodi = Kodi()
        self.controllers = []
        self.controller = self.create()

    def create(self) -> Any:
        controller = self.module.OwnedPlayback(
            self.kodi.rpc,
            self.receipt,
            media_path=self.media,
            expected_sha256=self.sha,
            auto_monitor=False,
            operation_timeout=1.0,
            poll_interval=0.001,
        )
        self.controllers.append(controller)
        return controller

    def tearDown(self) -> None:
        for controller in getattr(self, "controllers", []):
            controller.close()
        if hasattr(self, "temp"):
            self.temp.cleanup()

    def test_single_file_loop_requires_explicit_setrepeat_and_actual_readback(
        self,
    ) -> None:
        proof = self.controller.start()
        self.assertEqual(proof["state"], "playing")
        self.assertTrue(proof["owned"] and proof["file_matches"])
        self.assertEqual(proof["repeat"], "one")
        self.assertIsInstance(proof["observed_at"], str)
        self.assertEqual(
            [m for m, _ in self.kodi.changes()], ["Player.Open", "Player.SetRepeat"]
        )
        self.assertEqual(self.kodi.changes()[-1][1], {"playerid": 1, "repeat": "one"})
        self.assertNotIn("options", self.kodi.changes()[0][1])
        self.assertTrue(self.receipt.exists())

    def test_repeated_request_does_not_reopen_or_forget_original_repeat(self) -> None:
        self.kodi.repeat = "all"
        now = [99.0]
        with patch.object(self.module.time, "monotonic", side_effect=lambda: now[0]):
            self.controller.start()
            self.kodi.calls.clear()
            self.assertEqual(self.controller.start()["state"], "playing")
            self.assertEqual(self.kodi.changes(), [])
            self.kodi.active, self.kodi.file, self.kodi.speed = None, None, 0
            now[0] = 100.0
            self.controller.observe()
            now[0] = 102.5
            self.controller.observe()
        self.assertEqual(self.kodi.repeat, "all")
        self.assertFalse(self.receipt.exists())

    def test_store_failure_prevents_any_repeat_mutation(self) -> None:
        with (
            patch.object(
                self.controller, "_persist", side_effect=OSError("disk failure")
            ),
            self.assertRaises(self.module.FireplaceError),
        ):
            self.controller.start()
        self.assertEqual(self.kodi.repeat, "off")
        self.assertNotIn("Player.SetRepeat", [m for m, _ in self.kodi.calls])
        self.assertFalse(self.controller.status()["owned"])

    def test_exit_to_other_video_restores_only_owned_repeat_and_hides_filename(
        self,
    ) -> None:
        self.controller.start()
        self.kodi.file = "/private/other-film.mkv"
        self.kodi.calls.clear()
        proof = self.controller.observe()
        self.assertEqual(self.kodi.repeat, "off")
        self.assertFalse(proof["owned"])
        self.assertEqual(
            self.kodi.changes(),
            [("Player.SetRepeat", {"playerid": 1, "repeat": "off"})],
        )
        self.assertNotIn("private", json.dumps(proof))
        self.assertFalse(self.receipt.exists())

    def test_external_repeat_changes_are_preserved(self) -> None:
        for mode in ("off", "all"):
            with self.subTest(mode=mode):
                self.controller.start()
                self.kodi.repeat = mode
                self.kodi.calls.clear()
                proof = self.controller.observe()
                self.assertEqual(self.kodi.repeat, mode)
                self.assertFalse(proof["owned"])
                self.assertEqual(self.kodi.changes(), [])

    def test_pause_retains_loop_ownership_stop_restores_previous_repeat(self) -> None:
        self.controller.start()
        self.kodi.speed = 0
        self.kodi.calls.clear()
        proof = self.controller.observe()
        self.assertEqual(proof["state"], "paused")
        self.assertTrue(proof["owned"])
        self.assertEqual(self.kodi.changes(), [])
        proof = self.controller.stop()
        self.assertEqual(
            [m for m, _ in self.kodi.changes()], ["Player.Stop", "Player.SetRepeat"]
        )
        self.assertFalse(proof["owned"])
        self.assertEqual(self.kodi.repeat, "off")

    def test_relaunch_resumes_owned_pause_without_reopen_or_losing_prior_mode(
        self,
    ) -> None:
        self.kodi.repeat = "all"
        self.controller.start()
        self.kodi.speed = 0
        self.controller.observe()
        self.kodi.calls.clear()
        proof = self.controller.start()
        self.assertEqual(proof["state"], "playing")
        self.assertEqual(
            self.kodi.changes(), [("Player.PlayPause", {"playerid": 1, "play": True})]
        )
        self.controller.stop()
        self.assertEqual(self.kodi.repeat, "all")

    def test_failed_rpc_never_claims_playing_and_preserves_recovery_receipt(
        self,
    ) -> None:
        now = [99.0]
        with patch.object(self.module.time, "monotonic", side_effect=lambda: now[0]):
            self.controller.start()
            self.kodi.available = False
            proof = self.controller.observe()
            self.assertNotEqual(proof["state"], "playing")
            self.assertTrue(self.receipt.exists())
            self.kodi.available = True
            self.kodi.active, self.kodi.file, self.kodi.speed = None, None, 0
            now[0] = 100.0
            self.controller.observe()
            now[0] = 102.5
            self.controller.observe()
        self.assertEqual(self.kodi.repeat, "off")
        self.assertFalse(self.receipt.exists())

    def test_offline_receipt_recovers_after_canonical_restart_before_new_open(
        self,
    ) -> None:
        self.controller.start()
        self.kodi.available = False
        self.assertEqual(self.controller.observe()["state"], "unknown")
        self.assertTrue(self.receipt.exists())
        # HA's canonical pair starts Kodi first. Native readiness is idle, while
        # no active player exists; the next fixed POST must settle old ownership.
        self.kodi.available = True
        self.kodi.active, self.kodi.file, self.kodi.speed = None, None, 0
        self.kodi.calls.clear()
        proof = self.controller.start()
        self.assertEqual(proof["state"], "playing")
        self.assertEqual(
            self.kodi.changes(),
            [
                ("Player.SetRepeat", {"playerid": 1, "repeat": "off"}),
                ("Player.Open", {"item": {"file": str(self.media)}}),
                ("Player.SetRepeat", {"playerid": 1, "repeat": "one"}),
            ],
        )
        self.controller.stop()
        self.assertEqual(self.kodi.repeat, "off")

    def test_new_request_can_recover_video_context_after_audio_finished(self) -> None:
        self.controller.start()
        # Kodi21.2 GetPlayer/GetPlaylist can still resolve to the previous audio
        # playlist after it stopped. Video properties then return FailedToExecute.
        self.kodi.active, self.kodi.file, self.kodi.speed = None, None, 0
        self.kodi.preferred_audio = True
        self.assertEqual(self.controller.observe()["state"], "unknown")
        self.assertTrue(self.receipt.exists())
        self.kodi.calls.clear()
        proof = self.controller.start()
        self.assertEqual(proof["state"], "playing")
        self.assertEqual(
            self.kodi.changes(), [("Player.Open", {"item": {"file": str(self.media)}})]
        )
        self.assertEqual(json.loads(self.receipt.read_text())["prior_repeat"], "off")
        self.kodi.preferred_audio = False
        self.controller.stop()
        self.assertEqual(self.kodi.repeat, "off")
        # A failed properties query with an active video does NOT qualify for
        # that recovery. It must not replace another active user's movie.
        self.controller.start()
        self.kodi.file = "/private/active-film.mkv"

        def fail_properties(method, _params) -> None:
            if method == "Player.GetProperties":
                error_message = "query failed"
                raise RuntimeError(error_message)

        self.kodi.before_call = fail_properties
        self.kodi.calls.clear()
        with self.assertRaises(self.module.FireplaceError):
            self.controller.start()
        self.assertEqual(self.kodi.changes(), [])
        self.kodi.before_call = None

    def test_other_audio_never_gets_video_repeat_or_stop_commands(self) -> None:
        self.controller.start()
        self.kodi.active = 0
        self.kodi.calls.clear()
        proof = self.controller.observe()
        self.assertNotEqual(proof["state"], "playing")
        self.assertTrue(self.receipt.exists())
        self.assertEqual(self.kodi.calls, [("Player.GetActivePlayers", {})])
        self.controller.stop()
        self.assertEqual(self.kodi.changes(), [])
        self.kodi.active, self.kodi.file, self.kodi.speed = None, None, 0
        self.controller.observe()
        self.assertEqual(self.kodi.repeat, "off")

    def test_explicit_new_request_can_replace_audio_preserving_prior_and_never_retry_open(
        self,
    ) -> None:
        self.kodi.repeat = "all"
        self.controller.start()
        self.kodi.active, self.kodi.file, self.kodi.speed = 0, "/private/song.flac", 1
        self.controller.observe()
        self.kodi.calls.clear()
        proof = self.controller.start()
        self.assertEqual(proof["state"], "playing")
        self.assertEqual(
            self.kodi.changes(), [("Player.Open", {"item": {"file": str(self.media)}})]
        )
        first_open = next(
            i for i, call in enumerate(self.kodi.calls) if call[0] == "Player.Open"
        )
        self.assertTrue(
            all(
                method == "Player.GetActivePlayers"
                for method, _ in self.kodi.calls[:first_open]
            )
        )
        self.assertEqual(json.loads(self.receipt.read_text())["prior_repeat"], "all")
        self.controller.stop()
        self.assertEqual(self.kodi.repeat, "all")
        self.controller.start()
        self.kodi.active, self.kodi.file, self.kodi.speed = 0, "/private/song.flac", 1
        self.kodi.open_uncertain = True
        self.kodi.calls.clear()
        with self.assertRaises(self.module.FireplaceError):
            self.controller.start()
        self.assertEqual(
            self.kodi.changes(), [("Player.Open", {"item": {"file": str(self.media)}})]
        )
        self.assertEqual(json.loads(self.receipt.read_text())["prior_repeat"], "all")
        self.assertNotEqual(self.controller.status()["state"], "playing")

    def test_crash_between_setrepeat_and_readback_recovers_from_durable_owner(
        self,
    ) -> None:
        self.kodi.crash_after_repeat = True
        with self.assertRaises(SimulatedCrash):
            self.controller.start()
        self.assertEqual(self.kodi.repeat, "one")
        saved = json.loads(self.receipt.read_text())
        self.assertEqual(saved["prior_repeat"], "off")
        self.assertEqual(saved["phase"], "armed")
        self.controller.close()
        recovered = self.create()
        self.kodi.calls.clear()
        self.assertEqual(recovered.observe()["state"], "playing")
        self.assertEqual(self.kodi.changes(), [])
        self.kodi.file = "/private/next.mkv"
        recovered.observe()
        self.assertEqual(self.kodi.repeat, "off")
        self.assertFalse(self.receipt.exists())

    def test_restart_receipt_without_repeat_mutation_does_not_replay_start(
        self,
    ) -> None:
        self.kodi.crash_after_repeat = True
        with self.assertRaises(SimulatedCrash):
            self.controller.start()
        self.kodi.repeat = "off"
        self.controller.close()
        recovered = self.create()
        self.kodi.calls.clear()
        proof = recovered.observe()
        self.assertFalse(proof["owned"])
        self.assertEqual(self.kodi.changes(), [])

    def test_successful_rpc_with_unverified_repeat_is_explicit_failure(self) -> None:
        self.kodi.ignore_repeat = True
        with self.assertRaises(self.module.FireplaceError):
            self.controller.start()
        self.assertNotEqual(self.controller.status()["state"], "playing")
        self.assertEqual(sum(m == "Player.SetRepeat" for m, _ in self.kodi.calls), 1)

    def test_wrong_opened_file_never_receives_repeat_or_false_success(self) -> None:
        self.kodi.open_foreign = True
        with self.assertRaises(self.module.FireplaceError):
            self.controller.start()
        self.assertNotIn("Player.SetRepeat", [m for m, _ in self.kodi.calls])
        self.assertFalse(self.controller.status()["owned"])

    def test_external_repeat_change_during_cleanup_preflight_is_not_overwritten(
        self,
    ) -> None:
        self.controller.start()
        self.kodi.file = "/private/next.mkv"
        reads = 0

        def change(method, _params) -> None:
            nonlocal reads
            if method == "Player.GetProperties":
                reads += 1
                if reads == 2:
                    self.kodi.repeat = "all"

        self.kodi.before_call = change
        self.kodi.calls.clear()
        self.controller.observe()
        self.assertEqual(self.kodi.repeat, "all")
        self.assertEqual(self.kodi.changes(), [])

    def test_content_changed_during_durable_arm_never_receives_repeat(self) -> None:
        persist = self.controller._persist

        def change_after_store(receipt) -> None:
            persist(receipt)
            self.kodi.file = "/private/new-film.mkv"

        with (
            patch.object(self.controller, "_persist", side_effect=change_after_store),
            self.assertRaises(self.module.FireplaceError),
        ):
            self.controller.start()
        self.assertNotIn("Player.SetRepeat", [method for method, _ in self.kodi.calls])
        self.assertEqual(self.kodi.repeat, "off")
        self.assertFalse(self.receipt.exists())

    def test_external_mode_changed_during_durable_cleanup_is_preserved(self) -> None:
        self.controller.start()
        self.kodi.file = "/private/new-film.mkv"
        persist = self.controller._persist

        def change_after_store(receipt) -> None:
            persist(receipt)
            self.kodi.repeat = "all"

        self.kodi.calls.clear()
        with patch.object(self.controller, "_persist", side_effect=change_after_store):
            self.controller.observe()
        self.assertEqual(self.kodi.repeat, "all")
        self.assertEqual(self.kodi.changes(), [])
        self.assertFalse(self.receipt.exists())

    def test_shutdown_prevents_new_rpc_and_does_not_clear_durable_receipt(self) -> None:
        self.controller.start()
        self.controller.close()
        self.kodi.calls.clear()
        with self.assertRaises(self.module.FireplaceError):
            self.controller.start()
        self.assertEqual(self.kodi.calls, [])
        self.assertTrue(self.receipt.exists())

    def test_asset_mutation_is_rejected_before_player_open(self) -> None:
        self.media.write_bytes(b"wrong")
        with self.assertRaises(self.module.FireplaceError):
            self.controller.start()
        self.assertEqual(self.kodi.changes(), [])


class HostFireplaceBoundaryTests(unittest.TestCase):
    """Exercise host fireplace boundary tests behavior with deterministic native fixtures."""

    def setUp(self) -> None:

        self.BytesIO = BytesIO
        spec = importlib.util.spec_from_file_location(
            "ha_host_actions_fireplace_fixture",
            ROOT / "scripts/host/ha-host-actions.py",
        )
        self.host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.host)
        self.controller = Mock()
        self.controller.start.return_value = {
            "state": "playing",
            "owned": True,
            "file_matches": True,
            "repeat": "one",
        }
        self.controller.status.return_value = {"state": "idle", "owned": False}
        self.factory = self.host._fireplace_controller
        self.host._fireplace_controller = Mock(return_value=self.controller)

    def request(self, method, payload=None, *, authorized=True) -> Any:
        handler = object.__new__(self.host.Handler)
        handler.command = method
        handler.path = "/kodi/fireplace"
        handler.authorized = lambda: authorized
        handler.read_json_body = lambda: payload
        results = []
        handler.send_json = lambda status, body: results.append((status, body))
        handler.handle_route()
        return results[0]

    def test_existing_http_auth_precedes_both_new_routes(self) -> None:
        for method in ("GET", "POST"):
            self.assertEqual(
                self.request(method, {"action": "start"}, authorized=False)[0], 403
            )
        self.host._fireplace_controller.assert_not_called()
        self.assertEqual(
            self.request("GET"), (200, {"ok": True, "state": "idle", "owned": False})
        )
        self.assertEqual(self.request("POST", {"action": "start"})[0], 200)
        self.controller.start.assert_called_once_with(None)

    def test_clients_cannot_supply_any_file_url_or_rpc_method(self) -> None:
        for payload in (
            {},
            {"action": []},
            {"action": "Player.Open"},
            {"action": "start", "file": "/private/file"},
            {"action": "start", "url": "https://example.invalid"},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.request("POST", payload)[0], 400)
        self.host._fireplace_controller.assert_not_called()

    def test_player_mutations_have_room_for_plugin_startup_within_deadline(
        self,
    ) -> None:
        response = {"ok": True, "body": {"result": "OK"}}
        with (
            patch.object(self.host.time, "monotonic", return_value=10),
            patch.object(self.host, "kodi_jsonrpc", return_value=response) as rpc,
        ):
            for method in ("Player.Open", "Player.Stop"):
                self.host._fireplace_rpc(method, {}, 20)
                self.assertEqual(rpc.call_args.kwargs["timeout"], 8.0)
                self.host._fireplace_rpc(method, {}, 10.4)
                self.assertAlmostEqual(rpc.call_args.kwargs["timeout"], 0.4)
            self.host._fireplace_rpc("Player.GetItem", {}, 20)
            self.assertEqual(rpc.call_args.kwargs["timeout"], 1.0)

    def test_controller_creation_starts_recovery_monitor(self) -> None:
        with patch.object(
            self.host._fireplace_module,
            "FireplaceController",
            return_value=self.controller,
        ):
            self.assertIs(self.factory(), self.controller)
            self.assertIs(self.factory(), self.controller)
        self.controller.start_monitor.assert_called_once_with()

    def test_malformed_request_logging_needs_no_parsed_path_or_command(self) -> None:
        handler = object.__new__(self.host.Handler)
        handler.client_address = ("127.0.0.1", 45678)
        with patch.object(self.host.logging, "getLogger") as logger:
            handler.log_message("code %d, message %s", 400, "invalid request")
        logger.return_value.info.assert_called_once()


if __name__ == "__main__":
    unittest.main()
