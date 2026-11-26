"""
Synchronize a display and its player using independent observed power.

Native integrations know nothing about infrared remotes. This controller only
combines their observations and explicit power services. Unknown observations
never mean off, and an old in-flight command cannot replace a newer intent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from uuid import uuid4

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PowerEndpoint:
    """Read actual power and declare whether a request needs native feedback."""

    read: Callable[[], bool | None]
    command: Callable[[bool], Awaitable[None]]
    allow_unknown_command: bool = False
    wait_for_feedback: bool = False


@dataclass(frozen=True)
class PowerPairHooks:
    """Bind lifecycle callbacks and opt-in command cancellation policy."""

    changed: Callable[[], None] | None = None
    requested: Callable[[bool], None] | None = None
    preempt_commands: bool = False
    create_task: (
        Callable[[Coroutine[Any, Any, None], str], asyncio.Task[None]] | None
    ) = None


class PowerPair:
    """Keep one desired state while both independent devices confirm it."""

    def __init__(
        self,
        name: str,
        display: PowerEndpoint,
        player: PowerEndpoint,
        *,
        timeout: float = 120.0,
        hooks: PowerPairHooks | None = None,
    ) -> None:
        """Bind observed endpoints, confirmation policy and lifecycle callbacks."""
        self.name = name
        self.endpoints = {"display": display, "player": player}
        self.timeout = timeout
        bindings = hooks or PowerPairHooks()
        self.preempt_commands = bindings.preempt_commands
        self.changed = bindings.changed or (lambda: None)
        self.requested = bindings.requested or (lambda _target: None)
        self._create_task = bindings.create_task or (
            lambda coro, name: asyncio.create_task(coro, name=name)
        )
        self.target: bool | None = None
        self.last_error: str | None = None
        self.last_intent: bool | None = None
        self._intent_epoch = uuid4().hex
        self._intent_sequence = 0
        self._last = dict.fromkeys(self.endpoints)
        # Retain at most ON and OFF feedback for each endpoint. A command may
        # return before its observed acknowledgement, even after a newer intent
        # has finished. The existing confirmation timeout bounds that window.
        self._expected: dict[tuple[str, bool], tuple[int, float]] = {}
        self._retry_sides: set[str] = set()
        # State-gated POWER services must see their prior pushed state before
        # another command; cancelling the service cannot recall its frame.
        self._pending_commands: dict[str, tuple[bool, float]] = {}
        self._started = False
        self._baseline_ready = False
        self._generation = 0
        self._deadline = 0.0
        self._future: asyncio.Future[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._command_batch: asyncio.Future | None = None
        self._wake = asyncio.Event()

    @property
    def intent_revision(self) -> str:
        """Identify accepted intent, independent of status updates and completion."""
        return f"{self._intent_epoch}:{self._intent_sequence}"

    def start(self) -> None:
        """Establish a baseline, then reconcile startup from the physical display."""
        self._started = True
        self._baseline_ready = False
        self._last = dict.fromkeys(self.endpoints)
        self.observe()

    def observe(self) -> None:
        """Process fresh feedback; retain the last known state across outages."""
        if not self._started:
            return
        values = {side: endpoint.read() for side, endpoint in self.endpoints.items()}
        if not self._baseline_ready:
            self._observe_baseline(values)
            self._wake.set()
            return

        transitions = [
            value
            for side, value in values.items()
            if self._new_transition(side, value=value)
        ]
        if transitions:
            # HA state events normally change one side at a time. If both changed
            # together and disagree, the actual display remains authoritative.
            desired = transitions[-1]
            if len(transitions) > 1 and values["display"] is not None:
                desired = values["display"]
            self._request(target=desired)
        self._wake.set()

    def _observe_baseline(self, values: dict[str, bool | None]) -> None:
        for side, value in values.items():
            if value is not None:
                self._last[side] = value
        if all(value is not None for value in values.values()):
            self._baseline_ready = True
            if self.target is None and values["display"] != values["player"]:
                self._request(target=values["display"])

    def _new_transition(self, side: str, *, value: bool | None) -> bool:
        previous = self._last[side]
        if value is None:
            return False
        pending = self._pending_commands.get(side)
        confirmed = pending is not None and value is pending[0]
        if confirmed:
            del self._pending_commands[side]
        self._last[side] = value
        if (previous is None or value == previous) and not confirmed:
            return False
        expected = self._expected.pop((side, value), None)
        # An older requested ON can arrive after a newer OFF was requested.
        # It is confirmation of that old command, not a new human request.
        if expected and expected[1] >= asyncio.get_running_loop().time():
            if value is self.last_intent:
                # A current acknowledgement satisfies any retry queued by an
                # older opposite acknowledgement while this command was pending.
                self._retry_sides.discard(side)
            elif self.last_intent is not None:
                if self.target is None:
                    self._sync_to(target=self.last_intent)
                else:
                    self._retry_sides.add(side)
            return False
        return value != self.target

    def _request(self, *, target: bool) -> asyncio.Future[None]:
        # A repeated explicit OFF still supersedes an older queued INPUT wake.
        # Keep this separate from physical-command generation and coalescing.
        self.requested(target)
        self._intent_sequence += 1
        self.last_intent = target
        return self._sync_to(target=target)

    def _sync_to(self, *, target: bool) -> asyncio.Future[None]:
        """Reconcile accepted intent without treating late feedback as intent."""
        if (
            self.target is target
            and self._future is not None
            and not self._future.done()
        ):
            self.changed()
            return self._future
        if self._future is not None and not self._future.done():
            self._future.set_exception(
                HomeAssistantError(
                    f"{self.name}: power request was superseded by a newer request"
                )
            )
        self._generation += 1
        # An opposite frame may already be in flight while feedback still looks
        # correct. Discrete endpoints can accept the inverse immediately;
        # state-gated endpoints wait for their prior command at dispatch.
        now = asyncio.get_running_loop().time()
        if self.target is None:
            self._discard_expired_commands(now)
        self._retry_sides = {
            side
            for (side, expected), (_, deadline) in self._expected.items()
            if expected is not target and deadline >= now
        }
        self.target = target
        # Only bindings with cancellation-safe endpoints may interrupt a batch.
        # Projector OFF must finish its persistent latch and physical sequence.
        if (
            self.preempt_commands
            and self._command_batch is not None
            and not self._command_batch.done()
        ):
            self._command_batch.cancel()
        self.last_error = None
        self._deadline = asyncio.get_running_loop().time() + self.timeout
        self._future = asyncio.get_running_loop().create_future()
        # State-driven requests need no caller; still retrieve failures so their
        # explicit diagnostic error is not also an unhandled-future warning.
        self._future.add_done_callback(
            lambda future: future.exception() if not future.cancelled() else None
        )
        self._wake.set()
        if self._worker is None or self._worker.done():
            self._worker = self._create_task(self._drive(), f"{self.name} power sync")
        self.changed()
        return self._future

    async def request(self, *, target: bool) -> None:
        """Request both devices, with confirmation and duplicate coalescing."""
        if not self._started:
            error_message = f"{self.name}: power synchronization is not ready"
            raise HomeAssistantError(error_message)
        await asyncio.shield(self._request(target=target))

    @property
    def confirmed_on(self) -> bool:
        """Both live endpoints confirmed the latest completed ON."""
        return (
            self._started
            and self.target is None
            and self.last_intent is True
            and self.last_error is None
            and all(endpoint.read() is True for endpoint in self.endpoints.values())
        )

    async def wait_for_start(self) -> bool:
        """Join the current ON confirmation without submitting another intent."""
        pending = self._future
        if not self._started or self.target is not True or pending is None:
            return False
        try:
            await asyncio.shield(pending)
        except HomeAssistantError:
            return False
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if pending.cancelled() and caller is not None and not caller.cancelling():
                return False  # The pair unloaded, the caller is still running.
            raise
        return self._future is pending and self.confirmed_on

    async def wake_if_current(self, expected_revision: str) -> None:
        """Accept queued ON only if no newer power intent has been received."""
        if not self._started:
            error_message = f"{self.name}: power synchronization is not ready"
            raise HomeAssistantError(error_message)
        if expected_revision != self.intent_revision:
            error_message = (
                f"{self.name}: queued wake was superseded by a newer request"
            )
            raise HomeAssistantError(error_message)
        if self.endpoints["display"].read() is None:
            error_message = f"{self.name}: display power state is unknown"
            raise HomeAssistantError(error_message)
        # No await separates the compare from recording the new intent.
        await asyncio.shield(self._request(target=True))

    async def _drive(self) -> None:
        attempted: set[tuple[int, str]] = set()
        try:
            while self.target is not None and self._started:
                generation, target = self._generation, self.target
                values = {
                    side: endpoint.read() for side, endpoint in self.endpoints.items()
                }
                # A native read can precede its state_changed callback. Consume
                # that acknowledgement before issuing a state-dependent inverse.
                for side, (pending, _) in tuple(self._pending_commands.items()):
                    if values[side] is pending:
                        self._new_transition(side, value=pending)
                if (
                    not self._pending_commands
                    and not self._retry_sides
                    and all(value is target for value in values.values())
                ):
                    self._finish(generation)
                    continue
                deadline = min(
                    [self._deadline]
                    + [deadline for _, deadline in self._pending_commands.values()]
                )
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    self._finish(
                        generation,
                        "Both devices did not confirm the requested power state",
                    )
                    continue
                commands = self._commands_for(
                    values, generation, target=target, attempted=attempted
                )
                if commands:
                    await self._async_run_commands(commands, generation, remaining)
                    continue
                await self._async_wait_for_feedback(remaining)
        finally:
            self._worker = None

    def _commands_for(
        self,
        values: dict[str, bool | None],
        generation: int,
        *,
        target: bool,
        attempted: set[tuple[int, str]],
    ) -> list[Awaitable[None]]:
        commands = []
        for side, endpoint in self.endpoints.items():
            if (
                values["display"] is None
                and not self.endpoints["display"].allow_unknown_command
            ):
                break
            if side in self._pending_commands:
                continue
            if values[side] is target and side not in self._retry_sides:
                continue
            if (generation, side) in attempted and side not in self._retry_sides:
                continue
            if values[side] is None and not endpoint.allow_unknown_command:
                continue
            commands.append(
                self._async_command(
                    side, generation, target=target, attempted=attempted
                )
            )
        return commands

    async def _async_command(
        self,
        side: str,
        generation: int,
        *,
        target: bool,
        attempted: set[tuple[int, str]],
    ) -> None:
        if not self._started or (
            self.preempt_commands and generation != self._generation
        ):
            return
        endpoint = self.endpoints[side]
        value = endpoint.read()
        if value is None and not endpoint.allow_unknown_command:
            return
        attempted.add((generation, side))
        self._retry_sides.discard(side)
        deadline = asyncio.get_running_loop().time() + self.timeout
        if value is not target or self._last[side] is not target:
            self._expected[side, target] = (generation, deadline)
        # Register only when this coroutine actually starts. Cancellation before
        # dispatch creates no phantom expectation or native feedback fence.
        if endpoint.wait_for_feedback and value is not target:
            self._pending_commands[side] = (target, deadline)
        # A forced discrete inverse may still send from a matching observation;
        # retain older genuine expectations without inventing a new state edge.
        await endpoint.command(target)

    def _discard_expired_commands(self, now: float) -> None:
        self._pending_commands = {
            side: pending
            for side, pending in self._pending_commands.items()
            if pending[1] > now
        }

    async def _async_run_commands(
        self, commands: list[Awaitable[None]], generation: int, remaining: float
    ) -> None:
        self._command_batch = asyncio.gather(*commands, return_exceptions=True)
        try:
            async with asyncio.timeout(remaining):
                results = await self._command_batch
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if (
                generation != self._generation
                and self._started
                and caller is not None
                and not caller.cancelling()
            ):
                # Keep the one driver alive; only this obsolete batch ended.
                return
            raise
        except TimeoutError:
            results = [HomeAssistantError("Power command confirmation timed out")]
        finally:
            self._command_batch = None
        if generation != self._generation:
            return
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            failure = failures[0]
            self._finish(generation, str(failure) or type(failure).__name__)

    async def _async_wait_for_feedback(self, remaining: float) -> None:
        self._wake.clear()
        with suppress(TimeoutError):
            # Re-read freshness even when unchanged native states do not
            # publish state_changed events.
            await asyncio.wait_for(self._wake.wait(), min(1.0, remaining))

    def _finish(self, generation: int, error: str | None = None) -> None:
        if generation != self._generation:
            return
        self.target = None
        self._discard_expired_commands(asyncio.get_running_loop().time())
        self.last_error = error
        self._retry_sides.clear()
        if self._future is not None and not self._future.done():
            if error is not None:
                _LOGGER.error("%s power synchronization: %s", self.name, error)
                self._future.set_exception(HomeAssistantError(f"{self.name}: {error}"))
            else:
                self._future.set_result(None)
        self.changed()

    async def close(self) -> None:
        """Stop monitoring; unloading never sends a power command."""
        self._started = False
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        if self._future is not None and not self._future.done():
            self._future.cancel()
        self.target = None
        self._expected.clear()
        self._retry_sides.clear()
        self._pending_commands.clear()
