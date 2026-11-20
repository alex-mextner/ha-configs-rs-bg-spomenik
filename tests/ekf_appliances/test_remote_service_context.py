"""Defensive service-boundary checks with real HA calls and inert endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import ServiceResponse

import asyncio
import tempfile
import time
import unittest
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.auth import auth_manager_from_config
from homeassistant.auth.const import GROUP_ID_READ_ONLY, GROUP_ID_USER
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.script import Script
from homeassistant.helpers.service import entity_service_call

from custom_components.ekf_appliances.const import DOMAIN
from custom_components.ekf_appliances.protocol import encode_pulses, projector_pulses
from custom_components.ekf_appliances.receiver import ReceiverRouter
from custom_components.ekf_appliances.remote_control import (
    PROJECTOR,
    TV,
    RemoteController,
    async_register_remote_services,
)
from custom_components.ekf_appliances.runtime import ApplianceRuntime


class PermissionCheckedRemote(Entity):
    """Expose an inert write behind the real HA entity-service permission check."""

    _attr_should_poll = False

    def __init__(self, hass, runtime, calls) -> None:
        self.hass = hass
        self.entity_id = runtime.remote_entity_id
        self.runtime = runtime
        self.calls = calls

    async def async_send_command(self, command) -> None:
        self.calls.append((command, self.runtime.projector_shutting_down))


class ShutdownObserverPermissionTests(unittest.IsolatedAsyncioTestCase):
    """Check observer ordering with real HA users, scripts and permission policy."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        self.hass.auth = await auth_manager_from_config(self.hass, [], [])
        await self.hass.auth.async_create_user("Owner")
        self.denied = await self.hass.auth.async_create_user(
            "Read only", group_ids=[GROUP_ID_READ_ONLY]
        )
        self.allowed = await self.hass.auth.async_create_user(
            "Controller", group_ids=[GROUP_ID_USER]
        )
        self.calls = []
        self.runtime = ApplianceRuntime(
            self.hass,
            SimpleNamespace(
                entry_id="observer-permission",
                data={
                    "remote_entity_id": "remote.ekf",
                    "temperature_entity_id": "sensor.room_temperature",
                    "projector_power_entity_id": "binary_sensor.projector_hdmi",
                    "projector_off_script": "script.projector_turn_off",
                },
            ),
        )
        self.hass.states.async_set(self.runtime.projector_power_entity_id, "on")
        await self.runtime.async_setup_shutdown()
        self.receiver = ReceiverRouter(self.runtime)
        remote = PermissionCheckedRemote(self.hass, self.runtime, self.calls)
        self.hass.states.async_set(remote.entity_id, "on")
        self.hass.services.async_register(
            "remote",
            "send_command",
            partial(
                entity_service_call,
                self.hass,
                {remote.entity_id: remote},
                "async_send_command",
            ),
        )
        self.command = encode_pulses(projector_pulses("power"))
        self.script = Script(
            self.hass,
            cv.SCRIPT_SCHEMA(
                [
                    {
                        "action": "remote.send_command",
                        "target": {"entity_id": remote.entity_id},
                        "data": {"command": self.command},
                    }
                ]
            ),
            "Projector off",
            "script",
        )

        async def run_script(call) -> None:
            await self.script.async_run({}, context=call.context)

        self.hass.services.async_register("script", "projector_turn_off", run_script)

    async def asyncTearDown(self) -> None:
        await self.receiver.async_close()
        await self.runtime.async_close_shutdown()
        await self.script.async_stop()
        await self.hass.async_block_till_done()
        await self.hass.async_stop()
        self.tmp.cleanup()

    async def send(self, context) -> None:
        await self.hass.services.async_call(
            "remote",
            "send_command",
            {"entity_id": self.runtime.remote_entity_id, "command": self.command},
            context=context,
            blocking=True,
        )

    async def test_denied_raw_ir_does_not_persist_shutdown_intent(self) -> None:
        with self.assertRaises(Unauthorized):
            await self.send(Context(user_id=self.denied.id))
        await self.hass.async_block_till_done()
        self.assertFalse(self.runtime.projector_shutting_down)
        self.assertIsNone(self.runtime._shutdown_save_task)
        self.assertEqual(self.calls, [])

    async def test_denied_off_script_does_not_persist_shutdown_intent(self) -> None:
        with self.assertRaises(Unauthorized):
            await self.hass.services.async_call(
                "script",
                "projector_turn_off",
                {},
                context=Context(user_id=self.denied.id),
                blocking=True,
            )
        await self.hass.async_block_till_done()
        self.assertFalse(self.runtime.projector_shutting_down)
        self.assertIsNone(self.runtime._shutdown_save_task)
        self.assertEqual(self.calls, [])

    async def test_allowed_raw_ir_latches_before_inert_write(self) -> None:
        self.assertFalse(self.allowed.is_admin)
        await self.send(Context(user_id=self.allowed.id))
        self.assertEqual(self.calls, [(self.command, True)])
        await self.hass.async_block_till_done()
        self.assertIs(
            (await self.runtime._shutdown_store.async_load())["active"], expr2=True
        )

    async def test_allowed_off_script_latches_before_inert_write(self) -> None:
        await self.hass.services.async_call(
            "script",
            "projector_turn_off",
            {},
            context=Context(user_id=self.allowed.id),
            blocking=True,
        )
        self.assertEqual(self.calls, [(self.command, True)])

    async def test_trusted_raw_ir_latches_before_inert_write(self) -> None:
        await self.send(Context())
        self.assertEqual(self.calls, [(self.command, True)])

    async def test_physical_projector_observation_stays_trusted(self) -> None:
        self.receiver.native.frame_observer(
            {"repeat": False, "address": 0xBD00, "command": 0xFE01, "offset": 0},
            time.time(),
        )
        self.assertTrue(self.runtime.projector_shutting_down)
        self.assertEqual(self.calls, [])


