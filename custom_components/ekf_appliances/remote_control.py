"""HA bindings and final dispatch guards for the canonical universal remote mode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping

    from homeassistant.core import Event, HomeAssistant, ServiceCall, ServiceResponse

    from .remote_mode import RemoteRoute
    from .runtime import ApplianceRuntime

import asyncio
import logging
import time
from contextvars import ContextVar
from uuid import uuid4

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN
from .remote_mode import INTERNAL_KEYS, PowerObservations, RemoteMode
from .runtime import PROJECTOR_SHUTDOWN_TIMEOUT, UNIVERSAL_PROJECTOR_COMMAND

_LOGGER = logging.getLogger(__name__)
TV = "media_player.ue48j5500_ue48j5500"
PROJECTOR = "media_player.projector"
TARGET = "sensor.universal_remote_target"
_ON = {"on", "playing", "paused", "idle", "buffering"}
_SERVICE_CONTEXT = ContextVar("universal_remote_service_context", default=None)


_CAPTURED_ROUTE_LIMIT = 256
_POWER_CAPTURE_TIMEOUT = 5.0
_POWER_OPERATION_TIMEOUT = (
    150.0  # Longer than the paired/native confirmation deadlines.
)
_MEDIA_BUTTONS = {
    "up",
    "down",
    "left",
    "right",
    "ok",
    "ok_hold",
    "back",
    "menu",
    "exit",
    "home",
    "info",
    "guide",
    "volume_up",
    "volume_down",
    "mute",
    "play",
    "prev",
    "next",
    "dash",
    *map(str, range(10)),
}
_WAKE_SERVICES = {"turn_on", "turn_off", "toggle", "select_source", "play_media"}


class PowerConfirmationTimeoutError(HomeAssistantError):
    """A bounded owned operation timed out and already emitted its rejection."""


def _with_service_context(
    handler: Callable[[ServiceCall], Awaitable[ServiceResponse]],
) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    """Retain caller identity in nested services, tasks and delayed callbacks."""

    async def handle(call: ServiceCall) -> ServiceResponse:
        token = _SERVICE_CONTEXT.set(call.context)
        try:
            return await handler(call)
        finally:
            _SERVICE_CONTEXT.reset(token)

    return handle


class RemoteController:
    """Publish complete target transitions; helpers are compatibility mirrors."""

    def __init__(self, runtime: ApplianceRuntime) -> None:
        """Bind the canonical mode to HA state, native dispatch and shared runtime."""
        self.runtime, self.hass = runtime, runtime.hass
        self.listeners = []
        self.initialized = False
        self._tasks = set()
        self._projector_dispatches = set()
        self._captured_routes = {}
        self._captured_press_ids: dict[tuple[str, float], str] = {}
        self._context_press_ids: dict[str, str] = {}
        self._settled_projector_shutdown_at: float | None = None
        self._pending_power: tuple[str, str, float] | None = None
        self._power_operations: dict[str, object] = {}
        self._mirror_lock = asyncio.Lock()
        self._closed = False
        self._clear_audio = False
        self._unsubscribe = None
        self.mode = RemoteMode(
            clock=time.time,
            schedule=self._schedule,
            changed=self._changed,
            wake=self._wake,
        )
        self.mode.shutting_down = runtime.projector_shutting_down

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        if self._closed:
            coroutine.close()
            return
        task = self.hass.async_create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _schedule(
        self, delay: float, action: Callable[[], None]
    ) -> asyncio.TimerHandle:
        @callback
        def run() -> None:
            if not self._closed:
                self.refresh()
                action()

        return self.hass.loop.call_later(delay, run)

    def _changed(self) -> None:
        audio = self.hass.states.get("input_boolean.audio_mode")
        if not self.mode.audio and audio is not None and audio.state == "on":
            self._clear_audio = True
        for listener in tuple(self.listeners):
            listener()
        self._spawn(self.async_mirror())

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to complete committed mode transitions."""
        self.listeners.append(listener)
        return lambda: (
            self.listeners.remove(listener) if listener in self.listeners else None
        )

    def refresh(self, *, physical_at: float | None = None) -> None:
        """Reconcile physical input and fresh power observations before routing."""
        if self._closed:
            return
        # Ready receiver input owns its capture time even if a timer, state
        # callback or service runs first after a busy event-loop turn.
        receiver = getattr(self.runtime, "receiver", None)
        if physical_at is None and receiver is not None:
            receiver.drain_pending()
        self._cancel_shutdown_dispatches()
        tv = self._tv_power_state()
        revisions, intents, targets = self._power_observations()
        audio_on = self._audio_mode_active()
        self.mode.observe(
            PowerObservations(
                tv=tv,
                projector=self.runtime.projector_power_state,
                shutting_down=self.runtime.projector_shutting_down,
                revisions=revisions,
                intents=intents,
                targets=targets,
            ),
            audio=audio_on,
            at=physical_at,
        )

    def _cancel_shutdown_dispatches(self) -> None:
        if not self.runtime.projector_shutting_down:
            return
        # Blocking HA service calls propagate cancellation into their native
        # handlers. Discard accepted work still waiting in a native queue.
        current = asyncio.current_task()
        for task in tuple(self._projector_dispatches):
            if task is not current:
                task.cancel()

    def _tv_power_state(self) -> bool | None:
        television = self.hass.states.get(TV)
        tv = None
        if television is not None and not television.attributes.get("restored"):
            tv = (
                True
                if television.state in _ON
                else False
                if television.state == "off"
                else None
            )
        if self.runtime.tv_power_pair is not None:
            tv = self.runtime.tv_power_pair.endpoints["display"].read()
        return tv

    def _power_observations(
        self,
    ) -> tuple[dict[str, str], dict[str, bool | None], dict[str, bool | None]]:
        pairs = {
            "tv": self.runtime.tv_power_pair,
            "projector": self.runtime.projector_power_pair,
        }
        revisions, intents, targets = {}, {}, {}
        for pair, backend in pairs.items():
            if backend is not None:
                revisions[pair], intents[pair], targets[pair] = (
                    backend.intent_revision,
                    backend.last_intent,
                    backend.target,
                )
            else:
                state = self.hass.states.get(TV if pair == "tv" else PROJECTOR)
                attrs = state.attributes if state is not None else {}
                revisions[pair] = attrs.get("power_intent_revision") or ""
                intents[pair], targets[pair] = (
                    attrs.get("last_power_intent"),
                    attrs.get("power_sync_target"),
                )
        return revisions, intents, targets

    def _audio_mode_active(self) -> bool:
        audio = self.hass.states.get("input_boolean.audio_mode")
        audio_on = audio is not None and audio.state == "on"
        if not audio_on:
            self._clear_audio = False
        return audio_on and not self._clear_audio

    async def async_start(self, _hass: HomeAssistant | None = None) -> None:
        """Subscribe to HA observations and publish the initialized target."""
        self._unsubscribe = async_track_state_change_event(
            self.hass,
            [
                TV,
                PROJECTOR,
                self.runtime.projector_power_entity_id,
                "input_boolean.audio_mode",
            ],
            self._state_changed,
        )
        self.refresh()
        await self.async_mirror()
        self.initialized = True
        for listener in tuple(self.listeners):
            listener()

    @callback
    def _state_changed(self, _event: Event) -> None:
        self.refresh()

    async def async_mirror(self) -> None:
        """Update legacy helpers from the complete committed controller state."""
        async with self._mirror_lock:
            if self._closed:
                return
            values = [
                (
                    "input_select.universal_remote_pair",
                    self.mode.pair,
                    "select_option",
                    "option",
                ),
                (
                    "input_boolean.universal_remote_native_target",
                    "on" if self.mode.native else "off",
                    None,
                    None,
                ),
                (
                    "input_boolean.nav_to_tv",
                    "on" if self.mode.native and self.mode.pair == "tv" else "off",
                    None,
                    None,
                ),
                (
                    "input_text.universal_remote_focus_seconds",
                    "60",
                    "set_value",
                    "value",
                ),
                (
                    "input_text.universal_remote_projector_until_ms",
                    str(round(self.mode.expires_at * 1000)),
                    "set_value",
                    "value",
                ),
            ]
            if self._clear_audio:
                values.append(("input_boolean.audio_mode", "off", None, None))
            for entity_id, wanted, service, field in values:
                state = self.hass.states.get(entity_id)
                if state is None or state.state == wanted:
                    continue
                domain = entity_id.split(".", 1)[0]
                action = service or "turn_" + wanted
                if self.hass.services.has_service(domain, action):
                    await self.hass.services.async_call(
                        domain,
                        action,
                        {"entity_id": entity_id, **({field: wanted} if field else {})},
                        blocking=True,
                        context=_SERVICE_CONTEXT.get(),
                    )

    def _wake(self, pair: str, revision: str) -> None:
        async def wake() -> None:
            self.refresh()
            if reason := self._pair_transition(pair):
                await self.async_reject(reason)
                return
            try:
                await self._async_owned_power(
                    pair,
                    self.hass.services.async_call(
                        DOMAIN,
                        "wake_if_current",
                        {
                            "entity_id": PROJECTOR if pair == "projector" else TV,
                            "expected_revision": revision,
                        },
                        blocking=True,
                        context=_SERVICE_CONTEXT.get(),
                    ),
                )
            except HomeAssistantError as err:
                if not isinstance(err, PowerConfirmationTimeoutError):
                    await self.async_reject("wake_rejected")
                _LOGGER.warning("Remote single INPUT wake was not accepted: %s", err)
            finally:
                self.refresh()

        self._spawn(wake())

    @staticmethod
    def _capture_key(button: str, received_ms: float) -> tuple[str, float]:
        return button, round(received_ms, 3)

    def physical_press(
        self, name: str, received_at: float, *, press_id: str | None = None
    ) -> None:
        """Commit trusted receiver timing before asynchronous YAML dispatch."""
        # A service may drain an already-ready receiver frame. That physical
        # action belongs to the receiver, not to the unrelated service caller.
        token = _SERVICE_CONTEXT.set(None)
        try:
            button = (
                name.split("_", 3)[2]
                if name.startswith(("t9_add_", "t9_cycle_"))
                else name
            )
            key = self._capture_key(button, received_at * 1000)
            if key in self._captured_routes:
                return
            self.refresh(physical_at=received_at)
            route = self.mode.press(button, received_at)
            if button == "ok_hold" and press_id is not None:
                # Both events from one physical OK share the first event's owner
                # even if focus changes between separate receiver frames.
                for previous, identity in reversed(self._captured_press_ids.items()):
                    if previous[0] == "ok" and identity == press_id:
                        route = self._captured_routes[previous]
                        break
            self._captured_routes[key] = self._prepare_press(button, route=route)
            self._captured_press_ids[key] = press_id or f"{key[0]}:{key[1]:.3f}"
            if len(self._captured_routes) > _CAPTURED_ROUTE_LIMIT:
                expired = next(iter(self._captured_routes))
                del self._captured_routes[expired]
                self._captured_press_ids.pop(expired, None)
        finally:
            _SERVICE_CONTEXT.reset(token)

    async def async_prepare(self, data: Mapping[str, Any]) -> RemoteRoute:
        """Return the route captured for a physical press without replaying it."""
        received = data.get("received_ms")
        if received is None:
            # Explicit compatibility calls have no physical timestamp to trust.
            self.refresh()
            captured = self.mode.press(data["button"])
            if data["button"] == "power":
                await self._async_check_control(
                    PROJECTOR if captured["pair"] == "projector" else TV
                )
                self.refresh()
            route = self._prepare_press(
                data["button"], route=captured, reserve_power=False
            )
        else:
            # A replay, an evicted event or a caller-supplied timestamp must
            # never become another physical press or backdate a lease.
            key = self._capture_key(data["button"], received)
            route = self._captured_routes.get(key)
            context_id = getattr(_SERVICE_CONTEXT.get(), "id", None)
            identity = self._captured_press_ids.get(key)
            if route is not None and context_id is not None and identity is not None:
                # HA keeps this automation Context through nested named scripts.
                self._context_press_ids[context_id] = identity
                if len(self._context_press_ids) > _CAPTURED_ROUTE_LIMIT:
                    del self._context_press_ids[next(iter(self._context_press_ids))]
            if route is None:
                route = self.resolve({})
                if data["button"] == "power":
                    route = {**route, "blocked": "expired_power_press"}
        if reason := route.get("blocked"):
            press_id = None
            if received is not None:
                key = self._capture_key(data["button"], received)
                press_id = self._captured_press_ids.get(key, f"{key[0]}:{key[1]:.3f}")
            await self.async_reject(reason, press_id=press_id)
        await self.async_mirror()
        return route

    def _pending_power_request(self) -> tuple[str, str, float] | None:
        pending = self._pending_power
        if pending and pending[2] <= asyncio.get_running_loop().time():
            self._pending_power = None
        return self._pending_power

    def _pair_transition(self, pair: str) -> str | None:
        if pair == "projector" and self.runtime.projector_shutting_down:
            return "projector_shutting_down"
        return self._active_transition(pair)

    def _active_transition(self, pair: str) -> str | None:
        pending = self._pending_power_request()
        if (
            pair in self._power_operations
            or self.mode.targets[pair] is not None
            or (pending is not None and pending[1] == pair)
        ):
            return f"{pair}_power_transition"
        return None

    def _power_transition(self) -> str | None:
        if reason := self._active_transition("projector") or self._active_transition(
            "tv"
        ):
            return reason
        # Runtime records this timestamp and RemoteMode measures age in wall time.
        requested_at = self.runtime.shutdown.requested_at
        if (
            self.runtime.projector_shutting_down
            and requested_at != self._settled_projector_shutdown_at
            and 0 <= self.mode.clock() - requested_at < PROJECTOR_SHUTDOWN_TIMEOUT
        ):
            # OEM/script-only OFF has no paired target. Its recent window is
            # transient; an old persistent safety latch still guards only projector.
            return "projector_shutting_down"
        return None

    def _ordinary_command_blocked(self, pair: str) -> str | None:
        if reason := self._pair_transition(pair):
            return reason
        observed = (
            self.runtime.projector_power_state
            if pair == "projector"
            else self._tv_power_state()
        )
        if observed is None:
            return f"{pair}_power_unknown"
        return f"{pair}_off" if observed is False else None

    def _prepare_press(
        self,
        button: str,
        at: float | None = None,
        *,
        route: RemoteRoute | None = None,
        reserve_power: bool = True,
    ) -> RemoteRoute:
        route = self.mode.press(button, at) if route is None else route
        if button in INTERNAL_KEYS:
            return route
        # Freeze ownership before another physical key or timeout changes focus.
        # An OK hold reuses the already prepared route of the same physical press.
        route = {**route, "speaker": route.get("speaker", self.mode.audio)}
        reason = None
        if button == "power":
            reason = self._power_transition() or self._pair_transition(route["pair"])
            if reason is None and reserve_power:
                request_id = uuid4().hex
                self._pending_power = (
                    request_id,
                    route["pair"],
                    asyncio.get_running_loop().time() + _POWER_CAPTURE_TIMEOUT,
                )
                return {**route, "power_request_id": request_id}
        elif button in _MEDIA_BUTTONS and not route["speaker"]:
            reason = self._ordinary_command_blocked(route["pair"])
        elif button in {"netflix", "youtube", "apple_tv"} and not route["speaker"]:
            reason = self._pair_transition("tv")
        elif button in {"disney", "disney_hold"}:
            reason = self._pair_transition("projector")
        return {**route, "blocked": reason} if reason else route

    async def async_reject(
        self, reason: str, *, press_id: str | None = None
    ) -> dict[str, Any]:
        """Signal one rejected logical command without forwarding its device action."""
        context = _SERVICE_CONTEXT.get()
        context_id = getattr(context, "id", None)
        identity = press_id or self._context_press_ids.get(context_id) or context_id
        if self.hass.services.has_service("remote_announcements", "reject"):
            try:
                await self.hass.services.async_call(
                    "remote_announcements",
                    "reject",
                    {"reason": reason, **({"press_id": identity} if identity else {})},
                    blocking=True,
                    context=context,
                )
            except HomeAssistantError as err:
                _LOGGER.warning("Remote rejection cue unavailable: %s", err)
        return {"sent": False, "blocked": reason}

    async def _async_owned_power(
        self,
        pair: str,
        action: Coroutine[Any, Any, Any],
        *,
        owns_shutdown: bool = False,
    ) -> Any:
        """Reserve synchronously, then retain admission through caller cancellation."""
        owner = object()
        self._power_operations[pair] = owner
        started = False

        def release() -> None:
            if self._power_operations.get(pair) is owner:
                del self._power_operations[pair]
                if owns_shutdown:
                    # A finished/failed owned OFF must not become a new native
                    # busy window merely because its persistent latch remains.
                    self._settled_projector_shutdown_at = (
                        self.runtime.shutdown.requested_at
                    )

        async def run() -> Any:
            nonlocal started
            started = True
            try:
                async with asyncio.timeout(_POWER_OPERATION_TIMEOUT):
                    return await action
            except TimeoutError as err:
                await self.async_reject("power_confirmation_timeout")
                message = "Remote power confirmation timed out"
                raise PowerConfirmationTimeoutError(message) from err
            finally:
                release()

        def finished(done: asyncio.Task[Any]) -> None:
            self._tasks.discard(done)
            # A shutdown can cancel the task before run() reaches its finally.
            if not started:
                action.close()
                release()
            if not done.cancelled():
                done.exception()

        task = self.hass.async_create_task(run())
        self._tasks.add(task)
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def async_select(self, data: Mapping[str, Any]) -> RemoteRoute:
        """Commit an explicit route selection and update compatibility helpers."""
        self.refresh()
        route = self.mode.select(
            data["pair"],
            native=data.get("native", False),
            explicit=data.get("explicit", True),
        )
        await self.async_mirror()
        return route

    def resolve(self, data: Mapping[str, Any]) -> RemoteRoute:
        """Resolve the current safe route without renewing a physical lease."""
        self.refresh()
        return self.mode.resolve(data.get("pair"), native=data.get("native"))

    async def _async_check_control(self, entity: str) -> None:
        context = _SERVICE_CONTEXT.get()
        if context is None or not context.user_id:
            return
        user = await self.hass.auth.async_get_user(context.user_id)
        if user is None:
            raise UnknownUser(context=context)
        if not user.is_admin and not user.permissions.check_entity(
            entity, POLICY_CONTROL
        ):
            raise Unauthorized(
                context=context, entity_id=entity, permission=POLICY_CONTROL
            )

    async def async_power(self, data: Mapping[str, Any]) -> dict[str, str | bool]:
        """Authorize and dispatch one power action with immediate shutdown guarding."""
        route = self.resolve(data)
        # Navigation may fall back during shutdown; captured power intent must
        # never cross to the other display while waiting for dispatch.
        pair = data.get("pair", route["pair"])
        entity = PROJECTOR if pair == "projector" else TV
        await self._async_check_control(entity)
        self.refresh()
        request_id = data.get("power_request_id")
        if request_id is not None:
            pending = self._pending_power_request()
            if pending is None or pending[:2] != (request_id, pair):
                return await self.async_reject(
                    "expired_power_press", press_id=request_id
                )
            self._pending_power = None
        if reason := (self._power_transition() or self._pair_transition(pair)):
            return await self.async_reject(reason, press_id=request_id)
        if pair == "projector":
            observed = self.runtime.projector_power_state
            if observed is True:
                self.runtime.note_projector_off_requested()
            # This accepted lifecycle operation owns its confirmation frames.
            # Later remote actions see the latch before transport dispatch.
            service = (
                "toggle" if observed is None else "turn_off" if observed else "turn_on"
            )
        else:
            service = "toggle"
        return await self._async_owned_power(
            pair,
            self._async_service(
                {
                    "domain": "media_player",
                    "service": service,
                    "service_data": {"entity_id": entity},
                },
                projector_action=pair == "projector" and service != "turn_off",
                power_lifecycle=True,
            ),
            owns_shutdown=pair == "projector" and service == "turn_off",
        )

    async def async_dispatch(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Forward a native service with caller context and final shutdown checks."""
        domain, service = data["domain"], data["service"]
        payload = dict(data.get("service_data", {}))
        entities = payload.get("entity_id", [])
        entities = [entities] if isinstance(entities, str) else entities
        kodi = self.runtime.entry.data.get("tv_kodi_entity_id", "media_player.home")
        projector_action = (
            data.get("pair") == "projector"
            or domain == "kodi"
            or (domain == "rest_command" and service.startswith("host_kodi_"))
            or any(
                entity in {PROJECTOR, kodi} or entity.startswith("button.projector_")
                for entity in entities
            )
        )
        pair = "projector" if projector_action else data.get("pair")
        tv_entities = {
            TV,
            TV + "_network",
            "media_player.televizor",
            "remote.televizor",
            "remote.ue48j5500_ue48j5500",
            self.runtime.entry.data.get("tv_entity_id"),
            self.runtime.entry.data.get("tv_androidtv_entity_id"),
        }
        if pair is None and any(entity in tv_entities for entity in entities):
            pair = "tv"
        for entity in entities:
            await self._async_check_control(entity)
        self.refresh()
        if pair is not None:
            reason = (
                self._pair_transition(pair)
                if service in _WAKE_SERVICES
                else self._ordinary_command_blocked(pair)
            )
            if reason:
                return await self.async_reject(reason)
        power_action = (
            pair is not None
            and domain in {"media_player", "remote"}
            and service in {"turn_on", "turn_off", "toggle"}
        )
        operation = self._async_service(
            data,
            projector_action=projector_action
            and not (power_action and service == "turn_off"),
            power_lifecycle=power_action,
        )
        if power_action:
            return await self._async_owned_power(
                pair,
                operation,
                owns_shutdown=pair == "projector" and service == "turn_off",
            )
        return await operation

    async def _async_service(
        self,
        data: Mapping[str, Any],
        *,
        projector_action: bool,
        power_lifecycle: bool = False,
    ) -> dict[str, Any]:
        domain, service = data["domain"], data["service"]
        payload = dict(data.get("service_data", {}))
        token = UNIVERSAL_PROJECTOR_COMMAND.set(projector_action)
        task = asyncio.current_task()
        # An admitted lifecycle owns its wait, while ON/unknown still carries
        # the final transmit guard against a genuine later native shutdown.
        cancellable_dispatch = projector_action and not power_lifecycle
        if cancellable_dispatch:
            self._projector_dispatches.add(task)
        try:
            result = await self.hass.services.async_call(
                domain,
                service,
                payload,
                blocking=True,
                return_response=data.get("return_response", False),
                context=_SERVICE_CONTEXT.get(),
            )
        except asyncio.CancelledError:
            if cancellable_dispatch and self.runtime.projector_shutting_down:
                # The hardware action has been discarded. A later refresh must
                # not cancel its single rejection or the structured response.
                self._projector_dispatches.discard(task)
                return await self.async_reject("projector_shutting_down")
            raise
        except HomeAssistantError:
            await self.async_reject("command_failed")
            raise
        finally:
            self._projector_dispatches.discard(task)
            UNIVERSAL_PROJECTOR_COMMAND.reset(token)
        return result if isinstance(result, dict) else {"sent": True}

    async def async_close(self) -> None:
        """Cancel controller observers, deadlines and queued dispatch tasks."""
        self._closed = True
        self._captured_routes.clear()
        self._captured_press_ids.clear()
        self._context_press_ids.clear()
        self._pending_power = None
        self.mode.close()
        if self._unsubscribe is not None:
            self._unsubscribe()
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


def async_register_remote_services(hass: HomeAssistant) -> None:
    """Only native integrations own device transports; this boundary guards them."""

    def controller() -> RemoteController:
        value = hass.data.get(DOMAIN, {}).get("remote_controller")
        if value is None:
            error_message = "Universal remote mode controller is not ready"
            raise HomeAssistantError(error_message)
        return value

    async def prepare(call: ServiceCall) -> ServiceResponse:
        return await controller().async_prepare(call.data)

    async def select(call: ServiceCall) -> ServiceResponse:
        return await controller().async_select(call.data)

    async def resolve(call: ServiceCall) -> ServiceResponse:
        return controller().resolve(call.data)

    async def dispatch(call: ServiceCall) -> ServiceResponse:
        return await controller().async_dispatch(call.data)

    async def power(call: ServiceCall) -> ServiceResponse:
        return await controller().async_power(call.data)

    async def reject(call: ServiceCall) -> ServiceResponse:
        return await controller().async_reject(
            call.data["reason"], press_id=call.data.get("press_id")
        )

    pair = {
        vol.Optional("pair"): vol.In(["tv", "projector"]),
        vol.Optional("native"): cv.boolean,
    }
    services = {
        "remote_prepare": (
            prepare,
            {
                vol.Required("button"): cv.string,
                vol.Optional("received_ms"): vol.Any(None, vol.Coerce(float)),
                vol.Optional("expected_ms"): vol.Coerce(float),
                vol.Optional("wake_id"): cv.string,
            },
        ),
        "remote_select": (
            select,
            {
                vol.Required("pair"): vol.In(["tv", "projector"]),
                vol.Optional("native", default=False): cv.boolean,
                vol.Optional("explicit", default=True): cv.boolean,
            },
        ),
        "remote_resolve": (resolve, pair),
        "remote_power": (power, {**pair, vol.Optional("power_request_id"): cv.string}),
        "remote_reject": (
            reject,
            {vol.Required("reason"): cv.string, vol.Optional("press_id"): cv.string},
        ),
        "remote_dispatch": (
            dispatch,
            {
                vol.Required("domain"): vol.In(
                    [
                        "kodi",
                        "media_player",
                        "button",
                        "rest_command",
                        "remote",
                        "androidtv_remote",
                    ]
                ),
                vol.Required("service"): cv.string,
                vol.Optional("service_data", default={}): dict,
                vol.Optional("pair"): vol.In(["tv", "projector"]),
                vol.Optional("return_response", default=False): cv.boolean,
            },
        ),
    }
    for name, (handler, schema) in services.items():
        hass.services.async_register(
            DOMAIN,
            name,
            _with_service_context(handler),
            schema=vol.Schema(schema),
            supports_response=SupportsResponse.OPTIONAL,
        )
