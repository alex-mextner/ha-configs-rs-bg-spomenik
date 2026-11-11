"""Independent encoders for the user's verified Haier and projector wire formats."""
import base64
from collections.abc import Sequence
import math
import struct
from types import MappingProxyType

MODES = MappingProxyType({"auto":0, "cool":1, "dry":2, "heat":4, "fan_only":6})
FANS = MappingProxyType({"auto":5, "low":3, "medium":2, "high":1})
SWINGS = ("off", "vertical")
PRESETS = ("none", "sleep", "boost")
PROJECTOR_COMMANDS = MappingProxyType({
    "power":0x01, "play_pause":0x5F, "source":0x04, "up":0x0B,
    "down":0x0F, "left":0x49, "right":0x4A, "menu":0x0A,
    "back":0x54, "vol_down":0x10, "mute":0x6A, "vol_up":0x0C,
})
OFF_STATE = bytes.fromhex("A6 90 00 00 00 A0 00 80 00 00 00 00 05 5B")


def haier_state(mode: str, temperature: float, fan: str, swing: str, preset: str) -> bytes:
    """Encode a complete 112-bit YRW02 command, including the additive checksum."""
    if mode != "off" and mode not in MODES:
        raise ValueError(f"Unsupported HVAC mode: {mode}")
    if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
        or not math.isfinite(temperature) or not 16 <= temperature <= 30
        or temperature != int(temperature)):
        raise ValueError("Target temperature must be an integer from 16 to 30°C")
    if fan not in FANS:
        raise ValueError(f"Unsupported fan mode: {fan}")
    if swing not in SWINGS:
        raise ValueError(f"Unsupported swing mode: {swing}")
    if preset not in PRESETS:
        raise ValueError(f"Unsupported preset: {preset}")
    if mode == "off":
        return OFF_STATE
    state = bytearray(14)
    state[0] = 0xA6
    state[1] = ((int(temperature) - 16) << 4) | (0xC if swing == "vertical" else 0)
    state[3] = 0  # Health/Filter is unsupported; see upstream issue #1894.
    state[4] = 0x40
    state[5] = FANS[fan] << 5
    state[6] = 0x40 if preset == "boost" and mode in ("cool", "heat") else 0
    state[7] = MODES[mode] << 5
    state[8] = 0x80 if preset == "sleep" else 0
    # IRac::haierYrwo2 finishes a complete state with setPower(), then send().
    # Preserve the mode bits; do not inherit the old custom setter-order artifact.
    state[12] = 5
    state[13] = sum(state[:13]) & 255
    return bytes(state)


def haier_pulses(state: bytes) -> tuple[int, ...]:
    """Produce one frame; long interframe delays belong to the transport."""
    if len(state) != 14 or state[0] != 0xA6 or state[-1] != sum(state[:-1]) & 255:
        raise ValueError("Invalid Haier YRW02 state or checksum")
    pulses = [3000, 3000, 3000, 4300]
    for byte in state:
        for bit in range(7, -1, -1):
            pulses.extend((520, 1650 if byte & (1 << bit) else 650))
    return (*pulses, 520, 5000)


def projector_pulses(command: str) -> tuple[int, ...]:
    """Encode one extended-NEC frame using the saved address and key data."""
    if command not in PROJECTOR_COMMANDS:
        raise ValueError(f"Unknown projector command: {command}")
    key = PROJECTOR_COMMANDS[command]
    data = (0x00, 0xBD, key, key ^ 255)
    pulses = [9000, 4500]
    for byte in data:
        for bit in range(8):
            pulses.extend((562, 1687 if byte & (1 << bit) else 562))
    return (*pulses, 562, 5000)


def encode_pulses(pulses: Sequence[int]) -> str:
    """Use the TinyTuya raw base64 format without importing device software."""
    if not pulses or len(pulses) % 2 or any(
        type(value) is not int or not 0 < value <= 65535 for value in pulses
    ):
        raise ValueError("IR timings must be positive 16-bit mark/space pairs")
    packed = struct.pack(f"<{len(pulses)}H", *pulses)
    return "b64:" + base64.b64encode(packed).decode("ascii")


SAMSUNG_COMMANDS = MappingProxyType({"power": 0x02, "power_on": 0x99, "power_off": 0x98})
SAMSUNG_HEADER = 4480
SAMSUNG_MARK = 560
SAMSUNG_ONE_SPACE = 1680
SAMSUNG_FRAME_DURATION = 108080


def samsung_pulses(command: str) -> tuple[int, ...]:
    """Encode one Samsung32 frame with the physically verified EKF timings.

    Address 0x07 is repeated, then the command and its complement, all LSB
    first. Literal wire vectors are independently decoded in the tests.
    """
    if command not in SAMSUNG_COMMANDS:
        raise ValueError(f"Unknown Samsung command: {command}")
    key = SAMSUNG_COMMANDS[command]
    pulses = [SAMSUNG_HEADER, SAMSUNG_HEADER]
    for byte in (0x07, 0x07, key, key ^ 255):
        for bit in range(8):
            pulses.extend((SAMSUNG_MARK, SAMSUNG_ONE_SPACE if byte & (1 << bit) else SAMSUNG_MARK))
    pulses.append(SAMSUNG_MARK)
    return (*pulses, SAMSUNG_FRAME_DURATION - sum(pulses))
