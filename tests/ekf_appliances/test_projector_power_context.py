"""Exercise accepted projector lifecycle work through inert real-HA services."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextvars import ContextVar
from functools import partial
from types import SimpleNamespace

from homeassistant.auth import auth_manager_from_config
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import entity_service_call

from custom_components.ekf_appliances.const import DOMAIN
from custom_components.ekf_appliances.media_player import ProjectorMediaPlayer
from custom_components.ekf_appliances.paired_media import _projector_pair
from custom_components.ekf_appliances.protocol import encode_pulses, projector_pulses
from custom_components.ekf_appliances.remote_control import (
    _SERVICE_CONTEXT,
    PROJECTOR,
    TV,
    RemoteController,
    async_register_remote_services,
)
from custom_components.ekf_appliances.runtime import (
    UNIVERSAL_PROJECTOR_COMMAND,
    ApplianceRuntime,
)

_TRACE = ContextVar("projector_context_test_trace", default=None)


class ProjectorPowerContextTests(unittest.IsolatedAsyncioTestCase):
    """Preserve lifecycle ownership without disabling unrelated remote guards."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        self.hass.auth = await auth_manager_from_config(self.hass, [], [])
        await self.hass.auth.async_create_user("Owner")
        user = await self.hass.auth.async_create_user(
            "Controller", group_ids=[GROUP_ID_USER]
        )
        self.context = Context(user_id=user.id)
        self.trace = object()
        self.runtime = ApplianceRuntime(
            self.hass,
            SimpleNamespace(
                entry_id="power-context",
                data={
                    "remote_entity_id": "remote.ekf",
                    "temperature_entity_id": "sensor.room_temperature",
                    "projector_power_entity_id": "binary_sensor.projector_hdmi",
                    "projector_on_script": "script.projector_turn_on",
                    "projector_off_script": "script.projector_turn_off",
                    "tv_kodi_entity_id": "media_player.kodi",
                },
            ),
        )
        for entity, state in {
            self.runtime.remote_entity_id: "on",
            self.runtime.projector_power_entity_id: "off",
            "sensor.kodi_service_state": "off",
            "media_player.kodi": "off",
            PROJECTOR: "off",
            TV: "off",
            "input_boolean.audio_mode": "off",
        }.items():
            self.hass.states.async_set(entity, state)
        await self.runtime.async_setup_shutdown()
        self.controller = RemoteController(self.runtime)
        self.runtime.remote_controller = self.controller
        self.hass.data[DOMAIN] = {"remote_controller": self.controller}
        async_register_remote_services(self.hass)

        self.pair = _projector_pair(self.runtime, lambda: None)
        self.runtime.projector_power_pair = self.pair
        self.player = ProjectorMediaPlayer(self.runtime)
        self.player.hass = self.hass
        self.player.entity_id = PROJECTOR
        for service, method in (
            ("turn_on", "async_turn_on"),
            ("turn_off", "async_turn_off"),
        ):
            self.hass.services.async_register(
                "media_player",
                service,
                partial(
                    entity_service_call,
                    self.hass,
                    {PROJECTOR: self.player},
                    method,
                ),
            )
        self.writes = []
        self.lifecycle_contexts = []
        self.on_entered = asyncio.Event()
        self.on_started = asyncio.Event()
        self.on_send_release = asyncio.Event()
        self.on_send_release.set()
        self.on_finish_release = asyncio.Event()
        self.off_entered = asyncio.Event()
        self.off_release = asyncio.Event()
        self.off_release.set()
        self.shutdown_seen = asyncio.Event()
        self.tasks = []
        self.runtime.shutdown_callbacks.append(self.shutdown_seen.set)
        self.hass.services.async_register("remote", "send_command", self.send)
        self.hass.services.async_register("script", "projector_turn_on", self.turn_on)
        self.hass.services.async_register("script", "projector_turn_off", self.turn_off)
        self.hass.services.async_register("rest_command", "host_kodi_start", self.kodi)
        self.hass.services.async_register("rest_command", "host_kodi_stop", self.kodi)
        self.pair.start()

    async def asyncTearDown(self) -> None:
        self.on_send_release.set()
        self.on_finish_release.set()
        self.off_release.set()
        await self.pair.close()
        await self.runtime.async_cancel_projector_power()
        await self.controller.async_close()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.runtime.async_close_shutdown()
        await self.hass.async_stop()
        self.tmp.cleanup()

    async def send(self, call) -> None:
        self.writes.append(
            (
                call.data["command"],
                UNIVERSAL_PROJECTOR_COMMAND.get(),
                _SERVICE_CONTEXT.get(),
                _TRACE.get(),
            )
        )

    async def turn_on(self, _call) -> None:
        self.on_entered.set()
        await self.on_send_release.wait()
        await self.runtime.async_send_projector("power")
        self.hass.states.async_set(self.runtime.projector_power_entity_id, "on")
        self.pair.observe()
        self.on_started.set()
        await self.on_finish_release.wait()

    async def turn_off(self, _call) -> None:
        self.lifecycle_contexts.append(
            (UNIVERSAL_PROJECTOR_COMMAND.get(), _SERVICE_CONTEXT.get(), _TRACE.get())
        )
        self.off_entered.set()
        await self.off_release.wait()
        await self.runtime.async_send_projector("power")
        await self.runtime.async_send_projector("power")
        self.hass.states.async_set(self.runtime.projector_power_entity_id, "off")
        self.pair.observe()

    async def kodi(self, call) -> None:
        self.hass.states.async_set(
            "sensor.kodi_service_state",
            "on" if call.service == "host_kodi_start" else "off",
        )
        self.pair.observe()

    def start_call(self, service, data, *, domain=DOMAIN) -> asyncio.Task:
        async def call() -> dict:
            token = _TRACE.set(self.trace)
            try:
                return await self.hass.services.async_call(
                    domain,
                    service,
                    data,
                    context=self.context,
                    blocking=True,
                    return_response=domain == DOMAIN,
                )
            finally:
                _TRACE.reset(token)

        task = asyncio.create_task(call())
        self.tasks.append(task)
        return task

    def start_on(self) -> asyncio.Task:
        return self.start_call(
            "remote_dispatch",
            {
                "pair": "projector",
                "domain": "media_player",
                "service": "turn_on",
                "service_data": {"entity_id": PROJECTOR},
            },
        )

    async def test_repeat_power_does_not_cancel_on_then_settled_off_keeps_confirmation_frames(
        self,
    ) -> None:
        on = self.start_on()
        await asyncio.wait_for(self.on_started.wait(), 2)
        worker, revision = self.pair._worker, self.pair.intent_revision
        repeat = await self.start_call("remote_power", {"pair": "projector"})
        self.assertEqual(
            repeat, {"sent": False, "blocked": "projector_power_transition"}
        )
        self.assertFalse(self.runtime.projector_shutting_down)
        self.assertIs(self.pair._worker, worker)
        self.assertEqual(self.pair.intent_revision, revision)
        self.on_finish_release.set()
        self.assertEqual(await asyncio.wait_for(on, 2), {"sent": True})
        off = self.start_call("remote_power", {"pair": "projector"})
        self.assertEqual(await asyncio.wait_for(off, 2), {"sent": True})
        power = encode_pulses(projector_pulses("power"))
        self.assertEqual([entry[0] for entry in self.writes], [power] * 3)
        self.assertEqual([entry[1] for entry in self.writes], [True, False, False])
        self.assertEqual(self.lifecycle_contexts, [(False, self.context, self.trace)])
        for _, _, context, trace in self.writes:
            self.assertIs(context, self.context)
            self.assertIs(trace, self.trace)
        self.assertFalse(UNIVERSAL_PROJECTOR_COMMAND.get())

    async def test_queued_remote_on_keeps_final_guard_while_native_off_completes(
        self,
    ) -> None:
        self.on_send_release.clear()
        on = self.start_on()
        await asyncio.wait_for(self.on_entered.wait(), 2)
        self.hass.states.async_set(self.runtime.projector_power_entity_id, "on")
        self.pair.observe()
        # A genuine native/OEM request remains independent of remote admission.
        off = self.start_call(
            "turn_off", {"entity_id": PROJECTOR}, domain="media_player"
        )
        await asyncio.wait_for(self.shutdown_seen.wait(), 2)
        self.on_send_release.set()
        self.on_finish_release.set()
        self.assertIsNone(await asyncio.wait_for(off, 2))
        with self.assertRaisesRegex(HomeAssistantError, "superseded"):
            await on
        self.assertEqual(len(self.writes), 2)
        self.assertTrue(all(not entry[1] for entry in self.writes))
        self.assertEqual(self.lifecycle_contexts, [(False, self.context, self.trace)])

    async def test_shutdown_ownership_does_not_allow_queued_or_new_kodi_navigation(
        self,
    ) -> None:
        self.hass.states.async_set(self.runtime.projector_power_entity_id, "on")
        self.hass.states.async_set("sensor.kodi_service_state", "on")
        self.pair.start()
        self.off_release.clear()
        entered = {"kodi": asyncio.Event(), "button": asyncio.Event()}
        release = asyncio.Event()
        unrelated_writes, cancelled_contexts = [], []

        async def queued(call) -> None:
            entered[call.domain].set()
            try:
                await release.wait()
                unrelated_writes.append(call.domain)
            finally:
                cancelled_contexts.append(
                    (UNIVERSAL_PROJECTOR_COMMAND.get(), _SERVICE_CONTEXT.get())
                )

        actions = [
            {"domain": "kodi", "service": "call_method"},
            {"domain": "button", "service": "press"},
        ]
        pending = []
        for action in actions:
            self.hass.services.async_register(
                action["domain"], action["service"], queued
            )
            pending.append(
                self.start_call("remote_dispatch", {"pair": "projector", **action})
            )
        for event in entered.values():
            await asyncio.wait_for(event.wait(), 2)
        off = self.start_call("remote_power", {"pair": "projector"})
        await asyncio.wait_for(self.off_entered.wait(), 2)
        blocked = {"sent": False, "blocked": "projector_shutting_down"}
        self.assertEqual(await asyncio.gather(*pending), [blocked, blocked])
        for action in actions:
            self.assertEqual(
                await self.start_call(
                    "remote_dispatch", {"pair": "projector", **action}
                ),
                blocked,
            )
        self.assertEqual(cancelled_contexts, [(True, self.context)] * 2)
        self.assertEqual(unrelated_writes, [])
        self.off_release.set()
        self.assertEqual(await asyncio.wait_for(off, 2), {"sent": True})
        self.assertEqual(len(self.writes), 2)
