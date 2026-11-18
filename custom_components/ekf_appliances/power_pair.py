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
    """Read actual power and issue an idempotent request for one endpoint."""

    read: Callable[[], bool | None]
    command: Callable[[bool], Awaitable[None]]
    allow_unknown_command: bool = False


@dataclass(frozen=True)
class PowerPairHooks:
    """Bind HA state publication, accepted intent and task creation together."""

    changed: Callable[[], None] | None = None
    requested: Callable[[bool], None] | None = None
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
        self._last[side] = value
        if previous is None or value == previous:
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
        # correct. Send the new absolute intent before accepting that snapshot.
        now = asyncio.get_running_loop().time()
        self._retry_sides = {
            side
            for (side, expected), (_, deadline) in self._expected.items()
            if expected is not target and deadline >= now
        }
        self.target = target
        if self._command_batch is not None and not self._command_batch.done():
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
                if not self._retry_sides and all(
                    value is target for value in values.values()
                ):
                    self._finish(generation)
                    continue
                remaining = self._deadline - asyncio.get_running_loop().time()
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
            if values[side] is target and side not in self._retry_sides:
                continue
            if (generation, side) in attempted and side not in self._retry_sides:
                continue
            if values[side] is None and not endpoint.allow_unknown_command:
                continue
            attempted.add((generation, side))
            self._retry_sides.discard(side)
            self._expected[side, target] = (
                generation,
                asyncio.get_running_loop().time() + self.timeout,
            )
            commands.append(endpoint.command(target))
        return commands

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
