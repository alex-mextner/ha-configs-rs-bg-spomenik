"""Defensive service-boundary checks with real HA calls and inert endpoints."""

import asyncio
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized

from custom_components.ekf_appliances.const import DOMAIN
from custom_components.ekf_appliances.remote_control import (
    PROJECTOR,
    TV,
    RemoteController,
    async_register_remote_services,
)


class RemoteServiceContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        self.calls = []
        self.wake_seen = asyncio.Event()
        self.permissions = Mock()
        self.permissions.check_entity.return_value = True
        self.hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=(
            SimpleNamespace(is_admin=False, permissions=self.permissions)
        )))
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

        async def capture(call):
            self.calls.append(call)
            entity = call.data.get("entity_id")
            if call.domain == "input_select":
                self.hass.states.async_set(entity, call.data["option"])
            elif call.domain == "input_text":
                self.hass.states.async_set(entity, call.data["value"])
            elif call.domain == "input_boolean":
                self.hass.states.async_set(entity, "on" if call.service == "turn_on" else "off")
            if call.service == "wake_if_current":
                self.wake_seen.set()

        for domain, service in (
            ("input_select", "select_option"), ("input_text", "set_value"),
            ("input_boolean", "turn_on"), ("input_boolean", "turn_off"),
            ("media_player", "volume_up"), ("media_player", "toggle"),
            ("media_player", "turn_off"), (DOMAIN, "wake_if_current"),
        ):
            self.hass.services.async_register(domain, service, capture)
        self.runtime = SimpleNamespace(
            hass=self.hass, entry=SimpleNamespace(data={}),
            projector_power_state=False, projector_shutting_down=False,
            tv_power_pair=None, projector_power_pair=None,
            projector_power_entity_id="binary_sensor.projector_hdmi_connected",
        )
        self.controller = RemoteController(self.runtime)
        self.runtime.note_projector_off_requested = Mock()
        self.hass.data[DOMAIN] = {"remote_controller": self.controller}
        async_register_remote_services(self.hass)
        self.controller.refresh()
        await self.hass.async_block_till_done()
        self.calls.clear()

    async def asyncTearDown(self):
        await self.controller.async_close()
        await self.hass.async_block_till_done()
        self.tmp.cleanup()

    async def call(self, service, data, context):
        return await self.hass.services.async_call(
            DOMAIN, service, data, context=context, blocking=True,
        )

    async def test_concurrent_dispatches_keep_each_original_context(self):
        contexts = [Context(user_id="first"), Context(user_id="second")]
        await asyncio.gather(*(
            self.call("remote_dispatch", {
                "domain": "media_player", "service": "volume_up",
                "service_data": {"entity_id": TV},
            }, context) for context in contexts
        ))
        actual = [call.context for call in self.calls if call.domain == "media_player"]
        self.assertEqual(actual, contexts)

    async def test_helper_mirrors_keep_the_service_context(self):
        context = Context(user_id="selector")
        await self.call("remote_select", {"pair": "tv", "native": True}, context)
        await self.hass.async_block_till_done()
        self.assertTrue(self.calls)
        for call in self.calls:
            self.assertIs(call.context, context)

    async def test_delayed_wake_keeps_the_input_context(self):
        context = Context(user_id="input-caller")
        await self.call("remote_prepare", {"button": "input"}, context)
        await asyncio.wait_for(self.wake_seen.wait(), 2)
        wake = next(call for call in self.calls if call.service == "wake_if_current")
        self.assertIs(wake.context, context)

    async def test_permission_denial_precedes_off_latch_or_device_call(self):
        context = Context(user_id="denied-by-policy")
        self.permissions.check_entity.return_value = False
        self.runtime.projector_power_state = True
        with self.assertRaises(Unauthorized):
            await self.call("remote_power", {"pair": "projector"}, context)
        self.runtime.note_projector_off_requested.assert_not_called()
        self.assertFalse(any(call.domain == "media_player" for call in self.calls))
        self.permissions.check_entity.assert_called_once_with(PROJECTOR, "control")

    async def test_authorized_power_keeps_context_after_permission_check(self):
        context = Context(user_id="power-caller")
        self.runtime.projector_power_state = True
        await self.call("remote_power", {"pair": "projector"}, context)
        power = next(call for call in self.calls if call.domain == "media_player")
        self.assertIs(power.context, context)
        self.permissions.check_entity.assert_called_once_with(PROJECTOR, "control")
        self.runtime.note_projector_off_requested.assert_called_once_with()

    async def test_shutdown_during_auth_lookup_blocks_the_pending_power_call(self):
        entered, release = asyncio.Event(), asyncio.Event()
        user = self.hass.auth.async_get_user.return_value

        async def lookup(_user_id):
            entered.set()
            await release.wait()
            return user

        self.hass.auth.async_get_user.side_effect = lookup
        self.runtime.projector_power_state = True
        task = asyncio.create_task(self.hass.services.async_call(
            DOMAIN, "remote_power", {"pair": "projector"},
            context=Context(user_id="pending-policy"), blocking=True, return_response=True,
        ))
        await entered.wait()
        self.runtime.projector_shutting_down = True
        release.set()
        self.assertEqual(await task, {"sent": False, "blocked": "projector_shutting_down"})
        self.runtime.note_projector_off_requested.assert_not_called()
        self.assertFalse(any(call.domain == "media_player" for call in self.calls))

    async def test_trusted_physical_input_does_not_borrow_a_service_user(self):
        context = Context(user_id="unrelated-service")
        received_at = time.time()

        def receive_once():
            self.runtime.receiver = None
            self.controller.physical_press("input", received_at - .150)
            self.controller.physical_press("input", received_at)

        self.runtime.receiver = SimpleNamespace(drain_pending=receive_once)
        await self.call("remote_resolve", {}, context)
        await self.hass.async_block_till_done()
        self.assertEqual(self.controller.mode.target, "tv")
        self.assertTrue(self.calls)
        self.assertTrue(all(call.context.user_id is None for call in self.calls))


if __name__ == "__main__":
    unittest.main()
