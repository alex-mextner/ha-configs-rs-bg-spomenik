"""HA bindings and final dispatch guards for the canonical universal remote mode."""
from __future__ import annotations

import asyncio
import logging
import time

import voluptuous as vol
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN
from .remote_mode import RemoteMode
from .runtime import UNIVERSAL_PROJECTOR_COMMAND

_LOGGER = logging.getLogger(__name__)
TV = "media_player.ue48j5500_ue48j5500"
PROJECTOR = "media_player.projector"
TARGET = "sensor.universal_remote_target"
_ON = {"on", "playing", "paused", "idle", "buffering"}


class RemoteController:
    """Publish complete target transitions; helpers are compatibility mirrors."""

    def __init__(self, runtime) -> None:
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
        self.mode = RemoteMode(clock=time.time, schedule=self._schedule,
                               changed=self._changed, wake=self._wake)
        self.mode.shutting_down = runtime.projector_shutting_down

    def _spawn(self, coroutine):
        if self._closed:
            coroutine.close()
            return
        task = self.hass.async_create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _schedule(self, delay, action):
        @callback
        def run():
            if not self._closed:
                self.refresh()
                action()
        return self.hass.loop.call_later(delay, run)

    def _changed(self):
        audio = self.hass.states.get("input_boolean.audio_mode")
        if not self.mode.audio and audio is not None and audio.state == "on":
            self._clear_audio = True
        for listener in tuple(self.listeners):
            listener()
        self._spawn(self.async_mirror())

    def subscribe(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener) if listener in self.listeners else None

    def refresh(self, *, physical_at=None):
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
        self.mode.observe(tv=tv, projector=self.runtime.projector_power_state,
                          shutting_down=self.runtime.projector_shutting_down,
                          audio=audio_on, revisions=revisions, intents=intents,
                          targets=targets, at=physical_at)

    def _cancel_shutdown_dispatches(self):
        if not self.runtime.projector_shutting_down:
            return
        # Blocking HA service calls propagate cancellation into their native
        # handlers. Discard accepted work still waiting in a native queue.
        current = asyncio.current_task()
        for task in tuple(self._projector_dispatches):
            if task is not current:
                task.cancel()

    def _tv_power_state(self):
        television = self.hass.states.get(TV)
        tv = None
        if television is not None and not television.attributes.get("restored"):
            tv = True if television.state in _ON else False if television.state == "off" else None
        if self.runtime.tv_power_pair is not None:
            tv = self.runtime.tv_power_pair.endpoints["display"].read()
        return tv

    def _power_observations(self):
        pairs = {"tv": self.runtime.tv_power_pair, "projector": self.runtime.projector_power_pair}
        revisions, intents, targets = {}, {}, {}
        for pair, backend in pairs.items():
            if backend is not None:
                revisions[pair], intents[pair], targets[pair] = backend.intent_revision, backend.last_intent, backend.target
            else:
                state = self.hass.states.get(TV if pair == "tv" else PROJECTOR)
                attrs = state.attributes if state is not None else {}
                revisions[pair] = attrs.get("power_intent_revision") or ""
                intents[pair], targets[pair] = attrs.get("last_power_intent"), attrs.get("power_sync_target")
        return revisions, intents, targets

    def _audio_mode_active(self):
        audio = self.hass.states.get("input_boolean.audio_mode")
        audio_on = audio is not None and audio.state == "on"
        if not audio_on:
            self._clear_audio = False
        return audio_on and not self._clear_audio

    async def async_start(self, _hass=None):
        self._unsubscribe = async_track_state_change_event(
            self.hass, [TV, PROJECTOR, self.runtime.projector_power_entity_id, "input_boolean.audio_mode"],
            self._state_changed,
        )
        self.refresh()
        await self.async_mirror()
        self.initialized = True
        for listener in tuple(self.listeners):
            listener()

    @callback
    def _state_changed(self, _event):
        self.refresh()

    async def async_mirror(self):
        async with self._mirror_lock:
            if self._closed:
                return
            values = [
                ("input_select.universal_remote_pair", self.mode.pair, "select_option", "option"),
                ("input_boolean.universal_remote_native_target", "on" if self.mode.native else "off", None, None),
                ("input_boolean.nav_to_tv", "on" if self.mode.native and self.mode.pair == "tv" else "off", None, None),
                ("input_text.universal_remote_focus_seconds", "60", "set_value", "value"),
                ("input_text.universal_remote_projector_until_ms", str(round(self.mode.expires_at * 1000)), "set_value", "value"),
            ]
            if self._clear_audio:
                values.append(("input_boolean.audio_mode", "off", None, None))
            for entity_id, wanted, service, field in values:
                state = self.hass.states.get(entity_id)
                if state is None or state.state == wanted:
                    continue
                domain = entity_id.split(".", 1)[0]
                service = service or "turn_" + wanted
                if self.hass.services.has_service(domain, service):
                    await self.hass.services.async_call(domain, service,
                        {"entity_id": entity_id, **({field: wanted} if field else {})}, blocking=True)

    def _wake(self, pair, revision):
        async def wake():
            self.refresh()
            if pair == "projector" and self.runtime.projector_shutting_down:
                return
            try:
                await self.hass.services.async_call(DOMAIN, "wake_if_current", {
                    "entity_id": PROJECTOR if pair == "projector" else TV,
                    "expected_revision": revision,
                }, blocking=True)
            except HomeAssistantError as err:
                _LOGGER.warning("Remote single INPUT wake was not accepted: %s", err)
            finally:
                self.refresh()
        self._spawn(wake())

    @staticmethod
    def _capture_key(button, received_ms):
        return button, round(received_ms, 3)

    def physical_press(self, name, received_at):
        """Commit trusted receiver timing before asynchronous YAML dispatch."""
        button = name.split("_", 3)[2] if name.startswith(("t9_add_", "t9_cycle_")) else name
        key = self._capture_key(button, received_at * 1000)
        if key in self._captured_routes:
            return
        self.refresh(physical_at=received_at)
        self._captured_routes[key] = self.mode.press(button, received_at)
        if len(self._captured_routes) > 256:
            del self._captured_routes[next(iter(self._captured_routes))]

    async def async_prepare(self, data):
        received = data.get("received_ms")
        if received is None:
            # Explicit compatibility calls have no physical timestamp to trust.
            self.refresh()
            route = self.mode.press(data["button"])
        else:
            # A replay, an evicted event or a caller-supplied timestamp must
            # never become another physical press or backdate a lease.
            route = self._captured_routes.get(self._capture_key(data["button"], received))
            if route is None:
                route = self.resolve({})
        await self.async_mirror()
        return route

    async def async_select(self, data):
        self.refresh()
        route = self.mode.select(data["pair"], data.get("native", False), explicit=data.get("explicit", True))
        await self.async_mirror()
        return route

    def resolve(self, data):
        self.refresh()
        return self.mode.resolve(data.get("pair"), data.get("native"))

    async def async_power(self, data):
        route = self.resolve(data)
        if route["pair"] == "projector":
            observed = self.runtime.projector_power_state
            if observed is True:
                self.runtime.note_projector_off_requested()
            # This accepted lifecycle operation owns its confirmation frames.
            # Later remote actions see the latch before this first await.
            service = "toggle" if observed is None else "turn_off" if observed else "turn_on"
            entity = PROJECTOR
        else:
            service, entity = "toggle", TV
        token = UNIVERSAL_PROJECTOR_COMMAND.set(False)
        try:
            await self.hass.services.async_call("media_player", service, {"entity_id": entity}, blocking=True)
        finally:
            UNIVERSAL_PROJECTOR_COMMAND.reset(token)
        return {"sent": True}

    async def async_dispatch(self, data):
        domain, service = data["domain"], data["service"]
        payload = dict(data.get("service_data", {}))
        entities = payload.get("entity_id", [])
        entities = [entities] if isinstance(entities, str) else entities
        kodi = self.runtime.entry.data.get("tv_kodi_entity_id", "media_player.home")
        projector_action = (data.get("pair") == "projector" or domain == "kodi"
            or (domain == "rest_command" and service.startswith("host_kodi_"))
            or any(entity in {PROJECTOR, kodi} or entity.startswith("button.projector_")
                   for entity in entities))
        self.refresh()
        if projector_action and self.runtime.projector_shutting_down:
            self.mode.resolve("projector", False)
            return {"sent": False, "blocked": "projector_shutting_down"}
        token = UNIVERSAL_PROJECTOR_COMMAND.set(projector_action)
        task = asyncio.current_task()
        if projector_action:
            self._projector_dispatches.add(task)
        try:
            result = await self.hass.services.async_call(
                domain, service, payload, blocking=True,
                return_response=data.get("return_response", False),
            )
        except asyncio.CancelledError:
            if projector_action and self.runtime.projector_shutting_down:
                return {"sent": False, "blocked": "projector_shutting_down"}
            raise
        finally:
            self._projector_dispatches.discard(task)
            UNIVERSAL_PROJECTOR_COMMAND.reset(token)
        return result if isinstance(result, dict) else {"sent": True}

    async def async_close(self):
        self._closed = True
        self._captured_routes.clear()
        self.mode.close()
        if self._unsubscribe is not None:
            self._unsubscribe()
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


