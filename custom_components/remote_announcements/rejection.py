"""Quiet native feedback for rejected logical remote commands."""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.core import Event, EventStateChangedData

    from . import Manager, SpeechPlayback

MAX_PENDING = 8
MAX_AGE = 3.0
IDENTITY_LIFETIME = 30.0
MAX_IDENTITIES = 256
QUIET_VOLUME = 0.1
VOLUME_TOLERANCE = 0.025
CUE_BUDGET = 3.0
# The official Yandex preview is 1.032 seconds. AISIS's native IoT sound has
# no completion event; this is a bounded settle wait, not a measured duration.
CUE_SETTLE_SECONDS = 1.25
YANDEX_TAG = '<speaker audio="alice-sounds-game-ping-1.opus">'


class CueNotReadyError(Exception):
    """A fixed nonprivate reason why quiet native feedback was not delivered."""


def quiet_volume(current: object) -> float:
    """Lower a valid nonzero level to at most one tenth; never unmute or raise."""
    if (
        type(current) not in (int, float)
        or not math.isfinite(current)
        or not 0 < current <= 1
    ):
        reason = "speaker_volume_unknown_or_zero"
        raise CueNotReadyError(reason)
    return min(QUIET_VOLUME, current)


def native_command() -> dict[str, Any]:
    """Play one fixed vendor sound on Yandex's native speech channel."""
    return {
        "command": "serverAction",
        "serverActionEventPayload": {
            "type": "server_action",
            "name": "update_form",
            "payload": {
                "form_update": {
                    "name": "personal_assistant.scenarios.quasar.iot.repeat_phrase",
                    "slots": [
                        {
                            "type": "string",
                            "name": "phrase_to_repeat",
                            "value": YANDEX_TAG,
                        }
                    ],
                },
                "resubmit": True,
            },
        },
    }


@dataclass(frozen=True)
class RejectedPress:
    """Retain logical identity and freshness independently of native playback."""

    reason: str
    press_id: str
    received_at: float


