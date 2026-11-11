"""Wire behavior fixtures independently derived from the saved remote protocols."""
import base64
import math
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components" / "ekf_appliances"))
import protocol


class HaierProtocolTests(unittest.TestCase):
    def test_model_specific_off_frame_includes_checksum(self):
        self.assertEqual(
            protocol.haier_state("off", 24, "auto", "off", "none"),
            bytes.fromhex("A6 90 00 00 00 A0 00 80 00 00 00 00 05 5B"),
        )

    def test_cool_frame_contains_every_setting_and_checksum(self):
        self.assertEqual(
            protocol.haier_state("cool", 24, "auto", "off", "none"),
            bytes.fromhex("A6 80 00 00 40 A0 00 20 00 00 00 00 05 2B"),
        )

    def test_heat_boost_and_vertical_swing_frame(self):
        self.assertEqual(
            protocol.haier_state("heat", 30, "high", "vertical", "boost"),
            bytes.fromhex("A6 EC 00 00 40 20 40 80 00 00 00 00 05 B7"),
        )

    def test_dry_sleep_frame_uses_full_state_power_command(self):
        self.assertEqual(
            protocol.haier_state("dry", 16, "low", "off", "sleep"),
            bytes.fromhex("A6 00 00 00 40 60 00 40 80 00 00 00 05 0B"),
        )

    def test_heat_matches_independent_working_health_off_capture(self):
        # IRremoteESP8266 issue 1894: original remote, heat 18C, low fan.
        # Its swing-down and Temp-Up button differ from this adapter's
        # swing-off / full-state Power command; the other fields must match.
        working = bytes.fromhex("A6 2A 00 00 40 60 00 80 00 00 00 00 00 F0")
        state = protocol.haier_state("heat", 18, "low", "off", "none")
        self.assertEqual(state[0], working[0])
        self.assertEqual(state[1] >> 4, working[1] >> 4)
        self.assertEqual(state[2:12], working[2:12])
        self.assertEqual(state[1] & 15, 0)
        self.assertEqual(state[12:], bytes.fromhex("05 EB"))

    def test_health_stays_disabled_for_all_supported_settings(self):
        self.assertEqual(protocol.PRESETS, ("none", "sleep", "boost"))
        for mode in ("off", "auto", "cool", "heat", "dry", "fan_only"):
            for fan in ("auto", "low", "medium", "high"):
                for preset in ("none", "sleep", "boost"):
                    for swing in ("off", "vertical"):
                        with self.subTest(mode=mode, fan=fan, preset=preset, swing=swing):
                            state = protocol.haier_state(mode, 24, fan, swing, preset)
                            self.assertEqual(state[3] & 0x02, 0)
                            self.assertEqual(state[-1], sum(state[:-1]) & 255)

    def test_each_valid_combination_has_full_state_and_checksum(self):
        for mode in ("off", "auto", "cool", "heat", "dry", "fan_only"):
            for temperature in (16, 24, 30):
                state = protocol.haier_state(mode, temperature, "auto", "off", "none")
                self.assertEqual(len(state), 14)
                self.assertEqual(state[12], 5)
                self.assertEqual(state[-1], sum(state[:-1]) & 255)

    def test_rejects_invalid_climate_inputs(self):
        for position, value in (
            (0, "heat_cool"), (1, 15), (1, 31), (1, 24.5), (1, math.nan),
            (1, math.inf), (1, True), (2, "turbo"), (3, "horizontal"),
            (4, "away"), (4, "comfort"),
        ):
            values = ["cool", 24, "auto", "off", "none"]
            values[position] = value
            with self.subTest(values=values), self.assertRaises(ValueError):
                protocol.haier_state(*values)

    def test_single_frame_has_msb_first_prefix_and_tuya_terminal_space(self):
        frame = protocol.haier_pulses(bytes.fromhex("A6 90 00 00 00 A0 00 80 00 00 00 00 05 5B"))
        self.assertEqual(len(frame), 230)
        self.assertEqual(frame[:4], (3000, 3000, 3000, 4300))
        self.assertEqual(frame[4:20], (520,1650,520,650,520,1650,520,650,520,650,520,1650,520,1650,520,650))
        self.assertEqual(frame[-2:], (520,5000))
        self.assertLess(max(frame), 50001)

    def test_rejects_truncated_and_bad_checksum_frames(self):
        for state in (b"", bytes(13), bytes.fromhex("A6 90 00 02 00 A0 00 80 00 00 00 00 05 00")):
            with self.assertRaises(ValueError):
                protocol.haier_pulses(state)


class ProjectorProtocolTests(unittest.TestCase):
    def test_power_is_saved_extended_nec_payload(self):
        pulses = protocol.projector_pulses("power")
        self.assertEqual(pulses[:2], (9000, 4500))
        self.assertEqual(pulses[-2:], (562, 5000))
        self.assertEqual(len(pulses), 68)
        data = bytes(sum((pulses[3 + 2 * (byte * 8 + bit)] > 1000) << bit for bit in range(8)) for byte in range(4))
        self.assertEqual(data, bytes.fromhex("00 BD 01 FE"))

    def test_all_twelve_commands_match_recorded_payloads(self):
        expected = {"power":0x01,"play_pause":0x5F,"source":0x04,"up":0x0B,
                    "down":0x0F,"left":0x49,"right":0x4A,"menu":0x0A,
                    "back":0x54,"vol_down":0x10,"mute":0x6A,"vol_up":0x0C}
        self.assertEqual(set(protocol.PROJECTOR_COMMANDS), set(expected))
        for key, command in expected.items():
            pulses = protocol.projector_pulses(key)
            decoded = sum((pulses[3 + 2*i] > 1000) << i for i in range(32))
            self.assertEqual(decoded, ((command ^ 255) << 24) | (command << 16) | 0xBD00)

    def test_unknown_command_is_rejected(self):
        with self.assertRaises(ValueError):
            protocol.projector_pulses("ok")


class TuyaEncodingTests(unittest.TestCase):
    def test_unsigned_little_endian_base64_payload(self):
        encoded = protocol.encode_pulses((9000,4500,562,5000))
        self.assertEqual(encoded, "b64:KCOUETICiBM=")
        self.assertEqual(base64.b64decode(encoded[4:]), b"\x28\x23\x94\x11\x32\x02\x88\x13")

    def test_pulses_must_form_finite_positive_mark_space_pairs(self):
        for pulses in ((), (520,), (520,0), (520,-1), (520,65536), (520,1.5), (520,True)):
            with self.subTest(pulses=pulses), self.assertRaises(ValueError):
                protocol.encode_pulses(pulses)
