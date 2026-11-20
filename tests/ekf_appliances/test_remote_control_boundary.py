"""Exercise the real controller against in-memory HA state/service boundaries."""

from __future__ import annotations

import asyncio
import importlib
import sys
import unittest
from contextvars import ContextVar
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

PREFIX = "remote_control_boundary_test"
package = ModuleType(PREFIX)
package.__path__ = [
    str(Path(__file__).resolve().parents[2] / "custom_components/ekf_appliances")
]
runtime = ModuleType(PREFIX + ".runtime")
runtime.PROJECTOR_SHUTDOWN_TIMEOUT = 75.0
runtime.UNIVERSAL_PROJECTOR_COMMAND = ContextVar(
    "test_universal_projector", default=False
)
core = ModuleType("homeassistant.core")
core.callback = lambda fn: fn
core.SupportsResponse = SimpleNamespace(OPTIONAL="optional")
errors = ModuleType("homeassistant.exceptions")
errors.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
errors.Unauthorized = type("Unauthorized", (errors.HomeAssistantError,), {})
errors.UnknownUser = type("UnknownUser", (errors.Unauthorized,), {})
permissions = ModuleType("homeassistant.auth.permissions.const")
permissions.POLICY_CONTROL = "control"
helpers = ModuleType("homeassistant.helpers")
helpers.config_validation = SimpleNamespace()
event = ModuleType("homeassistant.helpers.event")
event.async_track_state_change_event = lambda *_args: lambda: None
with patch.dict(
    sys.modules,
    {
        PREFIX: package,
        PREFIX + ".runtime": runtime,
        "voluptuous": ModuleType("voluptuous"),
        "homeassistant.core": core,
        "homeassistant.exceptions": errors,
        "homeassistant.helpers": helpers,
        "homeassistant.auth.permissions.const": permissions,
        "homeassistant.helpers.event": event,
    },
):
    controller_module = importlib.import_module(PREFIX + ".remote_control")


class States:
    """Store in-memory HA entity snapshots for routing checks."""

    def __init__(self) -> None:
        self.values = {}

    def get(self, entity) -> SimpleNamespace | None:
        return self.values.get(entity)

    def set(self, entity, value, attrs=None) -> None:
        self.values[entity] = SimpleNamespace(state=value, attributes=attrs or {})


