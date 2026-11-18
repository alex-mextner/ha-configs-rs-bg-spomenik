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

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN
from .remote_mode import PowerObservations, RemoteMode
from .runtime import UNIVERSAL_PROJECTOR_COMMAND

_LOGGER = logging.getLogger(__name__)
TV = "media_player.ue48j5500_ue48j5500"
PROJECTOR = "media_player.projector"
TARGET = "sensor.universal_remote_target"
_ON = {"on", "playing", "paused", "idle", "buffering"}
_SERVICE_CONTEXT = ContextVar("universal_remote_service_context", default=None)


_CAPTURED_ROUTE_LIMIT = 256


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
            if pair == "projector" and self.runtime.projector_shutting_down:
                return
            try:
                await self.hass.services.async_call(
                    DOMAIN,
                    "wake_if_current",
                    {
                        "entity_id": PROJECTOR if pair == "projector" else TV,
                        "expected_revision": revision,
                    },
                    blocking=True,
                    context=_SERVICE_CONTEXT.get(),
                )
            except HomeAssistantError as err:
                _LOGGER.warning("Remote single INPUT wake was not accepted: %s", err)
            finally:
                self.refresh()

        self._spawn(wake())

    @staticmethod
    def _capture_key(button: str, received_ms: float) -> tuple[str, float]:
        return button, round(received_ms, 3)

    def physical_press(self, name: str, received_at: float) -> None:
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
            self._captured_routes[key] = self.mode.press(button, received_at)
            if len(self._captured_routes) > _CAPTURED_ROUTE_LIMIT:
                del self._captured_routes[next(iter(self._captured_routes))]
        finally:
            _SERVICE_CONTEXT.reset(token)

    async def async_prepare(self, data: Mapping[str, Any]) -> RemoteRoute:
        """Return the route captured for a physical press without replaying it."""
        received = data.get("received_ms")
        if received is None:
            # Explicit compatibility calls have no physical timestamp to trust.
            self.refresh()
            route = self.mode.press(data["button"])
        else:
            # A replay, an evicted event or a caller-supplied timestamp must
            # never become another physical press or backdate a lease.
            route = self._captured_routes.get(
                self._capture_key(data["button"], received)
            )
            if route is None:
                route = self.resolve({})
        await self.async_mirror()
        return route

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
        if pair == "projector":
            self.refresh()
            if self.runtime.projector_shutting_down:
                return {"sent": False, "blocked": "projector_shutting_down"}
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
        token = UNIVERSAL_PROJECTOR_COMMAND.set(False)
        try:
            await self.hass.services.async_call(
                "media_player",
                service,
                {"entity_id": entity},
                blocking=True,
                context=_SERVICE_CONTEXT.get(),
            )
        finally:
            UNIVERSAL_PROJECTOR_COMMAND.reset(token)
        return {"sent": True}

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
        self.refresh()
        if projector_action and self.runtime.projector_shutting_down:
            self.mode.resolve("projector", native=False)
            return {"sent": False, "blocked": "projector_shutting_down"}
        if (
            data.get("pair") == "tv"
            and domain in {"remote", "androidtv_remote", "media_player"}
            and service
            not in {"turn_on", "turn_off", "toggle", "select_source", "play_media"}
            and (self._tv_power_state() is False or self.mode.targets["tv"] is False)
        ):
            # Ordinary keys can wake Chromecast through HDMI-CEC. Only an
            # explicit power/source/app action may wake a stopped TV pair.
            return {"sent": False, "blocked": "tv_off"}
        token = UNIVERSAL_PROJECTOR_COMMAND.set(projector_action)
        task = asyncio.current_task()
        if projector_action:
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
            if projector_action and self.runtime.projector_shutting_down:
                return {"sent": False, "blocked": "projector_shutting_down"}
            raise
        finally:
            self._projector_dispatches.discard(task)
            UNIVERSAL_PROJECTOR_COMMAND.reset(token)
        return result if isinstance(result, dict) else {"sent": True}

    async def async_close(self) -> None:
        """Cancel controller observers, deadlines and queued dispatch tasks."""
        self._closed = True
        self._captured_routes.clear()
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
        "remote_power": (power, pair),
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