class RemoteServiceContextTests(unittest.IsolatedAsyncioTestCase):
    """Verify caller identity and policy checks with inert real-HA services."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        self.calls = []
        self.wake_seen = asyncio.Event()
        self.permissions = Mock()
        self.permissions.check_entity.return_value = True
        self.hass.auth = SimpleNamespace(
            async_get_user=AsyncMock(
                return_value=(
                    SimpleNamespace(is_admin=False, permissions=self.permissions)
                )
            )
        )
        for entity, state in {
            TV: "on",
            PROJECTOR: "off",
            "input_boolean.audio_mode": "off",
            "input_select.universal_remote_pair": "tv",
            "input_boolean.universal_remote_native_target": "off",
            "input_boolean.nav_to_tv": "off",
            "input_text.universal_remote_focus_seconds": "60",
            "input_text.universal_remote_projector_until_ms": "0",
        }.items():
            self.hass.states.async_set(entity, state)

        async def capture(call) -> None:
            self.calls.append(call)
            entity = call.data.get("entity_id")
            if call.domain == "input_select":
                self.hass.states.async_set(entity, call.data["option"])
            elif call.domain == "input_text":
                self.hass.states.async_set(entity, call.data["value"])
            elif call.domain == "input_boolean":
                self.hass.states.async_set(
                    entity, "on" if call.service == "turn_on" else "off"
                )
            if call.service == "wake_if_current":
                self.wake_seen.set()

        for domain, service in (
            ("input_select", "select_option"),
            ("input_text", "set_value"),
            ("input_boolean", "turn_on"),
            ("input_boolean", "turn_off"),
            ("media_player", "volume_up"),
            ("media_player", "toggle"),
            ("media_player", "turn_off"),
            (DOMAIN, "wake_if_current"),
        ):
            self.hass.services.async_register(domain, service, capture)
        self.runtime = SimpleNamespace(
            hass=self.hass,
            entry=SimpleNamespace(data={}),
            projector_power_state=False,
            projector_shutting_down=False,
            shutdown=SimpleNamespace(requested_at=0.0),
            tv_power_pair=None,
            projector_power_pair=None,
            projector_power_entity_id="binary_sensor.projector_hdmi_connected",
        )
        self.controller = RemoteController(self.runtime)
        self.runtime.note_projector_off_requested = Mock()
        self.hass.data[DOMAIN] = {"remote_controller": self.controller}
        async_register_remote_services(self.hass)
        self.controller.refresh()
        await self.hass.async_block_till_done()
        self.calls.clear()

    async def asyncTearDown(self) -> None:
        await self.controller.async_close()
        await self.hass.async_block_till_done()
        self.tmp.cleanup()

    async def call(self, service, data, context) -> ServiceResponse:
        return await self.hass.services.async_call(
            DOMAIN,
            service,
            data,
            context=context,
            blocking=True,
        )

    async def test_concurrent_dispatches_keep_each_original_context(self) -> None:
        contexts = [Context(user_id="first"), Context(user_id="second")]
        await asyncio.gather(
            *(
                self.call(
                    "remote_dispatch",
                    {
                        "domain": "media_player",
                        "service": "volume_up",
                        "service_data": {"entity_id": TV},
                    },
                    context,
                )
                for context in contexts
            )
        )
        actual = [call.context for call in self.calls if call.domain == "media_player"]
        self.assertEqual(actual, contexts)

    async def test_helper_mirrors_keep_the_service_context(self) -> None:
        context = Context(user_id="selector")
        await self.call("remote_select", {"pair": "tv", "native": True}, context)
        await self.hass.async_block_till_done()
        self.assertTrue(self.calls)
        for call in self.calls:
            self.assertIs(call.context, context)

    async def test_delayed_wake_keeps_the_input_context(self) -> None:
        context = Context(user_id="input-caller")
        await self.call("remote_prepare", {"button": "input"}, context)
        await asyncio.wait_for(self.wake_seen.wait(), 2)
        wake = next(call for call in self.calls if call.service == "wake_if_current")
        self.assertIs(wake.context, context)

    async def test_permission_denial_precedes_off_latch_or_device_call(self) -> None:
        context = Context(user_id="denied-by-policy")
        self.permissions.check_entity.return_value = False
        self.runtime.projector_power_state = True
        with self.assertRaises(Unauthorized):
            await self.call("remote_power", {"pair": "projector"}, context)
        self.runtime.note_projector_off_requested.assert_not_called()
        self.assertFalse(any(call.domain == "media_player" for call in self.calls))
        self.permissions.check_entity.assert_called_once_with(PROJECTOR, "control")

    async def test_authorized_power_keeps_context_after_permission_check(self) -> None:
        context = Context(user_id="power-caller")
        self.runtime.projector_power_state = True
        await self.call("remote_power", {"pair": "projector"}, context)
        power = next(call for call in self.calls if call.domain == "media_player")
        self.assertIs(power.context, context)
        self.permissions.check_entity.assert_called_once_with(PROJECTOR, "control")
        self.runtime.note_projector_off_requested.assert_called_once_with()

    async def test_shutdown_during_auth_lookup_blocks_the_pending_power_call(
        self,
    ) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        user = self.hass.auth.async_get_user.return_value

        async def lookup(_user_id) -> SimpleNamespace:
            entered.set()
            await release.wait()
            return user

        self.hass.auth.async_get_user.side_effect = lookup
        self.runtime.projector_power_state = True
        task = asyncio.create_task(
            self.hass.services.async_call(
                DOMAIN,
                "remote_power",
                {"pair": "projector"},
                context=Context(user_id="pending-policy"),
                blocking=True,
                return_response=True,
            )
        )
        await entered.wait()
        self.runtime.projector_shutting_down = True
        release.set()
        self.assertEqual(
            await task, {"sent": False, "blocked": "projector_shutting_down"}
        )
        self.runtime.note_projector_off_requested.assert_not_called()
        self.assertFalse(any(call.domain == "media_player" for call in self.calls))

    async def test_trusted_physical_input_does_not_borrow_a_service_user(self) -> None:
        context = Context(user_id="unrelated-service")
        received_at = time.time()

        def receive_once() -> None:
            self.runtime.receiver = None
            self.controller.physical_press("input", received_at - 0.150)
            self.controller.physical_press("input", received_at)

        self.runtime.receiver = SimpleNamespace(drain_pending=receive_once)
        await self.call("remote_resolve", {}, context)
        await self.hass.async_block_till_done()
        self.assertEqual(self.controller.mode.target, "tv")
        self.assertTrue(self.calls)
        self.assertTrue(all(call.context.user_id is None for call in self.calls))


if __name__ == "__main__":
    unittest.main()
