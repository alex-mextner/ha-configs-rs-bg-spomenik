"""One atomic, clock-driven authority for the universal remote's effective mode."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

LEASE_SECONDS = 60.0
INPUT_MIN_GAP = .150
INPUT_MAX_GAP = 1.000
CAPTURE_EPSILON = .000001  # UTC epoch float resolution; never a debounce window.
INTERNAL_KEYS = {"input_timeout", "projector_timeout", "power_state", "wake_done"}


@dataclass(frozen=True)
class PendingInput:
    pair: str
    native: bool
    at: float
    revisions: dict[str, str]


class RemoteMode:
    """Keep physical timestamps, power transitions and mode publication together.

    The scheduler takes seconds and returns a cancellable handle. No Home
    Assistant timer rounding or subsequent keypress is needed for expiration.
    Hardware writes are delegated only for the existing single-INPUT wake rule.
    """

    def __init__(self, *, clock: Callable, schedule: Callable,
                 changed: Callable, wake: Callable) -> None:
        self.clock, self.schedule, self.changed, self.wake = clock, schedule, changed, wake
        self.pair, self.native, self.audio = "tv", False, False
        self.expires_at = 0.0
        self.shutting_down = False
        self.displays = {"tv": None, "projector": None}
        self.revisions = {"tv": "", "projector": ""}
        self.intents = {"tv": None, "projector": None}
        self.targets = dict(self.intents)
        self._known = dict(self.displays)
        self.pending: PendingInput | None = None
        self._input_handle = self._lease_handle = None
        self._lease_generation = 0
        self._last_input_frame = float("-inf")
        self._published = None

    @property
    def target(self) -> str:
        if self.audio:
            return "speaker"
        return ("projector" if self.native else "kodi") if self.pair == "projector" else (
            "tv" if self.native else "chromecast"
        )

    def _publish(self) -> None:
        value = (self.pair, self.native, self.audio, self.expires_at, self.shutting_down)
        if value != self._published:
            self._published = value
            self.changed()

    def _automatic_pair(self) -> str:
        return "projector" if (not self.shutting_down and self.displays["projector"] is True
                               and self.displays["tv"] is False) else "tv"

    def _cancel_pending(self) -> None:
        self.pending = None
        if self._input_handle is not None:
            self._input_handle.cancel()
            self._input_handle = None

    def _automatic(self) -> None:
        self.pair, self.native, self.expires_at = self._automatic_pair(), False, 0.0
        self._arm_lease()

    def _arm_lease(self) -> None:
        self._lease_generation += 1
        generation = self._lease_generation
        if self._lease_handle is not None:
            self._lease_handle.cancel()
            self._lease_handle = None
        if not self.expires_at:
            return
        def expire():
            if generation != self._lease_generation:
                return
            # An early scheduler callback must rearm, never abandon the lease.
            if self.clock() + 1e-9 < self.expires_at:
                self._arm_lease()
                return
            self._automatic()
            self._publish()
        self._lease_handle = self.schedule(max(0, self.expires_at - self.clock()), expire)

    def expire(self, at=None) -> None:
        if self._lease_expired(at):
            self._automatic()
            self._publish()

    def _lease_expired(self, at):
        return bool(self.expires_at and (self.clock() if at is None else at) + 1e-9 >= self.expires_at)

    def observe(self, *, tv: bool | None, projector: bool | None,
                shutting_down: bool, audio: bool, revisions=None, intents=None, targets=None, at=None) -> None:
        values = {"tv": tv, "projector": projector}
        revisions = {**self.revisions, **(revisions or {})}
        intents = {**self.intents, **(intents or {})}
        turned_off = self._off_transitions(values, revisions, intents)
        self.displays, self.revisions, self.intents = values, revisions, intents
        shutdown_started = shutting_down and not self.shutting_down
        self.shutting_down, self.audio = bool(shutting_down), bool(audio)
        self.targets = {**self.targets, **(targets or {})}
        if turned_off or shutdown_started:
            # Both sides are in the first INPUT snapshot: neither an old origin
            # nor an old destination may be restored after a newer OFF.
            self._cancel_pending()
        if (self.pair in turned_off or (self.shutting_down and self.pair == "projector")
                or self._lease_expired(at)):
            self._automatic()
        elif not self.expires_at:
            self.pair, self.native = self._automatic_pair(), False
        self._publish()

    def _off_transitions(self, values, revisions, intents):
        turned_off = set()
        for pair, value in values.items():
            if value is not None:
                if self._known[pair] is True and value is False:
                    turned_off.add(pair)
                self._known[pair] = value
            if revisions[pair] != self.revisions[pair] and intents[pair] is False:
                turned_off.add(pair)
        return turned_off

    def select(self, pair: str, native: bool = False, *, at=None, explicit=True) -> dict:
        if pair not in {"tv", "projector"}:
            raise ValueError("Unknown remote pair")
        self._cancel_pending()
        if pair == "projector" and self.shutting_down:
            self._automatic()
        else:
            self.pair, self.native = pair, bool(native)
            self.expires_at = ((self.clock() if at is None else at) + LEASE_SECONDS) if explicit else 0.0
            self._arm_lease()
        self._publish()
        return self.resolve()

    def resolve(self, pair=None, native=None) -> dict:
        """Recheck safety at a queued command's final dispatch boundary."""
        self.expire()
        pair = self.pair if pair is None else pair
        native = self.native if native is None else bool(native)
        if pair == "projector" and self.shutting_down:
            if self.pair == "projector":
                self._automatic()
                self._publish()
            return {"pair": "tv", "native": False}
        return {"pair": pair, "native": native}

    def _valid(self, pending: PendingInput) -> bool:
        return all(self.revisions[key] == revision for key, revision in pending.revisions.items())

    def _single(self) -> None:
        pending = self.pending
        self._cancel_pending()
        if pending is None or not self._valid(pending):
            return
        destination = "tv" if pending.pair == "projector" else "projector"
        other = pending.pair
        wake = (self.displays[destination] is False and self.displays[other] is True
                and self.targets[destination] is not True
                and not (destination == "projector" and self.shutting_down))
        self.audio = False
        self.select(destination, at=pending.at)
        if wake:
            self.wake(destination, pending.revisions[destination])

    def press(self, button: str, at: float | None = None) -> dict:
        at = self.clock() if at is None else at
        self.expire(at)
        if button in INTERNAL_KEYS or button.startswith("samsung_ir_"):
            if button == "input_timeout" and self.pending and self.clock() >= self.pending.at + INPUT_MAX_GAP:
                self._single()
            return self.resolve()
        if button == "input":
            return self._input_press(at)
        if self.pending:
            self._single()
        if self.expires_at:
            self.expires_at = max(self.expires_at, at + LEASE_SECONDS)
            self._arm_lease()
            self._publish()
        return self.resolve()

    def _input_press(self, at):
        since_frame = at - self._last_input_frame
        self._last_input_frame = max(at, self._last_input_frame)
        if since_frame < INPUT_MIN_GAP - CAPTURE_EPSILON:
            return self.resolve()
        pending = self.pending
        if pending and INPUT_MIN_GAP - CAPTURE_EPSILON <= at - pending.at <= INPUT_MAX_GAP + CAPTURE_EPSILON:
            self._cancel_pending()
            if self._valid(pending):
                self.audio = False
                self.select(pending.pair, not pending.native, at=at)
            return self.resolve()
        if pending:
            self._single()
        self.pending = pending = PendingInput(self.pair, self.native, at, dict(self.revisions))
        def finish():
            if self.pending is pending:
                self._single()
        self._input_handle = self.schedule(max(0, at + INPUT_MAX_GAP + .010 - self.clock()), finish)
        if self.expires_at:
            self.expires_at = at + LEASE_SECONDS
            self._arm_lease()
            self._publish()
        return self.resolve()

    def close(self) -> None:
        self._cancel_pending()
        self.expires_at = 0
        self._arm_lease()
