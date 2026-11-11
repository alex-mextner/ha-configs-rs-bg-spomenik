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
        self._busy_until = 0.0

    async def async_send(self, pulses: Sequence[int], *, repeats: int = 1, gap: float = 0) -> None:
        """Send full single frames, releasing the lock even on failure/cancellation."""
        if type(repeats) is not int or repeats < 1:
            raise ValueError("Repeat count must be a positive integer")
        if isinstance(gap, bool) or not isinstance(gap, (int, float)) or not math.isfinite(gap) or gap < 0:
            raise ValueError("Repeat delay must be finite and nonnegative")
        command = encode_pulses(pulses)
        async with self._lock:
            loop = asyncio.get_running_loop()
            remaining = self._busy_until - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._busy_until = 0.0
            for index in range(repeats):
                if index:
                    await asyncio.sleep(gap)
                try:
                    await self._send(command)
                finally:
                    # Preserve this deadline if a service or waiting task is cancelled.
                    self._busy_until = loop.time() + sum(pulses) / 1_000_000
                # Tuya Local queues packets; service return is not waveform completion.
                # Keep the shared lock through the final frame as well.
                await asyncio.sleep(sum(pulses) / 1_000_000)
                self._busy_until = 0.0
