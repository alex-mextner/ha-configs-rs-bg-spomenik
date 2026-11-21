"""One atomic, clock-driven authority for the universal remote's effective mode."""

from __future__ import annotations

from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING:
    from asyncio import TimerHandle
    from collections.abc import Callable

from dataclasses import dataclass

LEASE_SECONDS = 60.0
INPUT_MIN_GAP = 0.150
INPUT_MAX_GAP = 1.000
CAPTURE_EPSILON = 0.000001  # UTC epoch float resolution; never a debounce window.
INTERNAL_KEYS = {"input_timeout", "projector_timeout", "power_state", "wake_done"}
DISPLAY_ONLY_KEYS = {
    "menu",
    "home",
    "guide",
    "dash",
    "netflix",
    "youtube",
    "disney",
    "disney_hold",
}


class RemoteRoute(TypedDict):
    """One committed pair and native-target choice."""

    pair: str
    native: bool
    blocked: NotRequired[str]
    power_request_id: NotRequired[str]
    speaker: NotRequired[bool]
    audio_action: NotRequired[str]


@dataclass(frozen=True)
class PowerObservations:
    """Snapshot both displays and their latest accepted power intentions."""

    tv: bool | None
    projector: bool | None
    shutting_down: bool
    revisions: dict[str, str] | None = None
    intents: dict[str, bool | None] | None = None
    targets: dict[str, bool | None] | None = None


@dataclass(frozen=True)
class PendingInput:
    """Capture the first physical INPUT route and both power-intent revisions."""

    pair: str
    native: bool
    at: float
    revisions: dict[str, str]


