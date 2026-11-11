"""Serialize whole IR bursts at one async send boundary."""
import asyncio
from collections.abc import Awaitable, Callable, Sequence
import math

from .protocol import encode_pulses


class Transport:
    """One lock for every appliance using this remote."""

    def __init__(self, send: Callable[[str], Awaitable[None]]) -> None:
        self._send = send
        self._lock = asyncio.Lock()

    async def async_send(self, pulses: Sequence[int], *, repeats: int = 1, gap: float = 0) -> None:
        """Send full single frames, releasing the lock even on failure/cancellation."""
        if type(repeats) is not int or repeats < 1:
            raise ValueError("Repeat count must be a positive integer")
        if isinstance(gap, bool) or not isinstance(gap, (int, float)) or not math.isfinite(gap) or gap < 0:
            raise ValueError("Repeat delay must be finite and nonnegative")
        command = encode_pulses(pulses)
        async with self._lock:
            for index in range(repeats):
                if index:
                    await asyncio.sleep(gap)
                await self._send(command)
