"""Routing boundary checks runnable without HA; behavior is tested in HA too."""
from pathlib import Path
import unittest

import yaml


PACKAGE = Path(__file__).resolve().parents[2] / "packages/universal_remote.yaml"


def steps(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from steps(child)
    elif isinstance(value, list):
        for child in value:
            yield from steps(child)


class UniversalRemoteYamlContractTests(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load(PACKAGE.read_text())

    def test_mode_wrappers_return_the_canonical_service_response(self):
        for script, service in [("remote_modes_prepare", "remote_prepare"),
                                ("remote_mode_select_pair", "remote_select")]:
            with self.subTest(script=script):
                sequence = self.doc["script"][script]["sequence"]
                self.assertEqual(sequence[0]["action"], f"ekf_appliances.{service}")
                response = sequence[0]["response_variable"]
                self.assertEqual(sequence[-1]["response_variable"], response)
                self.assertIn("stop", sequence[-1])

    def test_queued_wrappers_resolve_again_before_selecting_device(self):
        for script in ("remote_nav", "remote_volume", "remote_media",
                       "t9_send_char", "t9_commit", "t9_backspace", "tv_search"):
            with self.subTest(script=script):
                first = self.doc["script"][script]["sequence"][0]
                self.assertEqual(first["action"], "ekf_appliances.remote_resolve")
                self.assertIn("response_variable", first)

    def test_no_projector_or_kodi_transport_bypasses_final_dispatch(self):
        for step in steps(self.doc):
            action = step.get("action")
            if not isinstance(action, str) or action == "ekf_appliances.remote_dispatch":
                continue
            with self.subTest(action=action, target=step.get("target")):
                self.assertFalse(action.startswith("kodi."))
                self.assertFalse(action.startswith("rest_command.host_kodi_"))
                if action.startswith(("button.", "media_player.", "remote.")):
                    payload = str(step.get("target", {})) + str(step.get("data", {}))
                    self.assertNotIn("projector", payload)
                    self.assertNotIn("media_player.home", payload)
                    self.assertNotIn("target_player", payload)

    def test_power_uses_the_atomic_latch_service(self):
        automation = next(a for a in self.doc["automation"]
                          if a["id"] == "universal_remote_dispatcher")
        power = next(branch for branch in automation["action"][1]["choose"]
                     if branch["conditions"] == "{{ button == 'power' }}")
        self.assertEqual(power["sequence"][0]["action"], "ekf_appliances.remote_power")
        self.assertEqual(power["sequence"][0]["data"]["pair"], "{{ route.pair }}")

    def test_kodi_queued_text_checks_latch_at_its_final_transport(self):
        sequence = self.doc["script"]["t9_kodi_text"]["sequence"]
        dispatch = sequence[0]
        self.assertEqual(dispatch["action"], "ekf_appliances.remote_dispatch")
        self.assertEqual(dispatch["data"]["domain"], "rest_command")
        self.assertEqual(dispatch["data"]["service"], "host_kodi_text_input")
        self.assertEqual(dispatch["data"]["pair"], "projector")
        self.assertIs(dispatch["data"]["return_response"], True)
        self.assertEqual(sequence[-1]["response_variable"], dispatch["response_variable"])

    def test_kodi_reads_forward_the_native_service_response(self):
        reads = [step for step in steps(self.doc["script"]["kodi_play_next_episode"])
                 if step.get("response_variable") in {"shows_resp", "eps_resp"}]
        self.assertEqual(len(reads), 2)
        for step in reads:
            self.assertEqual(step["action"], "ekf_appliances.remote_dispatch")
            self.assertIs(step["data"]["return_response"], True)

    def test_legacy_callbacks_do_not_create_a_second_timer_or_wake_authority(self):
        for script in ("remote_pair_wake", "remote_input_single", "remote_input_finish"):
            actions = [step["action"] for step in steps(self.doc["script"][script])
                       if isinstance(step.get("action"), str)]
            self.assertEqual(actions, ["ekf_appliances.remote_resolve"])
        for step in steps(self.doc):
            self.assertNotIn(step.get("action") if isinstance(step.get("action"), str) else None,
                             {"timer.start", "timer.cancel", "ekf_appliances.wake_if_current"})

    def test_input_preserves_fractional_physical_reception_time(self):
        automation = next(a for a in self.doc["automation"]
                          if a["id"] == "universal_remote_dispatcher")
        timestamp = automation["action"][0]["data"]["received_ms"]
        self.assertIn("last_received", timestamp)
        self.assertNotIn("| int", timestamp)


if __name__ == "__main__":
    unittest.main()
