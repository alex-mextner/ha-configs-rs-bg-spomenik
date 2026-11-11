"""Decode the existing NEC universal-remote profile and its hold/T9 gestures."""
from __future__ import annotations
import base64
import binascii
import struct

MAIN = {
    0xF20D: "power", 0xED12: "input", 0x9E61: "dash",
    0xBB44: "volume_up", 0xBC43: "volume_down", 0xF10E: "mute",
    0xB54A: "channel_up", 0xB44B: "channel_down",
    0xE916: "up", 0xE817: "down", 0xE619: "left", 0xE718: "right",
    0xEA15: "ok", 0xB748: "back", 0xA35C: "exit",
    0xDF20: "home", 0xEB14: "menu", 0xE41B: "smart", 0xC53A: "guide",
    0xF30C: "info", 0x9966: "audio", 0xA857: "prev", 0x7689: "play",
    0xA956: "next", 0x4DB2: "red", 0xCE31: "green", 0x12ED: "yellow",
    0xD22D: "netflix", 0x44BB: "prime", 0x956A: "disney", 0x55AA: "youtube",
}
SAMSUNG = {0x807F: "samsung_ir_source", 0xA659: "samsung_ir_left",
           0x46B9: "samsung_ir_right", 0x16E9: "samsung_ir_ok",
           0x1AE5: "samsung_ir_back", 0xB44B: "samsung_ir_exit",
           0xF20D: "samsung_ir_power", 0x40BF: "samsung_ir_power"}
DIGITS = (" ", "1", "abc", "def", "ghi", "jkl", "mno", "pqrs", "tuv", "wxyz")
HOLD = {"prime", "disney", "ok"}
REPEAT = {"volume_up", "volume_down", "channel_up", "channel_down",
          "up", "down", "left", "right", "back"}
EVENT_TYPES = sorted(set(MAIN.values()) | set(SAMSUNG.values()) |
                     {"apple_tv", *map(str, range(10)), *(x + "_hold" for x in HOLD)} |
                     {f"t9_{mode}_{digit}_{'space' if char == ' ' else char}"
                      for mode in ("add", "cycle") for digit, chars in enumerate(DIGITS)
                      for char in chars})


def pulses_from_code(code: str) -> list[int]:
    if not isinstance(code, str) or len(code) > 32768:
        return []
    try:
        raw = base64.b64decode(code.removeprefix("b64:"), validate=True)
        if len(raw) % 2 or not raw:
            return []
        return list(struct.unpack("<" + "H" * (len(raw) // 2), raw))
    except (ValueError, binascii.Error, struct.error):
        return []


def decode_frames(code: str) -> list[dict]:
    """Decode NEC LSB fields and Samsung MSB fields as ESPHome reports them."""
    pulses = pulses_from_code(code)
    frames = []
    offset = 0
    i = 0
    while i + 2 < len(pulses):
        mark, space = pulses[i:i + 2]
        if 7000 <= mark <= 11000 and 1600 <= space <= 2900 and 300 <= pulses[i + 2] <= 900:
            frames.append({"repeat": True, "offset": offset / 1_000_000})
            consumed = 3
        elif (7000 <= mark <= 11000 or 3300 <= mark <= 5700) and 3300 <= space <= 5700 and i + 66 < len(pulses):
            word = 0
            valid = True
            for bit in range(32):
                on, off = pulses[i + 2 + bit * 2:i + 4 + bit * 2]
                if not 300 <= on <= 900 or not (250 <= off <= 950 or 1100 <= off <= 2300):
                    valid = False
                    break
                word |= int(off > 1000) << bit
            command = word >> 16
            if valid and (command & 255) ^ (command >> 8) == 255:
                address = word & 65535
                if mark < 7000:
                    # Samsung's 4.48 ms leader sends the same bits but ESPHome
                    # reports each 16-bit field in wire (MSB-first) order.
                    address = int(f"{address:016b}"[::-1], 2)
                    command = int(f"{command:016b}"[::-1], 2)
                frames.append({"repeat": False, "address": address,
                               "command": command, "offset": offset / 1_000_000})
                consumed = 67
            else:
                consumed = 1
        else:
            consumed = 1
        offset += sum(pulses[i:i + consumed])
        i += consumed
    return frames


class RemoteMapper:
    """Preserve the original receiver's semantic events without retransmitting IR."""

    def __init__(self):
        self.last = None
        self.initial = self.last_at = 0.0
        self.repeats = 0
        self.held = False
        self.digit = None
        self.digit_at = 0.0
        self.cycle = 0

    def feed(self, frames: list[dict], received_at: float) -> list[str]:
        result = []
        # Each decoded frame's relative position preserves bundled NEC repeats.
        base = received_at - (frames[-1]["offset"] if frames else 0)
        for frame in frames:
            now = base + frame["offset"]
            if frame["repeat"]:
                if not self.last or now - self.last_at > 0.6:
                    continue
                self.last_at = now
                self.repeats += 1
                if self.last in HOLD:
                    if self.repeats >= 2 and not self.held:
                        self.held = True
                        result.append(self.last + "_hold")
                elif self.last in REPEAT and now - self.initial > 0.15:
                    result.append(self.last)
                continue
            address, command = frame["address"], frame["command"]
            name = MAIN.get(command) if address == 0xBF00 else (
                "apple_tv" if address == 0xBA00 and command == 0xDC23 else
                SAMSUNG.get(command) if address == 0xE0E0 else None
            )
            digit = command & 255
            if address == 0xBF00 and 0 <= digit <= 9 and command >> 8 == 255 - digit:
                chars = DIGITS[digit]
                cycle = self.digit == digit and now - self.digit_at < 1.5 and len(chars) > 1
                self.cycle = (self.cycle + 1) % len(chars) if cycle else 0
                char = chars[self.cycle]
                result.append(f"t9_{'cycle' if cycle else 'add'}_{digit}_{'space' if char == ' ' else char}")
                self.digit, self.digit_at, self.last = digit, now, None
            elif name:
                self.digit = None
                self.last, self.initial, self.last_at = name, now, now
                self.repeats, self.held = 0, False
                result.append(name)
        return result