class RejectionQueue:
    """Serialize distinct rejections, deduplicate guards, and report bounded drops."""

    def __init__(
        self,
        play: Callable[[RejectedPress], Awaitable[str]],
        *,
        report: Callable[[str, dict[str, Any]], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Own a bounded FIFO and retain active provider/recovery task identity."""
        self.play, self.report, self.clock = play, report, clock
        self.pending: deque[RejectedPress] = deque()
        self.identities: dict[str, float] = {}
        self.task: asyncio.Task | None = None
        self.closed = False
        self.sequence = 0
        self.counts = {"accepted": 0, "sent": 0, "dropped": 0, "duplicates": 0}

    def publish(self, status: str, **values: Any) -> None:
        """Expose outcomes without treating a service acknowledgement as hearing."""
        self.report(status, {**self.counts, **values})

    def submit(self, reason: str, press_id: str | None = None) -> dict[str, str]:
        """Accept once per logical press without canceling an active cue."""
        now = self.clock()
        if self.closed:
            return {"status": "dropped", "reason": "closed"}
        self.sequence += 1
        identity = press_id or f"call:{self.sequence}"
        self.identities = {
            key: stamp
            for key, stamp in self.identities.items()
            if now - stamp < IDENTITY_LIFETIME
        }
        if identity in self.identities:
            self.counts["duplicates"] += 1
            self.publish("duplicate", reason=reason)
            return {"status": "duplicate"}
        self.identities[identity] = now
        if len(self.identities) > MAX_IDENTITIES:
            del self.identities[next(iter(self.identities))]
        if len(self.pending) >= MAX_PENDING:
            self.counts["dropped"] += 1
            self.publish("dropped", reason=reason, drop_reason="queue_full")
            return {"status": "dropped", "reason": "queue_full"}
        self.pending.append(RejectedPress(reason, identity, now))
        self.counts["accepted"] += 1
        self.publish("queued", reason=reason)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(
                self._run(), name="remote-rejection-feedback"
            )
        return {"status": "queued"}

    async def _run(self) -> None:
        while self.pending and not self.closed:
            item = self.pending.popleft()
            if self.clock() - item.received_at > MAX_AGE:
                self.counts["dropped"] += 1
                self.publish("dropped", reason=item.reason, drop_reason="expired")
                continue
            try:
                status = await self.play(item)
            except Exception as error:  # noqa: BLE001 - Keep following presses after native failures.
                self.counts["dropped"] += 1
                reason = (
                    str(error)
                    if isinstance(error, CueNotReadyError)
                    else type(error).__name__
                )
                self.publish("not_delivered", reason=item.reason, drop_reason=reason)
            else:
                self.counts["sent"] += 1
                self.publish(status, reason=item.reason)

    async def drain(self) -> None:
        """Join the owned worker after pending work or shutdown has settled."""
        if self.task is not None:
            await self.task

    async def stop(self, grace_seconds: float) -> None:
        """Bound teardown while retaining the active task until recovery settles."""
        self.closed = True
        if self.pending:
            self.counts["dropped"] += len(self.pending)
            self.pending.clear()
            self.publish("dropped", drop_reason="closed")
        if self.task is None:
            return
        done, _ = await asyncio.wait({self.task}, timeout=grace_seconds)
        if self.task not in done:
            self.task.cancel()
            done, _ = await asyncio.wait({self.task}, timeout=0.05)
        if self.task in done:
            try:
                await self.drain()
            except asyncio.CancelledError:
                return


def _quiet_unchanged(manager: Manager, playback: SpeechPlayback) -> bool:
    state = manager.hass.states.get(playback.entity)
    if state is None or playback.user_changed_volume:
        return False
    volume = state.attributes.get("volume_level")
    return (
        state.state not in ("unknown", "unavailable")
        and not state.attributes.get("restored")
        and not state.attributes.get("is_volume_muted", False)
        and type(volume) in (int, float)
        and math.isfinite(volume)
        and 0 < volume <= QUIET_VOLUME + 0.000001
        and abs(volume - playback.applied) < VOLUME_TOLERANCE
    )


def _may_lower(manager: Manager, playback: SpeechPlayback) -> bool:
    state = manager.hass.states.get(playback.entity)
    return bool(
        not manager.closing
        and state is not None
        and state.state not in ("unknown", "unavailable")
        and not state.attributes.get("restored")
        and not state.attributes.get("is_volume_muted", False)
        and state.attributes.get("volume_level") == playback.old_volume
    )


async def _confirm_quiet(manager: Manager, playback: SpeechPlayback) -> None:
    """Observe the lower volume before native feedback may be dispatched."""
    # Keep the bounded queue importable without Home Assistant.
    from homeassistant.core import callback  # noqa: PLC0415
    from homeassistant.helpers.event import (  # noqa: PLC0415
        async_track_state_change_event,
    )

    quiet = asyncio.Event()

    @callback
    def observed(_event: Event[EventStateChangedData] | None = None) -> None:
        if _quiet_unchanged(manager, playback):
            quiet.set()

    unsubscribe = async_track_state_change_event(
        manager.hass, [playback.entity], observed
    )
    try:
        observed()
        if not quiet.is_set():
            await asyncio.wait_for(
                quiet.wait(),
                min(0.4, max(0.0, playback.work_deadline - time.monotonic())),
            )
    except TimeoutError as error:
        reason = "quiet_volume_unconfirmed"
        raise CueNotReadyError(reason) from error
    finally:
        unsubscribe()


def _playback(
    manager: Manager, playback_type: type[SpeechPlayback]
) -> tuple[str, SpeechPlayback]:
    # This runtime-only import leaves queue tests independent of HA installation.
    from homeassistant.core import SupportsResponse  # noqa: PLC0415

    if manager.services.pending:
        reason = "speaker_service_recovery_pending"
        raise CueNotReadyError(reason)
    backend, entity = manager.speaker()
    before = manager.hass.states.get(entity)
    if (
        before is None
        or before.state in ("unknown", "unavailable")
        or before.attributes.get("restored")
        or before.attributes.get("is_volume_muted", False)
    ):
        reason = "speaker_muted_or_unavailable"
        raise CueNotReadyError(reason)
    applied = quiet_volume(before.attributes.get("volume_level"))
    manager.check_aisis_volume_recovery(before)
    if backend == "yandex":
        if before.attributes.get("alice_state") != "IDLE":
            reason = "speaker_busy_or_local_connection_unavailable"
            raise CueNotReadyError(reason)
        if (
            not manager.hass.services.has_service("yandex_station", "send_command")
            or manager.hass.services.supports_response("yandex_station", "send_command")
            is SupportsResponse.NONE
        ):
            reason = "yandex_native_response_service_unavailable"
            raise CueNotReadyError(reason)
    elif not manager.hass.services.has_service("aisis_speaker", "send_directive"):
        reason = "aisis_native_sound_service_unavailable"
        raise CueNotReadyError(reason)
    return backend, playback_type(
        before,
        applied,
        pause=False,
        verify_volume=backend == "aisis",
        budget=CUE_BUDGET,
    )


async def _settle(backend: str, playback: SpeechPlayback) -> str:
    wait = playback.completion_wait(CUE_SETTLE_SECONDS)
    if backend == "yandex":
        try:
            await asyncio.wait_for(playback.ended.wait(), wait)
        except TimeoutError:
            pass
        else:
            return "completed"
    else:
        await asyncio.sleep(wait)
    return "sent_unconfirmed"


async def _send_native(manager: Manager, backend: str, playback: SpeechPlayback) -> str:
    from .providers import validate_native_response  # noqa: PLC0415 - Runtime boundary.

    # A late acknowledgement must never consume the quiet interval and make
    # restoration raise the remaining sound. Preserve this reserve even when
    # an acknowledgement fails after the speaker may already have accepted it.
    deadline = playback.work_deadline - CUE_SETTLE_SECONDS
    if deadline <= time.monotonic():
        reason = "cue_budget_exhausted"
        raise CueNotReadyError(reason)

    def guard() -> bool:
        return not manager.closing and _quiet_unchanged(manager, playback)

    attempted = True
    try:
        if backend == "yandex":
            sent = await manager.services.call(
                ("yandex_station", "send_command"),
                {"entity_id": playback.entity, **native_command()},
                deadline=deadline,
                guard=guard,
                response_validator=validate_native_response,
            )
        else:
            sent = await manager.services.call(
                ("aisis_speaker", "send_directive"),
                {
                    "duid": playback.entity.removeprefix(
                        "media_player.aisis_speaker_"
                    ).upper(),
                    "name": "smart_speaker_play_iot_sound",
                    "arguments": {"isPowerOn": False},
                },
                deadline=deadline,
                guard=guard,
            )
        if not sent:
            attempted = False
            reason = "quiet_volume_or_lifecycle_changed"
            raise CueNotReadyError(reason)
    finally:
        status = await _settle(backend, playback) if attempted else "not_delivered"
    return status


async def play_rejection(
    manager: Manager, item: RejectedPress, playback_type: type[SpeechPlayback]
) -> str:
    """Use native speaker feedback with bounded lower/readback/guarded restore."""
    from homeassistant.helpers.event import (  # noqa: PLC0415
        async_track_state_change_event,
    )

    remaining = MAX_AGE - (time.monotonic() - item.received_at)
    if remaining <= 0:
        reason = "expired"
        raise CueNotReadyError(reason)
    try:
        await asyncio.wait_for(manager.playback_lock.acquire(), remaining)
    except TimeoutError as error:
        reason = "expired"
        raise CueNotReadyError(reason) from error
    try:
        if manager.closing or time.monotonic() - item.received_at > MAX_AGE:
            reason = "expired"
            raise CueNotReadyError(reason)
        backend, playback = _playback(manager, playback_type)
        unsubscribe = async_track_state_change_event(
            manager.hass, [playback.entity], playback.observe
        )
        try:
            if playback.applied < playback.old_volume:
                # The shared recovery helper owns this temporary decrease.
                playback.raised = True
                changed = await manager.call(
                    "volume_set",
                    playback.entity,
                    volume_level=playback.applied,
                    deadline=playback.work_deadline,
                    guard=lambda: _may_lower(manager, playback),
                )
                if not changed:
                    playback.raised = False
                    reason = "speaker_changed_before_quiet"
                    raise CueNotReadyError(reason)
            await _confirm_quiet(manager, playback)
            status = await _send_native(manager, backend, playback)
        finally:
            unsubscribe()
            await manager.restore_playback(playback)
        return "recovery_unconfirmed" if playback.recovery_failed else status
    finally:
        manager.playback_lock.release()
