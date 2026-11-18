# Copyright (c) 2026 Alex Ultra
# SPDX-License-Identifier: MIT
"""Pure independent-reference builders shared by bounded and native CI tests."""

DAY = 86400


def raw_burst(raw: float = 209) -> list[float]:
    """Return a fresh eleven-reading burst around the requested physical value."""
    return [raw - 1] * 5 + [raw] + [raw + 1] * 5


def physical_attributes(sampled_at: float, raw: float = 209) -> dict:
    """Build the physical sample fields without depending on Home Assistant."""
    return {
        "sampled_at": sampled_at,
        "samples": raw_burst(raw),
        "physical_pin": 22,
    }


def qualified_cycle_windows(now: float) -> list[tuple[int, float, float]]:
    """Return three training windows and one later holdout in explicit order."""
    starts = [now - 15 * DAY - 3600 + day * DAY for day in (0, 7, 14, 15)]
    return [(cycle, start, start + 3600) for cycle, start in enumerate(starts)]


def reference_attributes(
    cycle: int,
    start_at: float,
    end_at: float,
    fingerprint: str,
    *,
    protocol: str = "gravimetric-v1",
) -> dict:
    """Keep independently declared reference identity separate from its state."""
    return {
        "reference_id": f"wet-{cycle}",
        "cycle_id": f"cycle-{cycle}",
        "start_at": start_at,
        "end_at": end_at,
        "protocol": protocol,
        "source_fingerprint": fingerprint,
    }