def async_register_remote_services(hass):
    """Only native integrations own device transports; this boundary guards them."""
    def controller():
        value = hass.data.get(DOMAIN, {}).get("remote_controller")
        if value is None:
            raise HomeAssistantError("Universal remote mode controller is not ready")
        return value

    async def prepare(call):
        return await controller().async_prepare(call.data)
    async def select(call):
        return await controller().async_select(call.data)
    async def resolve(call):
        return controller().resolve(call.data)
    async def dispatch(call):
        return await controller().async_dispatch(call.data)
    async def power(call):
        return await controller().async_power(call.data)

    pair = {vol.Optional("pair"): vol.In(["tv", "projector"]), vol.Optional("native"): cv.boolean}
    services = {
        "remote_prepare": (prepare, {vol.Required("button"): cv.string,
            vol.Optional("received_ms"): vol.Any(None, vol.Coerce(float)), vol.Optional("expected_ms"): vol.Coerce(float),
            vol.Optional("wake_id"): cv.string}),
        "remote_select": (select, {vol.Required("pair"): vol.In(["tv", "projector"]),
            vol.Optional("native", default=False): cv.boolean, vol.Optional("explicit", default=True): cv.boolean}),
        "remote_resolve": (resolve, pair),
        "remote_power": (power, pair),
        "remote_dispatch": (dispatch, {
            vol.Required("domain"): vol.In(["kodi", "media_player", "button", "rest_command", "remote", "androidtv_remote"]),
            vol.Required("service"): cv.string, vol.Optional("service_data", default={}): dict,
            vol.Optional("pair"): vol.In(["tv", "projector"]),
            vol.Optional("return_response", default=False): cv.boolean,
        }),
    }
    for name, (handler, schema) in services.items():
        hass.services.async_register(DOMAIN, name, handler, schema=vol.Schema(schema),
                                     supports_response=SupportsResponse.OPTIONAL)
