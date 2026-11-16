"""Persistable projector shutdown intent, released by a new HDMI OFF report."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

import math


class ProjectorShutdown:
    """A failed/expired OFF request remains unsafe until hardware confirms OFF."""

    def __init__(self, *, changed: Callable[[], None]) -> None:
        """Initialize the persistent shutdown latch and its change callback."""
        self.changed = changed
        self.active = False
        self.requested_at = 0.0
        self._report_after = 0.0

    def restore(self, data: object, now: float) -> None:
        """Restore a persisted latch without trusting an old HDMI off report."""
        if not isinstance(data, dict) or data.get("active") is not True:
            return
        stamp = data.get("requested_at", 0)
        self.requested_at = (
            float(stamp)
            if type(stamp) in (int, float) and math.isfinite(stamp)
            else now
        )
        self._report_after = max(now, self.requested_at)
        self.active = True
        self.changed()

    def serialize(self) -> dict:
        """Return the minimal persistent shutdown state."""
        return {"active": self.active, "requested_at": self.requested_at}

    def request_off(self, *, observed: bool | None, now: float) -> None:
        """Latch an explicit off request only when physical power is known on."""
        if observed is not True or self.active:
            return
        self.active = True
        self.requested_at = self._report_after = now
        self.changed()

    def observe(
        self,
        *,
        value: bool | None,
        reported_at: float,
        fresh: bool,
        restored: bool = False,
    ) -> None:
        """Release the latch only after a fresh, later physical HDMI off report."""
        if (
            self.active
            and value is False
            and fresh
            and not restored
            and reported_at > self._report_after
        ):
            self.active = False
            self.changed()