class RemoteMode:
    """
    Keep physical timestamps, power transitions and mode publication together.

    The scheduler takes seconds and returns a cancellable handle. No Home
    Assistant timer rounding or subsequent keypress is needed for expiration.
    Hardware writes are delegated only for the existing single-INPUT wake rule.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        schedule: Callable[[float, Callable[[], None]], TimerHandle],
        changed: Callable[[], None],
        wake: Callable[[str, str], None],
    ) -> None:
        """Bind timing, publication and wake callbacks to automatic routing."""
        self.clock, self.schedule, self.changed, self.wake = (
            clock,
            schedule,
            changed,
            wake,
        )
        self.pair, self.native, self.audio = "tv", False, False
        self.audio_expires_at = 0.0
        self._audio_at = float("-inf")
        self._audio_handle = None
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
        """Return the effective player, display or temporary audio destination."""
        if self.audio:
            return "speaker"
        return (
            ("projector" if self.native else "kodi")
            if self.pair == "projector"
            else ("tv" if self.native else "chromecast")
        )

    def _publish(self) -> None:
        value = (
            self.pair,
            self.native,
            self.audio,
            self.expires_at,
            self.shutting_down,
        )
        if value != self._published:
            self._published = value
            self.changed()

    def _automatic_pair(self) -> str:
        return (
            "projector"
            if (
                not self.shutting_down
                and self.displays["projector"] is True
                and self.displays["tv"] is False
            )
            else "tv"
        )

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

        def expire() -> None:
            if generation != self._lease_generation:
                return
            # An early scheduler callback must rearm, never abandon the lease.
            if self.clock() + 1e-9 < self.expires_at:
                self._arm_lease()
                return
            self._automatic()
            self._publish()

        self._lease_handle = self.schedule(
            max(0, self.expires_at - self.clock()), expire
        )

    def expire(self, at: float | None = None) -> None:
        """Revert an expired lease using physical capture time when provided."""
        if self._lease_expired(at):
            self._automatic()
            self._publish()

    def _lease_expired(self, at: float | None) -> bool:
        return bool(
            self.expires_at
            and (self.clock() if at is None else at) + 1e-9 >= self.expires_at
        )

    def set_audio(self, *, active: bool, at: float) -> None:
        """Commit speaker focus and its physical inactivity deadline together."""
        self._set_audio(active=active, at=at)
        self._publish()

    def _set_audio(self, *, active: bool, at: float) -> None:
        if self._audio_handle is not None:
            self._audio_handle.cancel()
            self._audio_handle = None
        self.audio = active
        self._audio_at = max(self._audio_at, at)
        self.audio_expires_at = at + LEASE_SECONDS if active else 0.0
        if active:
            self._arm_audio()

    def _arm_audio(self) -> None:
        deadline = self.audio_expires_at

        def expire() -> None:
            if self.audio_expires_at != deadline:
                return
            if self.clock() < deadline:
                self._arm_audio()
            else:
                self.set_audio(active=False, at=self.clock())

        self._audio_handle = self.schedule(max(0, deadline - self.clock()), expire)

    def observe(
        self,
        power: PowerObservations,
        *,
        audio: bool,
        at: float | None = None,
    ) -> None:
        """Reconcile fresh display state and accepted power intent with current mode."""
        values = {"tv": power.tv, "projector": power.projector}
        revisions = {**self.revisions, **(power.revisions or {})}
        intents = {**self.intents, **(power.intents or {})}
        turned_off = self._off_transitions(values, revisions, intents)
        self.displays, self.revisions, self.intents = values, revisions, intents
        shutdown_started = power.shutting_down and not self.shutting_down
        self.shutting_down = bool(power.shutting_down)
        observed_at = self.clock() if at is None else at
        if audio != self.audio:
            self._set_audio(active=audio, at=observed_at)
        elif self.audio and observed_at >= self.audio_expires_at:
            self._set_audio(active=False, at=observed_at)
        self.targets = {**self.targets, **(power.targets or {})}
        if turned_off or shutdown_started:
            # Both sides are in the first INPUT snapshot: neither an old origin
            # nor an old destination may be restored after a newer OFF.
            self._cancel_pending()
        if (
            self.pair in turned_off
            or (self.shutting_down and self.pair == "projector")
            or self._lease_expired(at)
        ):
            self._automatic()
        elif not self.expires_at:
            self.pair, self.native = self._automatic_pair(), False
        self._publish()

    def _off_transitions(
        self,
        values: dict[str, bool | None],
        revisions: dict[str, str],
        intents: dict[str, bool | None],
    ) -> set[str]:
        turned_off = set()
        for pair, value in values.items():
            if value is not None:
                if self._known[pair] is True and value is False:
                    turned_off.add(pair)
                self._known[pair] = value
            if revisions[pair] != self.revisions[pair] and intents[pair] is False:
                turned_off.add(pair)
        return turned_off

    def select(
        self,
        pair: str,
        *,
        native: bool = False,
        at: float | None = None,
        explicit: bool = True,
    ) -> RemoteRoute:
        """Commit one explicit group and native-target selection."""
        if pair not in {"tv", "projector"}:
            error_message = "Unknown remote pair"
            raise ValueError(error_message)
        now = self.clock()
        # YAML milliseconds can round-trip one epoch-clock ULP earlier. Use
        # the existing capture precision only for causal ordering.
        if (
            explicit
            and at is not None
            and (
                at + CAPTURE_EPSILON < self._audio_at
                or at + LEASE_SECONDS <= now
                or at + LEASE_SECONDS + CAPTURE_EPSILON < self.expires_at
                or (self.pending is not None and at + CAPTURE_EPSILON < self.pending.at)
            )
        ):
            # A delayed physical source script cannot replace a newer choice,
            # cancel its pending INPUT, or extend an expired source lease.
            return self.resolve()
        at = now if at is None else at
        if explicit:
            self._set_audio(active=False, at=at)
        self._cancel_pending()
        if pair == "projector" and self.shutting_down:
            self._automatic()
        else:
            self.pair, self.native = pair, bool(native)
            self.expires_at = at + LEASE_SECONDS if explicit else 0.0
            self._arm_lease()
        self._publish()
        return self.resolve()

    def resolve(
        self, pair: str | None = None, *, native: bool | None = None
    ) -> RemoteRoute:
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
        return all(
            self.revisions[key] == revision
            for key, revision in pending.revisions.items()
        )

    def _single(self) -> None:
        pending = self.pending
        self._cancel_pending()
        if pending is None or not self._valid(pending):
            return
        destination = "tv" if pending.pair == "projector" else "projector"
        wake = (
            destination == "projector"
            and self.displays["projector"] is False
            and self.displays["tv"] is True
            and self.targets["projector"] is not True
            and not self.shutting_down
        )
        self.select(destination, at=pending.at)
        if wake:
            self.wake(destination, pending.revisions[destination])

    def press(self, button: str, at: float | None = None) -> RemoteRoute:
        """Apply one accepted logical press at its trusted physical capture time."""
        at = self.clock() if at is None else at
        self.expire(at)
        if button in INTERNAL_KEYS or button.startswith("samsung_ir_"):
            if (
                button == "input_timeout"
                and self.pending
                and self.clock() >= self.pending.at + INPUT_MAX_GAP
            ):
                self._single()
            return self.resolve()
        if button == "input":
            return self._input_press(at)
        if self.pending:
            self._single()
        if self.audio and button in DISPLAY_ONLY_KEYS:
            # The display owns this press, including guards at physical reception.
            self.set_audio(active=False, at=at)
        if self.expires_at:
            self.expires_at = max(self.expires_at, at + LEASE_SECONDS)
            self._arm_lease()
            self._publish()
        return self.resolve()

    def _input_press(self, at: float) -> RemoteRoute:
        since_frame = at - self._last_input_frame
        self._last_input_frame = max(at, self._last_input_frame)
        if since_frame < INPUT_MIN_GAP - CAPTURE_EPSILON:
            return self.resolve()
        pending = self.pending
        if (
            pending
            and INPUT_MIN_GAP - CAPTURE_EPSILON
            <= at - pending.at
            <= INPUT_MAX_GAP + CAPTURE_EPSILON
        ):
            self._cancel_pending()
            if self._valid(pending):
                self.select(pending.pair, native=not pending.native, at=at)
            return self.resolve()
        if pending:
            self._single()
        self.pending = pending = PendingInput(
            self.pair, self.native, at, dict(self.revisions)
        )

        def finish() -> None:
            if self.pending is pending:
                self._single()

        self._input_handle = self.schedule(
            max(0, at + INPUT_MAX_GAP + 0.010 - self.clock()), finish
        )
        if self.expires_at:
            self.expires_at = at + LEASE_SECONDS
            self._arm_lease()
            self._publish()
        return self.resolve()

    def close(self) -> None:
        """Cancel gestures and deadlines without dispatching hardware actions."""
        self._cancel_pending()
        if self._audio_handle is not None:
            self._audio_handle.cancel()
            self._audio_handle = None
        self.audio_expires_at = 0.0
        self.expires_at = 0
        self._arm_lease()
