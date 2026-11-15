"""Synchronize a display and its player using independent observed power.

Native integrations know nothing about infrared remotes. This controller only
combines their observations and explicit power services. Unknown observations
never mean off, and an old in-flight command cannot replace a newer intent.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from uuid import uuid4

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PowerEndpoint:
    """Read actual power and issue an idempotent request for one endpoint."""

    read: Callable[[], bool | None]
    command: Callable[[bool], Awaitable[None]]
    allow_unknown_command: bool = False


class PowerPair:
    """Keep one desired state while both independent devices confirm it."""

    def __init__(
        self, name: str, display: PowerEndpoint, player: PowerEndpoint, *,
        timeout: float = 120.0, changed: Callable[[], None] | None = None,
        create_task=None, requested: Callable[[bool], None] | None = None,
    ) -> None:
        self.name = name
        self.endpoints = {"display": display, "player": player}
        self.timeout = timeout
        self.changed = changed or (lambda: None)
        self.requested = requested or (lambda target: None)
        self._create_task = create_task or (lambda coro, name: asyncio.create_task(coro, name=name))
        self.target: bool | None = None
        self.last_error: str | None = None
        self.last_intent: bool | None = None
        self._intent_epoch = uuid4().hex
        self._intent_sequence = 0
        self._last = {side: None for side in self.endpoints}
        self._expected: dict[str, tuple[int, bool]] = {}
        self._started = False
        self._baseline_ready = False
        self._generation = 0
        self._deadline = 0.0
        self._future: asyncio.Future[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    @property
    def intent_revision(self) -> str:
        """Identify accepted intent, independent of status updates and completion."""
        return f"{self._intent_epoch}:{self._intent_sequence}"

    def start(self) -> None:
        """Establish a baseline, then reconcile startup from the physical display."""
        self._started = True
        self._baseline_ready = False
        self._last = {side: None for side in self.endpoints}
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

        transitions = [value for side, value in values.items() if self._new_transition(side, value)]
        if transitions:
            # HA state events normally change one side at a time. If both changed
            # together and disagree, the actual display remains authoritative.
            desired = transitions[-1]
            if len(transitions) > 1 and values["display"] is not None:
                desired = values["display"]
            self._request(desired)
        self._wake.set()

    def _observe_baseline(self, values):
        for side, value in values.items():
            if value is not None:
                self._last[side] = value
        if all(value is not None for value in values.values()):
            self._baseline_ready = True
            if self.target is None and values["display"] != values["player"]:
                self._request(values["display"])

    def _new_transition(self, side, value):
        previous = self._last[side]
        if value is None:
            return False
        self._last[side] = value
        if previous is None or value == previous:
            return False
        expected = self._expected.get(side)
        # An older requested ON can arrive after a newer OFF was requested.
        # It is confirmation of that old command, not a new human request.
        if expected and expected[1] is value:
            self._expected.pop(side, None)
            if expected[0] < self._generation:
                return False
        return value != self.target

    def _request(self, target: bool) -> asyncio.Future[None]:
        # A repeated explicit OFF still supersedes an older queued INPUT wake.
        # Keep this separate from physical-command generation and coalescing.
        self.requested(target)
        self._intent_sequence += 1
        self.last_intent = target
        if self.target is target and self._future is not None and not self._future.done():
            self.changed()
            return self._future
        if self._future is not None and not self._future.done():
            self._future.set_exception(HomeAssistantError(
                f"{self.name}: power request was superseded by a newer request"
            ))
        self._generation += 1
        self.target = target
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

    async def request(self, target: bool) -> None:
        """Request both devices, with confirmation and duplicate coalescing."""
        if not self._started:
            raise HomeAssistantError(f"{self.name}: power synchronization is not ready")
        await asyncio.shield(self._request(target))

    async def wake_if_current(self, expected_revision: str) -> None:
        """Accept queued ON only if no newer power intent has been received."""
        if not self._started:
            raise HomeAssistantError(f"{self.name}: power synchronization is not ready")
        if expected_revision != self.intent_revision:
            raise HomeAssistantError(f"{self.name}: queued wake was superseded by a newer request")
        if self.endpoints["display"].read() is None:
            raise HomeAssistantError(f"{self.name}: display power state is unknown")
        # No await separates the compare from recording the new intent.
        await asyncio.shield(self._request(True))

    async def _drive(self) -> None:
        attempted: set[tuple[int, str]] = set()
        try:
            while self.target is not None and self._started:
                generation, target = self._generation, self.target
                values = {side: endpoint.read() for side, endpoint in self.endpoints.items()}
                if all(value is target for value in values.values()):
                    self._finish(generation)
                    continue
                remaining = self._deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    self._finish(generation, "Both devices did not confirm the requested power state")
                    continue
                commands = self._commands_for(values, generation, target, attempted)
                if commands:
                    await self._async_run_commands(commands, generation, remaining)
                    continue
                await self._async_wait_for_feedback(remaining)
        except asyncio.CancelledError:
            raise
        finally:
            self._worker = None

    def _commands_for(self, values, generation, target, attempted):
        commands = []
        for side, endpoint in self.endpoints.items():
            if values["display"] is None and not self.endpoints["display"].allow_unknown_command:
                break
            if values[side] is target or (generation, side) in attempted:
                continue
            if values[side] is None and not endpoint.allow_unknown_command:
                continue
            attempted.add((generation, side))
            self._expected[side] = (generation, target)
            commands.append(endpoint.command(target))
        return commands

    async def _async_run_commands(self, commands, generation, remaining):
        try:
            async with asyncio.timeout(remaining):
                results = await asyncio.gather(*commands, return_exceptions=True)
        except TimeoutError:
            results = [HomeAssistantError("Power command confirmation timed out")]
        if generation != self._generation:
            return
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            self._finish(generation, str(failures[0]))

    async def _async_wait_for_feedback(self, remaining):
        self._wake.clear()
        try:
            # Re-read freshness even when unchanged native states do not
            # publish state_changed events.
            await asyncio.wait_for(self._wake.wait(), min(1.0, remaining))
        except TimeoutError:
            pass

    def _finish(self, generation: int, error: str | None = None) -> None:
        if generation != self._generation:
            return
        self.target = None
        self.last_error = error
        self._expected.clear()
        if self._future is not None and not self._future.done():
            if error:
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