class Services:
    """Capture inert native service calls and mirror helper updates."""

    def __init__(self, states) -> None:
        self.states, self.calls = states, []
        self.on_call = None

    def has_service(self, domain, service) -> bool:
        return True

    async def async_call(
        self, domain, service, data, **kwargs: Any
    ) -> dict[str, bool] | None:
        self.calls.append(
            (domain, service, dict(data), runtime.UNIVERSAL_PROJECTOR_COMMAND.get())
        )
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
    """Verify canonical routing, physical capture and final dispatch guards."""

    async def asyncSetUp(self) -> None:
        states = States()
        states.set(controller_module.TV, "off")
        states.set(controller_module.PROJECTOR, "on")
        states.set("input_boolean.audio_mode", "off")
        self.hass = SimpleNamespace(
            states=states,
            services=Services(states),
            loop=asyncio.get_running_loop(),
            async_create_task=asyncio.create_task,
        )
        self.runtime = SimpleNamespace(
            hass=self.hass,
            entry=SimpleNamespace(data={"tv_kodi_entity_id": "media_player.home"}),
            projector_power_state=True,
            projector_shutting_down=False,
            shutdown=SimpleNamespace(requested_at=0.0),
            tv_power_pair=None,
            projector_power_pair=None,
            projector_power_entity_id="binary_sensor.projector_hdmi_connected",
        )
        self.controller = controller_module.RemoteController(self.runtime)

        def off_requested() -> None:
            if self.runtime.projector_power_state is True:
                if not self.runtime.projector_shutting_down:
                    self.runtime.shutdown.requested_at = self.controller.mode.clock()
                self.runtime.projector_shutting_down = True
                self.controller.refresh()

        self.runtime.note_projector_off_requested = off_requested
        self.controller.refresh()

    async def asyncTearDown(self) -> None:
        await self.controller.async_close()

    def device_calls(self) -> list[tuple[str, str, dict[str, Any], bool]]:
        return [
            call
            for call in self.hass.services.calls
            if not call[0].startswith("input_") and call[0] != "remote_announcements"
        ]

    def rejection_calls(self) -> list:
        return [
            call
            for call in self.hass.services.calls
            if call[:2] == ("remote_announcements", "reject")
        ]

    async def test_guard_rechecks_after_a_projector_route_was_captured(self) -> None:
        route = self.controller.resolve({})
        self.assertEqual(route, {"pair": "projector", "native": False})
        self.runtime.note_projector_off_requested()
        result = await self.controller.async_dispatch(
            {
                "pair": route["pair"],
                "domain": "kodi",
                "service": "call_method",
                "service_data": {
                    "entity_id": "media_player.home",
                    "method": "Input.Left",
                },
            }
        )
        self.assertEqual(result, {"sent": False, "blocked": "projector_shutting_down"})
        self.assertEqual(self.device_calls(), [])
        self.assertEqual(self.controller.mode.target, "chromecast")

    async def test_every_projector_boundary_is_blocked_during_shutdown(self) -> None:
        self.runtime.note_projector_off_requested()
        for domain, service, data in [
            ("button", "press", {"entity_id": "button.projector_up"}),
            ("media_player", "volume_up", {"entity_id": "media_player.home"}),
            ("media_player", "turn_on", {"entity_id": "media_player.projector"}),
            ("rest_command", "host_kodi_text_input", {"action": "add", "char": "a"}),
            (
                "kodi",
                "call_method",
                {"entity_id": "media_player.home", "method": "Player.Open"},
            ),
        ]:
            with self.subTest(domain=domain, service=service):
                result = await self.controller.async_dispatch(
                    {
                        "pair": "tv",
                        "domain": domain,
                        "service": service,
                        "service_data": data,
                    }
                )
                self.assertFalse(result["sent"])
        self.assertEqual(self.device_calls(), [])

    async def test_resolve_stale_projector_native_route_falls_back_without_native(
        self,
    ) -> None:
        self.runtime.note_projector_off_requested()
        self.assertEqual(
            self.controller.resolve({"pair": "projector", "native": True}),
            {"pair": "tv", "native": False},
        )

    async def test_accepted_off_latches_before_service_and_permits_lifecycle_confirmation(
        self,
    ) -> None:
        observed = []
        self.hass.services.on_call = lambda domain, _service, _data: (
            observed.append(
                (
                    self.runtime.projector_shutting_down,
                    runtime.UNIVERSAL_PROJECTOR_COMMAND.get(),
                )
            )
            if domain == "media_player"
            else None
        )
        await self.controller.async_power({"pair": "projector"})
        self.assertEqual(observed, [(True, False)])
        self.assertEqual(
            self.device_calls()[0][:3],
            ("media_player", "turn_off", {"entity_id": "media_player.projector"}),
        )
        result = await self.controller.async_power({"pair": "projector"})
        self.assertEqual(result, {"sent": False, "blocked": "projector_shutting_down"})
        self.assertEqual(len(self.device_calls()), 1)

    async def test_power_cannot_wake_tv_after_projector_shutdown_fallback(self) -> None:
        self.runtime.note_projector_off_requested()
        self.runtime.projector_power_pair = SimpleNamespace(
            intent_revision="projector:1",
            last_intent=False,
            target=False,
        )
        route = await self.controller.async_select({"pair": "tv"})
        result = await self.controller.async_power(route)
        self.assertFalse(result["sent"])
        self.assertEqual(self.device_calls(), [])
        self.assertEqual(len(self.rejection_calls()), 1)

    async def test_tv_navigation_cannot_wake_a_confirmed_off_fallback(self) -> None:
        self.runtime.note_projector_off_requested()
        for remote in ("remote.televizor", "remote.ue48j5500_ue48j5500"):
            with self.subTest(remote=remote):
                result = await self.controller.async_dispatch(
                    {
                        "pair": "tv",
                        "domain": "remote",
                        "service": "send_command",
                        "service_data": {
                            "entity_id": remote,
                            "command": "KEYCODE_BACK",
                        },
                    }
                )
                self.assertEqual(result, {"sent": False, "blocked": "tv_off"})
        self.assertEqual(self.device_calls(), [])

    async def test_tv_navigation_rechecks_accepted_off_before_feedback_arrives(
        self,
    ) -> None:
        self.hass.states.set(controller_module.TV, "on")
        self.runtime.tv_power_pair = SimpleNamespace(
            endpoints={"display": SimpleNamespace(read=lambda: True)},
            intent_revision="tv:1",
            last_intent=False,
            target=False,
        )
        result = await self.controller.async_dispatch(
            {
                "pair": "tv",
                "domain": "remote",
                "service": "send_command",
                "service_data": {
                    "entity_id": "remote.televizor",
                    "command": "KEYCODE_BACK",
                },
            }
        )
        self.assertEqual(result, {"sent": False, "blocked": "tv_power_transition"})
        self.assertEqual(self.device_calls(), [])

    async def test_tv_navigation_is_rejected_during_an_accepted_startup(
        self,
    ) -> None:
        self.runtime.tv_power_pair = SimpleNamespace(
            endpoints={"display": SimpleNamespace(read=lambda: False)},
            intent_revision="tv:1",
            last_intent=True,
            target=True,
        )
        result = await self.controller.async_dispatch(
            {
                "pair": "tv",
                "domain": "remote",
                "service": "send_command",
                "service_data": {
                    "entity_id": "remote.televizor",
                    "command": "KEYCODE_BACK",
                },
            }
        )
        self.assertEqual(result, {"sent": False, "blocked": "tv_power_transition"})
        self.assertEqual(self.device_calls(), [])
        self.assertEqual(len(self.rejection_calls()), 1)

    async def test_tv_navigation_still_dispatches_to_a_confirmed_on_display(
        self,
    ) -> None:
        self.hass.states.set(controller_module.TV, "on")
        await self.controller.async_dispatch(
            {
                "pair": "tv",
                "domain": "remote",
                "service": "send_command",
                "service_data": {
                    "entity_id": "remote.televizor",
                    "command": "KEYCODE_BACK",
                },
            }
        )
        self.assertEqual(
            self.device_calls()[0][:3],
            (
                "remote",
                "send_command",
                {"entity_id": "remote.televizor", "command": "KEYCODE_BACK"},
            ),
        )

    async def test_projector_dispatch_marks_transport_context_and_returns_native_response(
        self,
    ) -> None:
        result = await self.controller.async_dispatch(
            {
                "domain": "rest_command",
                "service": "host_kodi_text_input",
                "service_data": {"action": "add", "char": "a"},
                "return_response": True,
            }
        )
        self.assertEqual(result, {"ok": True})
        self.assertTrue(self.device_calls()[-1][3])
        self.assertFalse(runtime.UNIVERSAL_PROJECTOR_COMMAND.get())

    async def test_power_rejects_each_pair_transition_without_changing_intent(
        self,
    ) -> None:
        for pair in ("tv", "projector"):
            for target in (True, False):
                with self.subTest(pair=pair, target=target):
                    backend = SimpleNamespace(
                        endpoints={
                            "display": SimpleNamespace(
                                read=lambda target=target: not target
                            )
                        },
                        intent_revision="pending:1",
                        last_intent=target,
                        target=target,
                    )
                    setattr(self.runtime, pair + "_power_pair", backend)
                    result = await self.controller.async_power({"pair": pair})
                    self.assertFalse(result["sent"])
                    self.assertIs(backend.target, target)
                    self.assertEqual(self.device_calls(), [])
                    setattr(self.runtime, pair + "_power_pair", None)
        self.assertEqual(len(self.rejection_calls()), 4)

    async def test_overlapping_power_cannot_enter_before_first_service_confirms(
        self,
    ) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.hass.services.async_call

        async def delayed(domain, service, data, **kwargs: Any) -> Any:
            await original(domain, service, data, **kwargs)
            if domain == "media_player":
                entered.set()
                await release.wait()

        self.hass.services.async_call = delayed
        first = asyncio.create_task(self.controller.async_power({"pair": "tv"}))
        await entered.wait()
        try:
            second = await asyncio.wait_for(
                self.controller.async_power({"pair": "tv"}), 0.1
            )
            self.assertFalse(second["sent"])
            self.assertEqual(len(self.device_calls()), 1)
        finally:
            release.set()
            await first
        self.assertEqual(
            await self.controller.async_power({"pair": "tv"}), {"sent": True}
        )
        self.assertEqual(len(self.device_calls()), 2)

    async def test_cancelled_caller_does_not_release_running_power_admission(
        self,
    ) -> None:
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = self.hass.services.async_call

        async def delayed(domain, service, data, **kwargs: Any) -> Any:
            await original(domain, service, data, **kwargs)
            if domain == "media_player":
                entered.set()
                await release.wait()
                finished.set()

        self.hass.services.async_call = delayed
        first = asyncio.create_task(self.controller.async_power({"pair": "tv"}))
        await entered.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        try:
            result = await asyncio.wait_for(
                self.controller.async_power({"pair": "tv"}), 0.1
            )
            self.assertFalse(result["sent"])
            self.assertEqual(len(self.device_calls()), 1)
        finally:
            release.set()
        await finished.wait()
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(
            await self.controller.async_power({"pair": "tv"}), {"sent": True}
        )

    async def test_failed_power_releases_admission_without_hiding_the_error(
        self,
    ) -> None:
        original = self.hass.services.async_call

        async def failing(domain, service, data, **kwargs: Any) -> Any:
            if domain == "media_player":
                message = "transmitter unavailable"
                raise errors.HomeAssistantError(message)
            return await original(domain, service, data, **kwargs)

        self.hass.services.async_call = failing
        with self.assertRaisesRegex(
            errors.HomeAssistantError, "transmitter unavailable"
        ):
            await self.controller.async_power({"pair": "tv"})
        self.assertEqual(len(self.rejection_calls()), 1)
        self.hass.services.async_call = original
        self.assertEqual(
            await self.controller.async_power({"pair": "tv"}), {"sent": True}
        )

    async def test_rejected_hold_and_repeats_keep_receiver_press_identity(self) -> None:
        self.controller.mode.select("tv")
        stamp = self.controller.mode.clock()
        for button, offset, identity in [
            ("ok", 0.0, "Odroid:ok:10.000000000"),
            ("ok_hold", 0.22, "Odroid:ok:10.000000000"),
            ("up", 1.0, "Odroid:up:11.000000000"),
            ("up", 1.22, "Odroid:up:11.000000000"),
            ("up", 1.30, "Odroid:up:11.300000000"),
        ]:
            self.controller.physical_press(button, stamp + offset, press_id=identity)
            route = await self.controller.async_prepare(
                {"button": button, "received_ms": (stamp + offset) * 1000}
            )
            self.assertEqual(route["blocked"], "tv_off")
        self.assertEqual(
            [call[2]["press_id"] for call in self.rejection_calls()],
            [
                "Odroid:ok:10.000000000",
                "Odroid:ok:10.000000000",
                "Odroid:up:11.000000000",
                "Odroid:up:11.000000000",
                "Odroid:up:11.300000000",
            ],
        )
        self.assertEqual(self.device_calls(), [])

    async def test_downstream_rejections_keep_physical_identity_between_contexts(
        self,
    ) -> None:
        stamp = self.controller.mode.clock()
        for index, identity in enumerate(("Odroid:held", "Odroid:held", "Odroid:tap")):
            received = stamp + index / 10
            self.controller.physical_press("channel_up", received, press_id=identity)
            token = controller_module._SERVICE_CONTEXT.set(
                SimpleNamespace(id=f"automation-{index}", user_id=None)
            )
            try:
                route = await self.controller.async_prepare(
                    {"button": "channel_up", "received_ms": received * 1000}
                )
                self.assertNotIn("blocked", route)
                await self.controller.async_reject("ac_setpoint_limit")
            finally:
                controller_module._SERVICE_CONTEXT.reset(token)
        self.assertEqual(
            [call[2]["press_id"] for call in self.rejection_calls()],
            ["Odroid:held", "Odroid:held", "Odroid:tap"],
        )

    async def test_service_failure_keeps_physical_identity_and_original_error(
        self,
    ) -> None:
        self.hass.states.set(controller_module.TV, "on")
        self.controller.mode.select("tv")
        original = self.hass.services.async_call

        async def failed(domain, service, data, **kwargs: Any) -> Any:
            if domain == "remote":
                message = "native volume unavailable"
                raise errors.HomeAssistantError(message)
            return await original(domain, service, data, **kwargs)

        self.hass.services.async_call = failed
        stamp = self.controller.mode.clock()
        for index in range(2):
            received = stamp + index / 5
            self.controller.physical_press(
                "volume_up", received, press_id="Odroid:held-volume"
            )
            token = controller_module._SERVICE_CONTEXT.set(
                SimpleNamespace(id=f"volume-{index}", user_id=None)
            )
            try:
                await self.controller.async_prepare(
                    {"button": "volume_up", "received_ms": received * 1000}
                )
                with self.assertRaisesRegex(
                    errors.HomeAssistantError, "native volume unavailable"
                ):
                    await self.controller.async_dispatch(
                        {
                            "pair": "tv",
                            "domain": "remote",
                            "service": "send_command",
                            "service_data": {
                                "entity_id": "remote.televizor",
                                "command": "KEYCODE_VOLUME_UP",
                            },
                        }
                    )
            finally:
                controller_module._SERVICE_CONTEXT.reset(token)
        self.assertEqual(
            [call[2] for call in self.rejection_calls()],
            [
                {"reason": "command_failed", "press_id": "Odroid:held-volume"},
                {"reason": "command_failed", "press_id": "Odroid:held-volume"},
            ],
        )

    async def test_disney_capture_rejects_projector_startup_before_fallback(
        self,
    ) -> None:
        self.runtime.projector_power_state = False
        self.hass.states.set(
            controller_module.PROJECTOR, "off", {"power_sync_target": True}
        )
        self.controller.mode.select("tv")
        stamp = self.controller.mode.clock()
        for index, button in enumerate(("disney", "disney_hold")):
            received = stamp + index / 5
            self.controller.physical_press(
                button, received, press_id="Odroid:held-disney"
            )
            route = await self.controller.async_prepare(
                {"button": button, "received_ms": received * 1000}
            )
            self.assertEqual(route["blocked"], "projector_power_transition")
        self.assertEqual(self.device_calls(), [])
        self.assertEqual(
            [call[2]["press_id"] for call in self.rejection_calls()],
            ["Odroid:held-disney", "Odroid:held-disney"],
        )

    async def test_native_shutdown_window_blocks_fallback_then_expires(
        self,
    ) -> None:
        stamp = self.controller.mode.clock()
        self.controller.mode.clock = lambda: stamp
        self.runtime.note_projector_off_requested()
        self.assertEqual(self.controller.resolve({})["pair"], "tv")
        self.assertEqual(
            await self.controller.async_power({"pair": "tv"}),
            {"sent": False, "blocked": "projector_shutting_down"},
        )
        self.assertEqual(self.device_calls(), [])
        self.controller.physical_press("power", stamp + 0.01)
        self.controller.mode.clock = lambda: (
            stamp + runtime.PROJECTOR_SHUTDOWN_TIMEOUT + 0.1
        )
        captured = await self.controller.async_prepare(
            {"button": "power", "received_ms": (stamp + 0.01) * 1000}
        )
        self.assertEqual(captured["blocked"], "projector_shutting_down")
        self.assertEqual(
            await self.controller.async_power({"pair": "tv"}), {"sent": True}
        )
        self.assertTrue(self.runtime.projector_shutting_down)
        self.assertFalse(
            (await self.controller.async_power({"pair": "projector"}))["sent"]
        )

    async def test_rejected_raw_toggle_cannot_settle_later_native_shutdown(
        self,
    ) -> None:
        original = self.hass.services.async_call

        async def rejected_toggle(domain, service, data, **kwargs: Any) -> Any:
            if (
                domain == "media_player"
                and data.get("entity_id") == controller_module.PROJECTOR
            ):
                # A real OEM OFF arrives after admission. The native service
                # rejects this remote action before transmitting any frame.
                self.runtime.note_projector_off_requested()
                self.assertTrue(runtime.UNIVERSAL_PROJECTOR_COMMAND.get())
                message = "native projector action rejected before dispatch"
                raise errors.HomeAssistantError(message)
            return await original(domain, service, data, **kwargs)

        self.hass.services.async_call = rejected_toggle
        with self.assertRaisesRegex(errors.HomeAssistantError, "before dispatch"):
            await self.controller.async_dispatch(
                {
                    "pair": "projector",
                    "domain": "media_player",
                    "service": "toggle",
                    "service_data": {"entity_id": controller_module.PROJECTOR},
                }
            )
        self.assertEqual(self.device_calls(), [])
        self.assertEqual(self.controller._power_operations, {})
        self.assertTrue(self.runtime.projector_shutting_down)
        self.assertEqual(
            await self.controller.async_power({"pair": "tv"}),
            {"sent": False, "blocked": "projector_shutting_down"},
        )

    async def test_tokenless_prepare_neither_reserves_nor_adopts_physical_power(
        self,
    ) -> None:
        self.runtime.projector_power_state = False
        route = await self.controller.async_prepare({"button": "power"})
        self.assertNotIn("power_request_id", route)
        self.assertIsNone(self.controller._pending_power)
        self.assertEqual(await self.controller.async_power(route), {"sent": True})
        stamp = self.controller.mode.clock()
        self.controller.physical_press("power", stamp)
        pending = self.controller._pending_power
        blocked = await self.controller.async_prepare({"button": "power"})
        self.assertEqual(blocked["blocked"], "tv_power_transition")
        self.assertFalse((await self.controller.async_power({"pair": "tv"}))["sent"])
        self.assertEqual(self.controller._pending_power, pending)
        captured = await self.controller.async_prepare(
            {"button": "power", "received_ms": stamp * 1000}
        )
        self.assertEqual(await self.controller.async_power(captured), {"sent": True})

    async def test_context_bridge_is_bounded_and_keeps_explicit_identity(self) -> None:
        stamp = self.controller.mode.clock()
        with patch.object(controller_module, "_CAPTURED_ROUTE_LIMIT", 2):
            for index in range(3):
                received = stamp + index / 10
                self.controller.physical_press(
                    "channel_up", received, press_id=f"Odroid:{index}"
                )
                token = controller_module._SERVICE_CONTEXT.set(
                    SimpleNamespace(id=f"context-{index}", user_id=None)
                )
                try:
                    await self.controller.async_prepare(
                        {"button": "channel_up", "received_ms": received * 1000}
                    )
                finally:
                    controller_module._SERVICE_CONTEXT.reset(token)
        self.assertEqual(len(self.controller._context_press_ids), 2)
        for context_id, explicit in (("context-0", None), ("context-2", "explicit")):
            token = controller_module._SERVICE_CONTEXT.set(
                SimpleNamespace(id=context_id, user_id=None)
            )
            try:
                await self.controller.async_reject(
                    "ac_setpoint_limit", press_id=explicit
                )
            finally:
                controller_module._SERVICE_CONTEXT.reset(token)
        self.assertEqual(
            [call[2]["press_id"] for call in self.rejection_calls()],
            ["context-0", "explicit"],
        )
        await self.controller.async_close()
        self.assertEqual(self.controller._context_press_ids, {})

    async def test_single_input_timeout_reports_one_rejection(self) -> None:
        original = self.hass.services.async_call

        async def waiting(domain, service, data, **kwargs: Any) -> Any:
            if (domain, service) == ("ekf_appliances", "wake_if_current"):
                await asyncio.Event().wait()
            return await original(domain, service, data, **kwargs)

        self.hass.services.async_call = waiting
        with patch.object(controller_module, "_POWER_OPERATION_TIMEOUT", 0.01):
            self.controller._wake("tv", "tv:0")
            await asyncio.gather(*tuple(self.controller._tasks))
        self.assertEqual(
            [call[2]["reason"] for call in self.rejection_calls()],
            ["power_confirmation_timeout"],
        )
        self.assertEqual(self.controller._power_operations, {})

    async def test_shutdown_refresh_during_rejection_preserves_blocked_response(
        self,
    ) -> None:
        entered, cue_entered, release_cue = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        original = self.hass.services.async_call

        async def waiting(domain, service, data, **kwargs: Any) -> Any:
            if domain == "kodi":
                entered.set()
                await asyncio.Event().wait()
            if domain == "remote_announcements":
                cue_entered.set()
                await release_cue.wait()
            return await original(domain, service, data, **kwargs)

        self.hass.services.async_call = waiting
        task = asyncio.create_task(
            self.controller.async_dispatch(
                {
                    "pair": "projector",
                    "domain": "kodi",
                    "service": "call_method",
                    "service_data": {
                        "entity_id": "media_player.home",
                        "method": "Input.Left",
                    },
                }
            )
        )
        await entered.wait()
        self.runtime.note_projector_off_requested()
        await cue_entered.wait()
        self.controller.refresh()
        release_cue.set()
        self.assertEqual(
            await task, {"sent": False, "blocked": "projector_shutting_down"}
        )
        self.assertEqual(len(self.rejection_calls()), 1)
        self.assertEqual(self.device_calls(), [])

    async def test_physical_power_is_reserved_before_yaml_and_rejection_is_not_replayed(
        self,
    ) -> None:
        self.controller.mode.select("tv")
        stamp = self.controller.mode.clock()
        self.controller.physical_press("power", stamp)
        self.controller.physical_press("power", stamp + 0.3)
        first = await self.controller.async_prepare(
            {"button": "power", "received_ms": stamp * 1000}
        )
        self.assertIn("power_request_id", first)
        await self.controller.async_power(first)
        second = await self.controller.async_prepare(
            {"button": "power", "received_ms": (stamp + 0.3) * 1000}
        )
        self.assertIn("blocked", second)
        self.assertEqual(len(self.device_calls()), 1)
        self.assertEqual(len(self.rejection_calls()), 1)

    async def test_press_rejected_while_busy_stays_rejected_after_confirmation(
        self,
    ) -> None:
        backend = SimpleNamespace(
            endpoints={"display": SimpleNamespace(read=lambda: False)},
            intent_revision="tv:1",
            last_intent=True,
            target=True,
        )
        self.runtime.tv_power_pair = backend
        self.controller.mode.select("tv")
        stamp = self.controller.mode.clock()
        self.controller.physical_press("up", stamp)
        backend.target = None
        self.hass.states.set(controller_module.TV, "on")
        route = await self.controller.async_prepare(
            {"button": "up", "received_ms": stamp * 1000}
        )
        self.assertEqual(route["blocked"], "tv_power_transition")
        self.assertEqual(self.device_calls(), [])
        self.assertEqual(len(self.rejection_calls()), 1)

    async def test_explicit_source_may_start_idle_tv_but_power_waits_for_both_pairs(
        self,
    ) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        self.runtime.note_projector_off_requested()
        original = self.hass.services.async_call

        async def delayed(domain, service, data, **kwargs: Any) -> Any:
            await original(domain, service, data, **kwargs)
            if domain == "media_player":
                entered.set()
                await release.wait()

        self.hass.services.async_call = delayed
        explicit = asyncio.create_task(
            self.controller.async_dispatch(
                {
                    "pair": "tv",
                    "domain": "media_player",
                    "service": "turn_on",
                    "service_data": {"entity_id": controller_module.TV},
                }
            )
        )
        await entered.wait()
        self.runtime.projector_shutting_down = False
        self.runtime.projector_power_state = False
        try:
            result = await asyncio.wait_for(
                self.controller.async_power({"pair": "projector"}), 0.1
            )
            self.assertFalse(result["sent"])
            self.assertEqual(len(self.device_calls()), 1)
        finally:
            release.set()
            await explicit

    async def test_projector_off_and_native_text_off_are_rejected_without_waking(
        self,
    ) -> None:
        self.runtime.projector_power_state = False
        for data in (
            {
                "pair": "projector",
                "domain": "kodi",
                "service": "call_method",
                "service_data": {
                    "entity_id": "media_player.home",
                    "method": "Input.Left",
                },
            },
            {
                "pair": "tv",
                "domain": "androidtv_remote",
                "service": "send_text",
                "service_data": {
                    "entity_id": "remote.televizor",
                    "operation": "insert",
                    "text": "a",
                },
            },
        ):
            result = await self.controller.async_dispatch(data)
            self.assertFalse(result["sent"])
        self.assertEqual(self.device_calls(), [])
        self.assertEqual(len(self.rejection_calls()), 2)

    async def test_denied_compatibility_power_cannot_reserve_the_button(self) -> None:
        async def denied(_entity) -> None:
            message = "not allowed"
            raise errors.Unauthorized(message)

        original = self.controller._async_check_control
        self.controller._async_check_control = denied
        with self.assertRaises(errors.Unauthorized):
            await self.controller.async_prepare({"button": "power"})
        self.controller._async_check_control = original
        self.assertEqual(
            await self.controller.async_power({"pair": "tv"}), {"sent": True}
        )

    async def test_accepted_projector_on_retains_final_native_shutdown_guard(
        self,
    ) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        writes = []
        original = self.hass.services.async_call

        async def queued(domain, service, data, **kwargs: Any) -> Any:
            if domain == "media_player":
                entered.set()
                await release.wait()
                if (
                    runtime.UNIVERSAL_PROJECTOR_COMMAND.get()
                    and self.runtime.projector_shutting_down
                ):
                    message = "Projector is shutting down; remote command was discarded"
                    raise errors.HomeAssistantError(message)
                writes.append(service)
            return await original(domain, service, data, **kwargs)

        self.runtime.projector_power_state = False
        self.hass.services.async_call = queued
        first = asyncio.create_task(
            self.controller.async_dispatch(
                {
                    "pair": "projector",
                    "domain": "media_player",
                    "service": "turn_on",
                    "service_data": {"entity_id": controller_module.PROJECTOR},
                }
            )
        )
        await entered.wait()
        self.runtime.projector_shutting_down = True
        self.controller.refresh()
        release.set()
        with self.assertRaisesRegex(errors.HomeAssistantError, "discarded"):
            await first
        self.assertEqual(writes, [])
        self.assertEqual(self.controller._power_operations, {})

    async def test_cancelled_before_owned_service_starts_releases_admission(
        self,
    ) -> None:
        # HA shutdown may cancel a created task before its first event-loop turn.
        def cancel_before_start(coroutine) -> asyncio.Task[Any]:
            task = asyncio.create_task(coroutine)
            task.cancel()
            return task

        self.hass.async_create_task = cancel_before_start
        with self.assertRaises(asyncio.CancelledError):
            await self.controller.async_power({"pair": "tv"})
        self.assertEqual(self.device_calls(), [])
        self.assertEqual(self.controller._power_operations, {})

    async def test_audio_bluetooth_key_stays_available_during_tv_startup(self) -> None:
        self.runtime.tv_power_pair = SimpleNamespace(
            endpoints={"display": SimpleNamespace(read=lambda: False)},
            intent_revision="tv:1",
            last_intent=True,
            target=True,
        )
        self.hass.states.set("input_boolean.audio_mode", "on")
        route = await self.controller.async_prepare({"button": "apple_tv"})
        self.assertNotIn("blocked", route)
        self.assertEqual(self.rejection_calls(), [])

    async def test_unpaired_speaker_command_does_not_depend_on_display_power(
        self,
    ) -> None:
        result = await self.controller.async_dispatch(
            {
                "domain": "media_player",
                "service": "volume_up",
                "service_data": {"entity_id": "media_player.living_room_speaker"},
            }
        )
        self.assertEqual(result, {"sent": True})
        self.assertEqual(
            self.device_calls()[0][2]["entity_id"], "media_player.living_room_speaker"
        )

    async def test_unknown_display_rejects_navigation_without_inventing_off(
        self,
    ) -> None:
        self.hass.states.set(controller_module.TV, "unavailable")
        result = await self.controller.async_dispatch(
            {
                "pair": "tv",
                "domain": "remote",
                "service": "send_command",
                "service_data": {
                    "entity_id": "remote.televizor",
                    "command": "KEYCODE_BACK",
                },
            }
        )
        self.assertEqual(result, {"sent": False, "blocked": "tv_power_unknown"})
        self.assertEqual(self.device_calls(), [])

    async def test_timed_out_projector_releases_global_power_but_retains_shutdown_safety(
        self,
    ) -> None:
        entered = asyncio.Event()
        original = self.hass.services.async_call

        async def delayed(domain, service, data, **kwargs: Any) -> Any:
            if (
                domain == "media_player"
                and data.get("entity_id") == controller_module.PROJECTOR
            ):
                entered.set()
                await asyncio.Event().wait()
            return await original(domain, service, data, **kwargs)

        self.hass.services.async_call = delayed
        with patch.object(controller_module, "_POWER_OPERATION_TIMEOUT", 0.02):
            first = asyncio.create_task(
                self.controller.async_power({"pair": "projector"})
            )
            await entered.wait()
            result = await self.controller.async_power({"pair": "tv"})
            self.assertFalse(result["sent"])
            with self.assertRaisesRegex(errors.HomeAssistantError, "timed out"):
                await first
        self.assertTrue(self.runtime.projector_shutting_down)
        self.assertFalse(
            (await self.controller.async_power({"pair": "projector"}))["sent"]
        )
        self.assertEqual(
            await self.controller.async_power({"pair": "tv"}), {"sent": True}
        )

    async def test_explicit_projector_off_owns_its_confirmation_during_later_refresh(
        self,
    ) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.hass.services.async_call

        async def delayed(domain, service, data, **kwargs: Any) -> Any:
            await original(domain, service, data, **kwargs)
            if domain == "media_player":
                self.runtime.note_projector_off_requested()
                entered.set()
                await release.wait()

        self.hass.services.async_call = delayed
        first = asyncio.create_task(
            self.controller.async_dispatch(
                {
                    "pair": "projector",
                    "domain": "media_player",
                    "service": "turn_off",
                    "service_data": {"entity_id": controller_module.PROJECTOR},
                }
            )
        )
        await entered.wait()
        self.controller.refresh()
        release.set()
        self.assertEqual(await first, {"sent": True})
        self.assertEqual(len(self.rejection_calls()), 0)

    async def test_audio_changes_effective_sensor_source_without_mutating_pair(
        self,
    ) -> None:
        self.hass.states.set("input_boolean.audio_mode", "on")
        self.controller.refresh()
        self.assertEqual(self.controller.mode.target, "speaker")
        self.hass.states.set("input_boolean.audio_mode", "off")
        self.controller.refresh()
        self.assertEqual(self.controller.mode.target, "kodi")

    async def test_shutdown_cancels_accepted_command_waiting_inside_native_service(
        self,
    ) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        writes = []

        async def pending_native_service(
            domain, service, payload, **_kwargs: Any
        ) -> None:
            entered.set()
            await release.wait()
            writes.append((domain, service, payload))

        self.hass.services.async_call = pending_native_service
        task = asyncio.create_task(
            self.controller.async_dispatch(
                {
                    "domain": "kodi",
                    "service": "call_method",
                    "service_data": {
                        "entity_id": "media_player.home",
                        "method": "Input.Left",
                    },
                }
            )
        )
        await entered.wait()
        self.runtime.note_projector_off_requested()
        await asyncio.sleep(0)
        release.set()
        result = await task
        self.assertEqual(
            [entry for entry in writes if entry[0] != "remote_announcements"], []
        )
        self.assertEqual(result, {"sent": False, "blocked": "projector_shutting_down"})

    async def test_physical_preparation_is_not_replayed_by_late_yaml(self) -> None:
        self.controller.mode.select("tv")
        stamp = self.controller.mode.clock()
        self.controller.physical_press("input", stamp)
        self.controller.physical_press("input", stamp + 0.150)
        self.assertEqual(self.controller.mode.target, "tv")
        for received in (stamp, stamp + 0.150, stamp):
            await self.controller.async_prepare(
                {"button": "input", "received_ms": received * 1000}
            )
        self.assertEqual(self.controller.mode.target, "tv")
        self.assertIsNone(self.controller.mode.pending)

    async def test_timer_drains_ready_input_at_inclusive_1000ms_before_single_wake(
        self,
    ) -> None:
        now, scheduled = [1000.0], []

        def schedule(delay, action) -> SimpleNamespace:
            scheduled.append((delay, action))
            return SimpleNamespace(cancel=lambda: None)

        self.hass.loop = SimpleNamespace(call_later=schedule)
        self.controller.mode.clock = lambda: now[0]
        self.hass.states.set(controller_module.TV, "on")
        self.runtime.projector_power_state = False
        self.controller.refresh()
        self.controller.mode.select("tv")
        self.controller.physical_press("input", 1000.0)
        timer = next(
            action for delay, action in scheduled if abs(delay - 1.010) < 0.000001
        )
        self.runtime.receiver = SimpleNamespace(
            drain_pending=lambda: self.controller.physical_press("input", 1001.0)
        )
        now[0] = 1001.020
        timer()
        await asyncio.sleep(0)
        self.assertEqual(self.controller.mode.target, "tv")
        self.assertTrue(self.controller.mode.native)
        self.assertIsNone(self.controller.mode.pending)
        self.assertEqual(self.device_calls(), [])

    async def test_expiry_timer_drains_action_captured_before_deadline(self) -> None:
        now, scheduled = [1000.0], []

        def schedule(delay, action) -> SimpleNamespace:
            scheduled.append((delay, action))
            return SimpleNamespace(cancel=lambda: None)

        self.hass.loop = SimpleNamespace(call_later=schedule)
        self.controller.mode.clock = lambda: now[0]
        self.hass.states.set(controller_module.TV, "on")
        self.controller.mode.select("tv", native=True)
        timer = scheduled[-1][1]
        self.runtime.receiver = SimpleNamespace(
            drain_pending=lambda: self.controller.physical_press("up", 1059.9)
        )
        now[0] = 1060.2
        timer()
        route = await self.controller.async_prepare(
            {"button": "up", "received_ms": 1059900.0}
        )
        self.assertEqual(route, {"pair": "tv", "native": True})
        self.assertAlmostEqual(self.controller.mode.expires_at, 1119.9)

    async def test_unrecognized_timestamp_cannot_replay_or_backdate_a_press(
        self,
    ) -> None:
        self.controller.mode.select("tv", native=True)
        deadline = self.controller.mode.expires_at
        route = await self.controller.async_prepare(
            {"button": "input", "received_ms": 1000000.0}
        )
        self.assertEqual(route, {"pair": "tv", "native": True})
        self.assertIsNone(self.controller.mode.pending)
        self.assertEqual(self.controller.mode.expires_at, deadline)

    async def test_resolve_also_consumes_ready_activity_before_wall_clock_expiry(
        self,
    ) -> None:
        now = [1000.0]
        self.controller.mode.clock = lambda: now[0]
        self.controller.mode.select("tv", native=True)
        self.runtime.receiver = SimpleNamespace(
            drain_pending=lambda: self.controller.physical_press("up", 1059.9)
        )
        now[0] = 1060.2
        self.assertEqual(self.controller.resolve({}), {"pair": "tv", "native": True})
        self.assertAlmostEqual(self.controller.mode.expires_at, 1119.9)

    async def test_route_cache_is_bounded_and_normalizes_t9_for_yaml(self) -> None:
        stamp = self.controller.mode.clock()
        self.controller.physical_press("t9_add_2_a", stamp)
        expected = self.controller.mode.expires_at
        await self.controller.async_prepare(
            {"button": "2", "received_ms": stamp * 1000}
        )
        self.assertEqual(self.controller.mode.expires_at, expected)
        for index in range(300):
            self.controller.physical_press("up", stamp + index / 1000)
        self.assertLessEqual(len(self.controller._captured_routes), 256)
        self.assertEqual(
            self.controller._captured_press_ids.keys(),
            self.controller._captured_routes.keys(),
        )


if __name__ == "__main__":
    unittest.main()
