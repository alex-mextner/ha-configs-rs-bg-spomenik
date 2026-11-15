"""Bound service acknowledgements while retaining ownership of cancelled calls."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

CANCELLATION_GRACE = 0.02
SERVICE_ACK_LIMIT = 0.4


class ServiceTimeoutError(TimeoutError):
    """An acknowledgement exceeded the controller's remaining time budget."""

    def __init__(self) -> None:
        """Use a fixed reason that contains no service data or credentials."""
        super().__init__("service_acknowledgement_timeout")


class ServiceCalls:
    """Track provider calls, including providers that suppress cancellation."""

    def __init__(self, hass: HomeAssistant, limit: float = SERVICE_ACK_LIMIT) -> None:
        """Retain unfinished tasks until the actual provider call settles."""
        self.hass = hass
        self.limit = limit
        self.pending: set[asyncio.Task[bool]] = set()

    def _finished(self, task: asyncio.Task[bool]) -> None:
        self.pending.discard(task)
        if not task.cancelled():
            # The awaiting caller handles expected errors. For an orphaned call,
            # retrieve the result without logging private service data.
            task.exception()

    async def call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        *,
        deadline: float,
        guard: Callable[[], bool] | None = None,
    ) -> bool:
        """Cancel a late call and return without waiting indefinitely for it."""
        remaining = min(self.limit, deadline - time.monotonic())
        if remaining <= 0:
            raise ServiceTimeoutError

        async def invoke() -> bool:
            if guard is not None and not guard():
                return False
            await self.hass.services.async_call(domain, service, data, blocking=True)
            return True

        task = asyncio.create_task(
            invoke(),
            name=f"remote-announcement:{domain}.{service}",
        )
        self.pending.add(task)
        task.add_done_callback(self._finished)
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if task in done:
                return task.result()
            task.cancel()
            await asyncio.wait(
                {task},
                timeout=max(0.0, min(CANCELLATION_GRACE, deadline - time.monotonic())),
            )
            raise ServiceTimeoutError
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def close(self, grace_seconds: float) -> None:
        """Request cancellation once and retain tracking beyond bounded shutdown."""
        pending = set(self.pending)
        if not pending:
            return
        for task in pending:
            task.cancel()
        await asyncio.wait(pending, timeout=grace_seconds)
