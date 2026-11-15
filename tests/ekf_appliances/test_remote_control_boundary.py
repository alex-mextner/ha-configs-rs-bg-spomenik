"""Exercise the real controller against in-memory HA state/service boundaries."""
import asyncio
from contextvars import ContextVar
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

PREFIX = "remote_control_boundary_test"
package = ModuleType(PREFIX)
package.__path__ = [str(Path(__file__).resolve().parents[2] / "custom_components/ekf_appliances")]
runtime = ModuleType(PREFIX + ".runtime")
runtime.UNIVERSAL_PROJECTOR_COMMAND = ContextVar("test_universal_projector", default=False)
core = ModuleType("homeassistant.core")
core.callback = lambda fn: fn
core.SupportsResponse = SimpleNamespace(OPTIONAL="optional")
errors = ModuleType("homeassistant.exceptions")
errors.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
helpers = ModuleType("homeassistant.helpers")
helpers.config_validation = SimpleNamespace()
event = ModuleType("homeassistant.helpers.event")
event.async_track_state_change_event = lambda *args: lambda: None
with patch.dict(sys.modules, {PREFIX:package, PREFIX + ".runtime":runtime,
    "voluptuous":ModuleType("voluptuous"), "homeassistant.core":core,
    "homeassistant.exceptions":errors, "homeassistant.helpers":helpers,
    "homeassistant.helpers.event":event}):
    controller_module = importlib.import_module(PREFIX + ".remote_control")


class States:
    def __init__(self):
        self.values = {}
    def get(self, entity):
        return self.values.get(entity)
    def set(self, entity, value, attrs=None):
        self.values[entity] = SimpleNamespace(state=value, attributes=attrs or {})


class Services:
    def __init__(self, states):
        self.states, self.calls = states, []
        self.on_call = None
    def has_service(self, domain, service):
        return True
    async def async_call(self, domain, service, data, **kwargs):
        self.calls.append((domain, service, dict(data), runtime.UNIVERSAL_PROJECTOR_COMMAND.get()))
        if self.on_call:
            self.on_call(domain, service, data)
        entity = data.get("entity_id")
        if isinstance(entity, str):
            if domain == "input_boolean":
                self.states.set(entity, "off" if service == "turn_off" else "on")
            elif domain == "input_select":
                self.states.set(entity, data["option"])
            elif domain == "input_text":
                self.states.set(entity, data["value"])
        await asyncio.sleep(0)
        return {"ok": True} if kwargs.get("return_response") else None


class RemoteControlBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        states = States()
        states.set(controller_module.TV, "off")
        states.set(controller_module.PROJECTOR, "on")
        states.set("input_boolean.audio_mode", "off")
        self.hass = SimpleNamespace(states=states, services=Services(states),
            loop=asyncio.get_running_loop(), async_create_task=asyncio.create_task)
        self.runtime = SimpleNamespace(hass=self.hass, entry=SimpleNamespace(data={"tv_kodi_entity_id":"media_player.home"}),
            projector_power_state=True, projector_shutting_down=False, tv_power_pair=None,
            projector_power_pair=None, projector_power_entity_id="binary_sensor.projector_hdmi_connected")
        self.controller = controller_module.RemoteController(self.runtime)
        def off_requested():
            if self.runtime.projector_power_state is True:
                self.runtime.projector_shutting_down = True
                self.controller.refresh()
        self.runtime.note_projector_off_requested = off_requested
        self.controller.refresh()
    async def asyncTearDown(self):
        await self.controller.async_close()
    def device_calls(self):
        return [call for call in self.hass.services.calls if not call[0].startswith("input_")]

    async def test_guard_rechecks_after_a_projector_route_was_captured(self):
        route = self.controller.resolve({})
        self.assertEqual(route, {"pair":"projector", "native":False})
        self.runtime.note_projector_off_requested()
        result = await self.controller.async_dispatch({"pair":route["pair"], "domain":"kodi",
            "service":"call_method", "service_data":{"entity_id":"media_player.home", "method":"Input.Left"}})
        self.assertEqual(result, {"sent":False, "blocked":"projector_shutting_down"})
        self.assertEqual(self.device_calls(), [])
        self.assertEqual(self.controller.mode.target, "chromecast")

    async def test_every_projector_boundary_is_blocked_during_shutdown(self):
        self.runtime.note_projector_off_requested()
        for domain, service, data in [("button","press",{"entity_id":"button.projector_up"}),
            ("media_player","volume_up",{"entity_id":"media_player.home"}),
            ("media_player","turn_on",{"entity_id":"media_player.projector"}),
            ("rest_command","host_kodi_text_input",{"action":"add", "char":"a"}),
            ("kodi","call_method",{"entity_id":"media_player.home", "method":"Player.Open"})]:
            with self.subTest(domain=domain, service=service):
                result = await self.controller.async_dispatch({"pair":"tv", "domain":domain,
                    "service":service, "service_data":data})
                self.assertFalse(result["sent"])
        self.assertEqual(self.device_calls(), [])

    async def test_resolve_stale_projector_native_route_falls_back_without_native(self):
        self.runtime.note_projector_off_requested()
        self.assertEqual(self.controller.resolve({"pair":"projector", "native":True}),
                         {"pair":"tv", "native":False})

    async def test_accepted_off_latches_before_service_and_permits_lifecycle_confirmation(self):
        observed = []
        self.hass.services.on_call = lambda domain, service, data: observed.append(
            (self.runtime.projector_shutting_down, runtime.UNIVERSAL_PROJECTOR_COMMAND.get())) if domain == "media_player" else None
        await self.controller.async_power({"pair":"projector"})
        self.assertEqual(observed, [(True,False)])
        self.assertEqual(self.device_calls()[0][:3], ("media_player","turn_off",{"entity_id":"media_player.projector"}))
        await self.controller.async_power({"pair":"projector"})
        self.assertEqual(self.device_calls()[-1][:3], ("media_player","toggle",{"entity_id":controller_module.TV}))

    async def test_projector_dispatch_marks_transport_context_and_returns_native_response(self):
        result = await self.controller.async_dispatch({"domain":"rest_command", "service":"host_kodi_text_input",
            "service_data":{"action":"add","char":"a"}, "return_response":True})
        self.assertEqual(result, {"ok":True})
        self.assertTrue(self.device_calls()[-1][3])
        self.assertFalse(runtime.UNIVERSAL_PROJECTOR_COMMAND.get())

    async def test_audio_changes_effective_sensor_source_without_mutating_pair(self):
        self.hass.states.set("input_boolean.audio_mode", "on")
        self.controller.refresh()
        self.assertEqual(self.controller.mode.target, "speaker")
        self.hass.states.set("input_boolean.audio_mode", "off")
        self.controller.refresh()
        self.assertEqual(self.controller.mode.target, "kodi")

    async def test_shutdown_cancels_accepted_command_waiting_inside_native_service(self):
        entered, release = asyncio.Event(), asyncio.Event()
        writes = []
        async def pending_native_service(domain, service, payload, **kwargs):
            entered.set()
            await release.wait()
            writes.append((domain, service, payload))
        self.hass.services.async_call = pending_native_service
        task = asyncio.create_task(self.controller.async_dispatch({"domain":"kodi", "service":"call_method",
            "service_data":{"entity_id":"media_player.home", "method":"Input.Left"}}))
        await entered.wait()
        self.runtime.note_projector_off_requested()
        await asyncio.sleep(0)
        release.set()
        result = await task
        self.assertEqual(writes, [])
        self.assertEqual(result, {"sent":False, "blocked":"projector_shutting_down"})

    async def test_physical_preparation_is_not_replayed_by_late_yaml(self):
        self.controller.mode.select("tv")
        stamp = self.controller.mode.clock()
        self.controller.physical_press("input", stamp)
        self.controller.physical_press("input", stamp + .150)
        self.assertEqual(self.controller.mode.target, "tv")
        for received in (stamp, stamp + .150, stamp):
            await self.controller.async_prepare({"button": "input", "received_ms": received * 1000})
        self.assertEqual(self.controller.mode.target, "tv")
        self.assertIsNone(self.controller.mode.pending)

    async def test_timer_drains_ready_input_at_inclusive_1000ms_before_single_wake(self):
        now, scheduled = [1000.0], []
        def schedule(delay, action):
            scheduled.append((delay, action))
            return SimpleNamespace(cancel=lambda: None)
        self.hass.loop = SimpleNamespace(call_later=schedule)
        self.controller.mode.clock = lambda: now[0]
        self.hass.states.set(controller_module.TV, "on")
        self.runtime.projector_power_state = False
        self.controller.refresh()
        self.controller.mode.select("tv")
        self.controller.physical_press("input", 1000.0)
        timer = next(action for delay, action in scheduled if abs(delay - 1.010) < .000001)
        self.runtime.receiver = SimpleNamespace(drain_pending=lambda:
            self.controller.physical_press("input", 1001.0))
        now[0] = 1001.020
        timer()
        await asyncio.sleep(0)
        self.assertEqual(self.controller.mode.target, "tv")
        self.assertTrue(self.controller.mode.native)
        self.assertIsNone(self.controller.mode.pending)
        self.assertEqual(self.device_calls(), [])

    async def test_expiry_timer_drains_action_captured_before_deadline(self):
        now, scheduled = [1000.0], []
        def schedule(delay, action):
            scheduled.append((delay, action))
            return SimpleNamespace(cancel=lambda: None)
        self.hass.loop = SimpleNamespace(call_later=schedule)
        self.controller.mode.clock = lambda: now[0]
        self.controller.mode.select("tv", native=True)
        timer = scheduled[-1][1]
        self.runtime.receiver = SimpleNamespace(drain_pending=lambda:
            self.controller.physical_press("up", 1059.9))
        now[0] = 1060.2
        timer()
        route = await self.controller.async_prepare({"button": "up", "received_ms": 1059900.0})
        self.assertEqual(route, {"pair": "tv", "native": True})
        self.assertAlmostEqual(self.controller.mode.expires_at, 1119.9)

    async def test_unrecognized_timestamp_cannot_replay_or_backdate_a_press(self):
        self.controller.mode.select("tv", native=True)
        deadline = self.controller.mode.expires_at
        route = await self.controller.async_prepare({"button": "input", "received_ms": 1000000.0})
        self.assertEqual(route, {"pair": "tv", "native": True})
        self.assertIsNone(self.controller.mode.pending)
        self.assertEqual(self.controller.mode.expires_at, deadline)

    async def test_resolve_also_consumes_ready_activity_before_wall_clock_expiry(self):
        now = [1000.0]
        self.controller.mode.clock = lambda: now[0]
        self.controller.mode.select("tv", native=True)
        self.runtime.receiver = SimpleNamespace(drain_pending=lambda:
            self.controller.physical_press("up", 1059.9))
        now[0] = 1060.2
        self.assertEqual(self.controller.resolve({}), {"pair": "tv", "native": True})
        self.assertAlmostEqual(self.controller.mode.expires_at, 1119.9)

    async def test_route_cache_is_bounded_and_normalizes_t9_for_yaml(self):
        stamp = self.controller.mode.clock()
        self.controller.physical_press("t9_add_2_a", stamp)
        expected = self.controller.mode.expires_at
        await self.controller.async_prepare({"button": "2", "received_ms": stamp * 1000})
        self.assertEqual(self.controller.mode.expires_at, expected)
        for index in range(300):
            self.controller.physical_press("up", stamp + index / 1000)
        self.assertLessEqual(len(self.controller._captured_routes), 256)


if __name__ == "__main__":
    unittest.main()
