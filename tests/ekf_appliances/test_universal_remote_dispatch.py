"""Execute the living-room remote package against real HA script/template helpers."""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import tempfile
import time
import unittest
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import yaml
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import (
    Context,
    HomeAssistant,
    ServiceCall,
    State,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame, trigger
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.script import Script, script_stack_cv
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util

from custom_components import remote_announcements
from custom_components.ekf_appliances.ir_decode import RemoteMapper
from custom_components.remote_announcements.rejection import CueResult, RejectionQueue

PACKAGE = Path(__file__).resolve().parents[2] / "packages/universal_remote.yaml"
MODES = list(product(("tv", "projector"), ("off", "on"), ("off", "on")))
AC = "climate.kondei_1"
WEATHER = "weather.forecast_home_assistant"
GLOBUS = "light.gostinaia_globus_socket_1"
TV = "media_player.ue48j5500_ue48j5500"
PROJECTOR = "media_player.projector"

# Load the real controller and services without booting unrelated integrations.
# Only the runtime's transport ContextVar is supplied by this test boundary.
PREFIX = "universal_remote_dispatch_test"
package = ModuleType(PREFIX)
package.__path__ = [str(PACKAGE.parents[1] / "custom_components/ekf_appliances")]
runtime_module = ModuleType(PREFIX + ".runtime")
runtime_module.UNIVERSAL_PROJECTOR_COMMAND = ContextVar(
    "test_remote_dispatch", default=False
)
with patch.dict(sys.modules, {PREFIX: package, PREFIX + ".runtime": runtime_module}):
    controller_module = importlib.import_module(PREFIX + ".remote_control")
    sensor_module = importlib.import_module(PREFIX + ".sensor")


class RuntimeBoundary:
    """Observed display state and shutdown latch; hardware services are captured."""

    tv_power_pair = projector_power_pair = None
    projector_power_entity_id = "binary_sensor.projector_hdmi_connected"
    projector_shutting_down = False

    def __init__(self, hass) -> None:
        self.hass = hass
        self.entry = SimpleNamespace(data={"tv_kodi_entity_id": "media_player.home"})

    @property
    def projector_power_state(self) -> Any:
        state = self.hass.states.get(PROJECTOR)
        if state is None or state.state in {"unknown", "unavailable"}:
            return None
        return state.state != "off"

    def note_projector_off_requested(self) -> None:
        if self.projector_power_state is True:
            self.projector_shutting_down = True
            self.controller.refresh()


class UniversalRemoteDispatchTests(unittest.IsolatedAsyncioTestCase):
    """Exercise universal remote dispatch tests contracts."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        await trigger.async_setup(self.hass)
        self.doc = yaml.safe_load(PACKAGE.read_text())
        self.calls = []
        self.scripts = {}
        self.automation_scripts = {}
        actions = {
            "ekf_appliances.wake_if_current",
            "remote_announcements.reject",
            "rest_command.host_kodi_text_input",
            "media_player.turn_on",
            "media_player.turn_off",
            "media_player.toggle",
            "media_player.select_source",
            "media_player.media_pause",
            "media_player.media_play",
            "media_player.media_play_pause",
            "media_player.media_previous_track",
            "media_player.media_next_track",
            "media_player.volume_up",
            "media_player.volume_down",
            "media_player.volume_set",
            "media_player.volume_mute",
            "remote.turn_on",
            "remote.turn_off",
            "remote.send_command",
            "androidtv_remote.send_text",
            "button.press",
            "light.toggle",
            "climate.set_temperature",
            "climate.set_hvac_mode",
            "climate.turn_off",
            "input_boolean.turn_on",
            "input_boolean.turn_off",
            "input_boolean.toggle",
            "input_text.set_value",
            "persistent_notification.create",
            "system_log.write",
            "notify.send_message",
            "aisis_speaker.send_directive",
            "script.music_remote_command",
            "script.projector_turn_on",
            "script.projector_turn_off",
            "input_select.select_option",
            "timer.start",
            "timer.cancel",
            "kodi.call_method",
            "light.turn_on",
        }

        for action in actions:
            domain, service = action.split(".", 1)
            if action in {
                "androidtv_remote.send_text",
                "rest_command.host_kodi_text_input",
                "kodi.call_method",
            }:
                self.hass.services.async_register(
                    domain,
                    service,
                    self.capture,
                    supports_response=SupportsResponse.OPTIONAL,
                )
            else:
                self.hass.services.async_register(domain, service, self.capture)
        self.runtime = RuntimeBoundary(self.hass)
        self.controller = controller_module.RemoteController(self.runtime)
        self.runtime.controller = self.controller
        self.hass.data.setdefault("ekf_appliances", {})["remote_controller"] = (
            self.controller
        )
        controller_module.async_register_remote_services(self.hass)
        for name, config in self.doc["script"].items():
            script = Script(
                self.hass,
                cv.SCRIPT_SCHEMA(config["sequence"]),
                name,
                "script",
                script_mode=config.get("mode", "single"),
                max_runs=config.get("max", 10),
            )
            self.scripts[name] = script

            async def run_script(call, script=script) -> Any:
                # Exact ScriptEntity._async_start_run(wait=True) behavior.
                if stack := script_stack_cv.get():
                    script_stack_cv.set(stack.copy())
                result = await script.async_run(dict(call.data), context=call.context)
                return result.service_response if result else None

            self.hass.services.async_register(
                "script", name, run_script, supports_response=SupportsResponse.OPTIONAL
            )
        self.background = []
        self.background_by_script = {}

        async def start_script(call) -> None:
            for entity in call.data.get("entity_id", []):
                name = entity.split(".", 1)[1]
                variables = call.data.get("variables", {})
                # Exact ScriptEntity._async_start_run(wait=False) behavior.
                script_stack_cv.set([])
                task = self.hass.async_create_task(
                    self.scripts[name].async_run(variables, context=call.context)
                )
                self.background.append(task)
                self.background_by_script.setdefault(name, []).append(task)

        self.hass.services.async_register("script", "turn_on", start_script)
        self.reset()
        await self.controller.async_start()

    async def capture(self, call) -> Any:
        action = f"{call.domain}.{call.service}"
        data = dict(call.data)
        self.calls.append((action, data))
        targets = data.get("entity_id", [])
        if isinstance(targets, str):
            targets = [targets]
        if action == "androidtv_remote.send_text":
            # Actual HA entity-platform service response is keyed by entity id.
            return {entity: {"sent": True, "verified": False} for entity in targets}
        if action == "rest_command.host_kodi_text_input":
            return {"status": 200, "content": {"ok": True, "verified": True}}
        if action == "kodi.call_method":
            return self.kodi_responses.get(data.get("method"), {})
        for entity in targets:
            self.capture_target(call, data, entity)
        return None

    def capture_target(self, call, data, entity) -> None:
        state = self.hass.states.get(entity)
        attrs = dict(state.attributes) if state else {}
        if call.domain == "input_boolean":
            self.capture_boolean(call.service, entity, state, attrs)
        elif call.domain == "input_select":
            self.hass.states.async_set(entity, str(data["option"]), attrs)
        elif call.domain == "timer":
            self.hass.states.async_set(
                entity, "active" if call.service == "start" else "idle", attrs
            )
        elif call.domain == "input_text":
            self.capture_text(entity, data["value"], attrs)
        elif call.domain == "climate":
            self.capture_climate(call.service, entity, state, attrs, data)

    def capture_boolean(self, service, entity, state, attrs) -> None:
        value = "off" if service == "turn_off" else "on"
        if service == "toggle" and state and state.state == "on":
            value = "off"
        self.hass.states.async_set(entity, value, attrs)

    def capture_text(self, entity, value, attrs) -> None:
        config = self.doc["input_text"].get(entity.removeprefix("input_text."), {})
        if len(str(value)) > config.get("max", 255):
            error_message = "input_text value exceeds configured maximum"
            raise HomeAssistantError(error_message)
        self.hass.states.async_set(entity, str(value), attrs)

    def capture_climate(self, service, entity, state, attrs, data) -> None:
        mode = state.state if state else "off"
        if "temperature" in data:
            attrs["temperature"] = data["temperature"]
        if "hvac_mode" in data:
            mode = data["hvac_mode"]
        if service == "turn_off":
            mode = "off"
        self.hass.states.async_set(entity, mode, attrs)

    async def asyncTearDown(self) -> None:
        for script in [*self.automation_scripts.values(), *self.scripts.values()]:
            await script.async_stop()
        await self.controller.async_close()
        await self.hass.async_block_till_done()
        for task in self.background:
            if not task.done():
                task.cancel()
        if self.background:
            await asyncio.gather(*self.background, return_exceptions=True)
        self.temp.cleanup()

    def reset(self, mode=("tv", "off", "off"), ac_mode="heat", target=24) -> None:
        source, audio, navigation = mode
        self.controller.mode.close()
        self.controller.mode = controller_module.RemoteMode(
            clock=time.time,
            schedule=self.controller._schedule,
            changed=self.controller._changed,
            wake=self.controller._wake,
        )
        self.controller._clear_audio = False
        self.controller._captured_routes.clear()
        self.controller._captured_press_ids.clear()
        self.controller._pending_power = None
        self.runtime.projector_shutting_down = False
        self.kodi_responses = {}
        self.calls.clear()
        for entity, state in (
            ("input_text.tv_source", "chromecast"),
            ("input_boolean.audio_mode", audio),
            ("input_select.universal_remote_pair", source),
            ("input_boolean.universal_remote_native_target", navigation),
            ("input_text.universal_remote_input_ms", "0"),
            ("input_text.universal_remote_input_context", "{}"),
            ("input_text.universal_remote_focus_seconds", "60"),
            ("input_text.universal_remote_wake_context", "{}"),
            (
                "input_text.universal_remote_projector_until_ms",
                str(int(dt_util.utcnow().timestamp() * 1000) + 60000),
            ),
            (
                "timer.universal_remote_projector",
                "active" if source == "projector" else "idle",
            ),
            ("input_boolean.nav_to_tv", navigation),
            ("input_boolean.projector_power", "off"),
            ("input_boolean.audio_was_playing", "off"),
            ("input_text.audio_last_press_ms", "0"),
            ("input_text.ac_last_mode", "cool"),
            (TV, "off"),
            ("media_player.projector", "off"),
            ("media_player.televizor", "off"),
            (GLOBUS, "off"),
        ):
            self.hass.states.async_set(entity, state)
        for entity, revision in [
            (TV, "tv:0"),
            ("media_player.projector", "projector:0"),
        ]:
            self.hass.states.async_set(
                entity,
                "off",
                {
                    "power_intent_revision": revision,
                    "last_power_intent": None,
                    "power_sync_target": None,
                },
            )
        self.hass.states.async_set(
            AC,
            ac_mode,
            {
                "temperature": target,
                "min_temp": 16,
                "max_temp": 30,
                "target_temp_step": 1,
                "current_temperature": 20.7,
            },
        )
        self.weather(18.3)
        self.controller.refresh()
        self.controller.mode.select(source, native=navigation == "on")

    def weather(self, value, unit="°C", state="sunny") -> None:
        self.hass.states.async_set(
            WEATHER, state, {"temperature": value, "temperature_unit": unit}
        )

    async def run_automation(self, identity, button, **variables: Any) -> None:
        config = next(item for item in self.doc["automation"] if item["id"] == identity)
        now = dt_util.utcnow()
        previous = State(
            "event.ekf_universal_remote",
            (now - timedelta(seconds=1)).isoformat(),
            {
                "event_type": "info",
                "last_received": now.timestamp() - 1,
                "receiver_enabled": True,
            },
            last_updated=now - timedelta(seconds=1),
        )
        current = State(
            "event.ekf_universal_remote",
            now.isoformat(),
            {
                "event_type": button,
                "last_received": now.timestamp(),
                "receiver_enabled": True,
            },
        )
        trigger = {
            "platform": "state",
            "entity_id": current.entity_id,
            "from_state": previous,
            "to_state": current,
        }
        override = variables.pop("trigger_override", None)
        if override is None:
            self.controller.physical_press(button, now.timestamp())
        else:
            trigger = override
        sequence = []
        if config.get("variables"):
            sequence.append({"variables": config["variables"]})
        sequence.extend(config.get("condition", []))
        sequence.extend(config["action"])
        if identity not in self.automation_scripts:
            self.automation_scripts[identity] = Script(
                self.hass,
                cv.SCRIPT_SCHEMA(sequence),
                identity,
                "automation",
                script_mode=config.get("mode", "single"),
                max_runs=config.get("max", 10),
            )
        await self.automation_scripts[identity].async_run(
            {"trigger": trigger, **variables}, context=Context()
        )

    def appliance_calls(self) -> Any:
        return [
            item
            for item in self.calls
            if item[0].split(".")[0]
            in {
                "climate",
                "light",
                "media_player",
                "remote",
                "button",
                "kodi",
                "ekf_appliances",
                "rest_command",
                "androidtv_remote",
            }
            or item[0]
            in {
                "script.projector_turn_on",
                "script.projector_turn_off",
                "script.music_remote_command",
                "persistent_notification.create",
            }
        ]

    async def test_channel_changes_ac_target_in_every_display_audio_and_navigation_mode(
        self,
    ) -> None:
        for mode, button, target in product(
            MODES, ("channel_up", "channel_down"), (24,)
        ):
            with self.subTest(mode=mode, button=button):
                self.reset(mode, target=target)
                await self.run_automation("universal_remote_dispatcher", button)
                wanted = 25 if button == "channel_up" else 23
                self.assertEqual(
                    self.appliance_calls(),
                    [
                        (
                            "climate.set_temperature",
                            {"entity_id": [AC], "temperature": wanted},
                        )
                    ],
                )
                self.assertEqual(
                    self.hass.states.get("input_boolean.audio_mode").state, mode[1]
                )

    async def test_channel_keeps_off_mode_and_does_not_repeat_a_bound_value(
        self,
    ) -> None:
        for ac_mode, target, button, wanted in [
            ("off", 24, "channel_up", 25),
            ("off", 24, "channel_down", 23),
            ("heat", 30, "channel_up", None),
            ("cool", 16, "channel_down", None),
        ]:
            with self.subTest(ac_mode=ac_mode, target=target, button=button):
                self.reset(ac_mode=ac_mode, target=target)
                await self.run_automation("universal_remote_dispatcher", button)
                expected = (
                    []
                    if wanted is None
                    else [
                        (
                            "climate.set_temperature",
                            {"entity_id": [AC], "temperature": wanted},
                        )
                    ]
                )
                self.assertEqual(self.appliance_calls(), expected)
                self.assertEqual(self.hass.states.get(AC).state, ac_mode)

    async def test_smart_toggles_the_existing_globus_in_every_mode(self) -> None:
        for mode in MODES:
            with self.subTest(mode=mode):
                self.reset(mode)
                await self.run_automation("universal_remote_dispatcher", "smart")
                self.assertEqual(
                    self.appliance_calls(), [("light.toggle", {"entity_id": [GLOBUS]})]
                )
                self.assertEqual(
                    self.hass.states.get("input_boolean.audio_mode").state, mode[1]
                )

    async def test_power_uses_only_the_selected_display_in_every_mode(self) -> None:
        for mode in MODES:
            with self.subTest(mode=mode):
                self.reset(mode)
                await self.run_automation("universal_remote_dispatcher", "power")
                target = "media_player.projector" if mode[0] == "projector" else TV
                self.assertEqual(
                    self.appliance_calls(),
                    [
                        (
                            "media_player.turn_on"
                            if mode[0] == "projector"
                            else "media_player.toggle",
                            {"entity_id": target},
                        )
                    ],
                )
                self.assertEqual(
                    self.hass.states.get("input_boolean.audio_mode").state, mode[1]
                )

    async def test_amazon_turns_on_once_at_24_using_outdoor_temperature_in_every_mode(
        self,
    ) -> None:
        for mode, outdoor in product(MODES, (23.9, 24.0, 30.0)):
            wanted = "heat" if outdoor == 23.9 else "cool"
            with self.subTest(mode=mode, outdoor=outdoor):
                self.reset(mode, ac_mode="off", target=30)
                self.weather(outdoor)
                await self.run_automation("universal_remote_dispatcher", "prime")
                await self.run_automation("universal_remote_prime", "prime")
                self.assertEqual(
                    self.appliance_calls(),
                    [
                        (
                            "climate.set_temperature",
                            {"entity_id": [AC], "temperature": 24, "hvac_mode": wanted},
                        )
                    ],
                )
                self.assertEqual(
                    self.hass.states.get("input_boolean.audio_mode").state, mode[1]
                )

    async def test_amazon_turns_off_once_in_every_mode(self) -> None:
        for mode in MODES:
            with self.subTest(mode=mode):
                self.reset(mode, ac_mode="heat", target=30)
                await self.run_automation("universal_remote_dispatcher", "prime")
                await self.run_automation("universal_remote_prime", "prime")
                self.assertEqual(
                    self.appliance_calls(), [("climate.turn_off", {"entity_id": [AC]})]
                )

    async def test_amazon_hold_does_not_add_a_second_command_or_change_mode(
        self,
    ) -> None:
        self.reset(ac_mode="off", target=30)
        await self.run_automation("universal_remote_prime", "prime")
        await self.run_automation("universal_remote_prime", "prime_hold")
        self.assertEqual(
            self.appliance_calls(),
            [
                (
                    "climate.set_temperature",
                    {"entity_id": [AC], "temperature": 24, "hvac_mode": "heat"},
                )
            ],
        )

    async def test_amazon_does_not_guess_from_room_temperature_when_weather_is_missing(
        self,
    ) -> None:
        for value, state, unit in [
            (None, "sunny", "°C"),
            ("unknown", "sunny", "°C"),
            (18, "unavailable", "°C"),
            (18, "sunny", "invalid"),
        ]:
            with self.subTest(value=value, state=state, unit=unit):
                self.reset(ac_mode="off")
                self.weather(value, unit, state)
                await self.run_automation("universal_remote_prime", "prime")
                errors = [
                    data
                    for action, data in self.calls
                    if action == "system_log.write" and data.get("level") == "error"
                ]
                self.assertEqual(len(errors), 1)
                self.assertEqual(
                    [x for x in self.calls if x[0].startswith("climate.")], []
                )

    async def test_calendar_does_not_override_an_explicit_heat_command(self) -> None:
        self.reset(ac_mode="heat", target=24)
        now = datetime(2026, 6, 15, 12, tzinfo=UTC)
        trigger = {
            "platform": "state",
            "from_state": State(AC, "off"),
            "to_state": self.hass.states.get(AC),
        }
        for config in self.doc["automation"]:
            if any(t.get("entity_id") == AC for t in config.get("trigger", [])):
                sequence = config.get("condition", []) + config["action"]
                script = Script(
                    self.hass, cv.SCRIPT_SCHEMA(sequence), config["id"], "automation"
                )
                await script.async_run(
                    {"trigger": trigger, "now": lambda: now}, context=Context()
                )
        self.assertEqual([x for x in self.calls if x[0].startswith("climate.")], [])

    async def test_audio_controls_use_the_music_owner_router(self) -> None:
        for button, command, extra in [
            ("left", "previous", {}),
            ("next", "next", {}),
            ("play", "play_pause", {}),
            ("volume_up", "volume_up", {}),
            ("down", "volume_down", {}),
            ("1", "volume_set", {"volume_level": 0.1}),
            ("9", "volume_set", {"volume_level": 0.9}),
            ("info", "info", {}),
            ("apple_tv", "bluetooth_enable", {}),
        ]:
            with self.subTest(button=button):
                self.reset(("tv", "on", "off"))
                await self.scripts["audio_mode_dispatch"].async_run(
                    {"button": button}, context=Context()
                )
                calls = [
                    x
                    for x in self.appliance_calls()
                    if x[0] != "script.music_remote_command"
                    or x[1].get("command") not in {"beep_on", "beep_off"}
                ]
                self.assertEqual(
                    calls,
                    [("script.music_remote_command", {"command": command, **extra})],
                )

    async def test_diagnostic_attribute_updates_do_not_replay_any_remote_automation(
        self,
    ) -> None:
        now = dt_util.utcnow()
        for identity, button in [
            ("universal_remote_dispatcher", "smart"),
            ("universal_remote_prime", "prime"),
            ("universal_remote_disney", "disney"),
        ]:
            with self.subTest(automation=identity):
                self.reset()
                deadline = self.controller.mode.expires_at
                attrs = {
                    "event_type": button,
                    "last_received": now.timestamp() - 1,
                    "receiver_enabled": True,
                    "last_error": None,
                }
                old = State(
                    "event.ekf_universal_remote",
                    (now - timedelta(seconds=1)).isoformat(),
                    attrs,
                )
                current = State(
                    old.entity_id, old.state, {**attrs, "last_rearmed": now.timestamp()}
                )
                await self.run_automation(
                    identity,
                    button,
                    trigger_override={
                        "platform": "state",
                        "entity_id": old.entity_id,
                        "from_state": old,
                        "to_state": current,
                    },
                )
                self.assertEqual(self.appliance_calls(), [])
                self.assertEqual(self.controller.mode.expires_at, deadline)

    async def test_first_fresh_event_after_unknown_passes_each_remote_guard(
        self,
    ) -> None:
        now = dt_util.utcnow()
        for identity, button in [
            ("universal_remote_dispatcher", "smart"),
            ("universal_remote_prime", "prime"),
            ("universal_remote_disney", "disney"),
        ]:
            with self.subTest(automation=identity):
                old = State(
                    "event.ekf_universal_remote",
                    "unknown",
                    last_updated=now - timedelta(seconds=1),
                )
                current = State(
                    old.entity_id,
                    now.isoformat(),
                    {
                        "event_type": button,
                        "last_received": now.timestamp(),
                        "receiver_enabled": True,
                    },
                )
                config = next(
                    item for item in self.doc["automation"] if item["id"] == identity
                )
                guard = Template(config["condition"][0]["value_template"], self.hass)
                self.assertIs(
                    guard.async_render(
                        {"trigger": {"from_state": old, "to_state": current}}
                    ),
                    expr2=True,
                )

    async def test_normal_transport_never_falls_back_to_speaker(self) -> None:
        for pair, native, state, button in product(
            ("tv", "projector"),
            ("off", "on"),
            ("off", "idle", "on", "playing", "paused", "buffering"),
            ("play", "prev", "next", "volume_up", "volume_down", "mute"),
        ):
            with self.subTest(pair=pair, native=native, state=state, button=button):
                self.reset((pair, "off", native))
                self.hass.states.async_set(TV, state, {"is_volume_muted": False})
                self.hass.states.async_set("media_player.projector", state)
                self.hass.states.async_set(
                    "media_player.home", state, {"is_volume_muted": False}
                )
                self.hass.states.async_set(
                    "media_player.ue48j5500_ue48j5500_network",
                    state,
                    {"is_volume_muted": False},
                )
                await self.run_automation("universal_remote_dispatcher", button)
                self.assertFalse(
                    any(x[0] == "script.music_remote_command" for x in self.calls)
                )
                calls = self.appliance_calls()
                if state == "off":
                    self.assertEqual(calls, [])
                    continue
                self.assertEqual(len(calls), 1)
                target = self.expected_transport_target(pair, native, button)
                self.assertEqual(calls[0][1].get("entity_id"), [target])

    @staticmethod
    def expected_transport_target(pair, native, button) -> Any:
        if button.startswith("volume") or button == "mute":
            if pair == "projector" and native == "on":
                suffix = {
                    "volume_up": "vol_up",
                    "volume_down": "vol_down",
                    "mute": "mute",
                }[button]
                return "button.projector_" + suffix
            if pair == "projector":
                return "media_player.home"
            return "remote.ue48j5500_ue48j5500" if native == "on" else TV
        return "media_player.home" if pair == "projector" else TV

    async def test_input_single_switches_pair_without_power_action(self) -> None:
        self.reset()
        await self.scripts["remote_modes_prepare"].async_run(
            {"button": "input"}, context=Context()
        )
        self.assertEqual(
            self.hass.states.get("input_select.universal_remote_pair").state, "tv"
        )
        await asyncio.sleep(1.1)
        self.assertEqual(
            self.hass.states.get("input_select.universal_remote_pair").state,
            "projector",
        )
        self.assertEqual(
            self.hass.states.get("input_boolean.universal_remote_native_target").state,
            "off",
        )
        self.assertEqual(self.appliance_calls(), [])

    async def test_input_double_keeps_pair_and_toggles_native_target(self) -> None:
        for pair in ("tv", "projector"):
            self.reset((pair, "off", "off"))
            await self.scripts["remote_modes_prepare"].async_run(
                {"button": "input"}, context=Context()
            )
            stamp = self.controller.mode.pending.at * 1000
            self.controller.physical_press("input", (stamp + 1000) / 1000)
            await self.scripts["remote_modes_prepare"].async_run(
                {"button": "input", "received_ms": stamp + 1000}, context=Context()
            )
            self.assertEqual(
                self.hass.states.get("input_select.universal_remote_pair").state, pair
            )
            self.assertEqual(
                self.hass.states.get(
                    "input_boolean.universal_remote_native_target"
                ).state,
                "on",
            )
            await asyncio.sleep(1.1)
            self.assertEqual(
                self.hass.states.get("input_select.universal_remote_pair").state, pair
            )
            self.assertEqual(self.appliance_calls(), [])

    async def test_following_command_flushes_pending_input_without_later_switch(
        self,
    ) -> None:
        self.reset()
        self.set_power_pair("on", "on")
        await self.scripts["remote_modes_prepare"].async_run(
            {"button": "input"}, context=Context()
        )
        await self.run_automation("universal_remote_dispatcher", "up")
        self.assertEqual(
            self.hass.states.get("input_select.universal_remote_pair").state,
            "projector",
        )
        self.assertEqual(
            self.appliance_calls(),
            [
                (
                    "kodi.call_method",
                    {"entity_id": "media_player.home", "method": "Input.Up"},
                )
            ],
        )
        await asyncio.sleep(1.1)
        self.assertEqual(
            self.hass.states.get("input_select.universal_remote_pair").state,
            "projector",
        )

    async def test_projector_expiry_returns_tv_and_does_not_power_anything(
        self,
    ) -> None:
        self.reset(("projector", "off", "on"))
        self.controller.mode.expires_at = time.time() - 1
        self.hass.states.async_set("timer.universal_remote_projector", "idle")
        await self.scripts["remote_modes_prepare"].async_run(
            {"button": "projector_timeout"}, context=Context()
        )
        self.assertEqual(
            self.hass.states.get("input_select.universal_remote_pair").state, "tv"
        )
        self.assertEqual(
            self.hass.states.get("input_boolean.universal_remote_native_target").state,
            "off",
        )
        self.assertEqual(self.appliance_calls(), [])

    async def test_stale_projector_timer_does_not_exit_refreshed_mode(self) -> None:
        self.reset(("projector", "off", "on"))
        await self.scripts["remote_modes_prepare"].async_run(
            {"button": "projector_timeout"}, context=Context()
        )
        self.assertEqual(
            self.hass.states.get("input_select.universal_remote_pair").state,
            "projector",
        )

    async def test_every_remote_action_refreshes_exactly_sixty_seconds(self) -> None:
        for button, audio in [
            ("up", "off"),
            ("volume_up", "off"),
            ("power", "on"),
            ("smart", "off"),
            ("prime", "off"),
            ("channel_up", "off"),
            ("volume_up", "on"),
            ("audio", "off"),
            ("disney", "off"),
        ]:
            with self.subTest(button=button, audio=audio):
                self.reset(("projector", audio, "off"))
                self.controller.mode.expires_at = time.time() + 1
                received_ms = time.time() * 1000
                await self.prepare_mode(button, received_ms=received_ms)
                self.assertAlmostEqual(
                    self.controller.mode.expires_at, received_ms / 1000 + 60, places=6
                )
                self.assertEqual(
                    self.hass.states.get(
                        "input_text.universal_remote_focus_seconds"
                    ).state,
                    "60",
                )

    async def test_navigation_uses_four_exact_targets_and_kodi_ok(self) -> None:
        for pair, native, direction, action, target, command in [
            (
                "tv",
                False,
                "ok",
                "remote.send_command",
                "remote.televizor",
                "KEYCODE_DPAD_CENTER",
            ),
            (
                "tv",
                True,
                "ok",
                "remote.send_command",
                "remote.ue48j5500_ue48j5500",
                "KEY_ENTER",
            ),
            (
                "projector",
                False,
                "ok",
                "kodi.call_method",
                "media_player.home",
                "Input.Select",
            ),
            ("projector", True, "ok", "button.press", "button.projector_ok", None),
        ]:
            with self.subTest(pair=pair, native=native):
                self.reset()
                self.set_power_pair("on", "on")
                await self.scripts["remote_nav"].async_run(
                    {"direction": direction, "pair": pair, "native": native},
                    context=Context(),
                )
                calls = self.appliance_calls()
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], action)
                self.assertIn(target, calls[0][1]["entity_id"])
                if command is not None:
                    self.assertEqual(
                        calls[0][1].get("command", calls[0][1].get("method")), command
                    )

    async def test_cc_exit_always_opens_home_and_back_remains_back(self) -> None:
        self.reset()
        self.set_power_pair("on", "off")
        await self.run_automation("universal_remote_dispatcher", "exit")
        await self.run_automation("universal_remote_dispatcher", "back")
        self.assertEqual(
            self.appliance_calls(),
            [
                (
                    "remote.send_command",
                    {
                        "entity_id": ["remote.televizor"],
                        "command": "KEYCODE_HOME",
                        "delay_secs": 0,
                    },
                ),
                (
                    "remote.send_command",
                    {
                        "entity_id": ["remote.televizor"],
                        "command": "KEYCODE_BACK",
                        "delay_secs": 0,
                    },
                ),
            ],
        )

    async def test_rapid_navigation_volume_and_delete_are_not_dropped_or_debounced(
        self,
    ) -> None:
        for button, service, command in [
            ("up", "remote.send_command", "KEYCODE_DPAD_UP"),
            ("volume_up", "media_player.volume_up", None),
            ("dash", "androidtv_remote.send_text", "delete_backward"),
        ]:
            with self.subTest(button=button):
                self.reset()
                self.set_power_pair("on", "off")
                # Real HA Script queues and templates, with only external device
                # services replaced. A one-second/key delay cannot fit this bound.
                await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            self.run_automation("universal_remote_dispatcher", button)
                            for _ in range(12)
                        )
                    ),
                    2.0,
                )
                self.assert_rapid_calls(self.appliance_calls(), service, command)

    def assert_rapid_calls(self, calls, service, command) -> None:
        self.assertEqual(len(calls), 12)
        self.assertTrue(all(name == service for name, _ in calls))
        expected_target = TV if command is None else "remote.televizor"
        self.assertTrue(
            all(data["entity_id"] == [expected_target] for _, data in calls)
        )
        if service == "androidtv_remote.send_text":
            self.assertTrue(
                all(
                    data["operation"] == command and data["text"] == ""
                    for _, data in calls
                )
            )
        elif command:
            self.assertTrue(all(data["command"] == command for _, data in calls))
            self.assertTrue(all(data["delay_secs"] == 0 for _, data in calls))

    async def test_input_after_double_window_is_two_singles_not_native_toggle(
        self,
    ) -> None:
        self.reset()
        await self.scripts["remote_modes_prepare"].async_run(
            {"button": "input"}, context=Context()
        )
        stamp = self.controller.mode.pending.at * 1000
        self.controller.physical_press("input", (stamp + 1001) / 1000)
        await self.scripts["remote_modes_prepare"].async_run(
            {"button": "input", "received_ms": stamp + 1001}, context=Context()
        )
        self.assertEqual(
            self.hass.states.get("input_select.universal_remote_pair").state,
            "projector",
        )
        self.assertEqual(
            self.hass.states.get("input_boolean.universal_remote_native_target").state,
            "off",
        )
        # Flush the second single with an immediate next command.
        await self.scripts["remote_modes_prepare"].async_run(
            {"button": "right"}, context=Context()
        )
        self.assertEqual(
            self.hass.states.get("input_select.universal_remote_pair").state, "tv"
        )
        self.assertEqual(self.appliance_calls(), [])

    async def test_audio_double_tap_has_no_single_play_side_effect_and_needs_no_hold(
        self,
    ) -> None:
        self.reset()
        first = asyncio.create_task(
            self.scripts["audio_mode_handle_press"].async_run({}, context=Context())
        )
        await asyncio.sleep(0.05)
        await self.scripts["audio_mode_handle_press"].async_run({}, context=Context())
        await first
        self.assertEqual(self.hass.states.get("input_boolean.audio_mode").state, "on")
        self.assertFalse(
            any(
                data.get("command") == "play_pause"
                for action, data in self.calls
                if action == "script.music_remote_command"
            )
        )
        await self.scripts["audio_mode_handle_press"].async_run({}, context=Context())
        self.assertEqual(self.hass.states.get("input_boolean.audio_mode").state, "off")

    async def test_input_single_at_projector_expiry_does_not_reenter_projector(
        self,
    ) -> None:
        self.reset(("projector", "off", "on"))
        await self.scripts["remote_modes_prepare"].async_run(
            {"button": "input"}, context=Context()
        )
        self.controller.mode.expires_at = time.time() - 1
        await self.scripts["remote_modes_prepare"].async_run(
            {"button": "projector_timeout"}, context=Context()
        )
        await asyncio.sleep(1.1)
        self.assertEqual(
            self.hass.states.get("input_select.universal_remote_pair").state, "tv"
        )
        self.assertEqual(
            self.hass.states.get("input_boolean.universal_remote_native_target").state,
            "off",
        )
        self.assertEqual(self.appliance_calls(), [])

    async def test_double_input_at_expiry_keeps_first_pair_and_toggles_first_target(
        self,
    ) -> None:
        self.reset(("projector", "off", "off"))
        await self.scripts["remote_modes_prepare"].async_run(
            {"button": "input"}, context=Context()
        )
        stamp = self.controller.mode.pending.at * 1000
        self.controller.mode.expires_at = time.time() - 1
        await self.scripts["remote_modes_prepare"].async_run(
            {"button": "projector_timeout"}, context=Context()
        )
        self.assertEqual(
            self.hass.states.get("input_select.universal_remote_pair").state, "tv"
        )
        self.controller.physical_press("input", (stamp + 500) / 1000)
        await self.scripts["remote_modes_prepare"].async_run(
            {"button": "input", "received_ms": stamp + 500}, context=Context()
        )
        self.assertEqual(
            self.hass.states.get("input_select.universal_remote_pair").state,
            "projector",
        )
        self.assertEqual(
            self.hass.states.get("input_boolean.universal_remote_native_target").state,
            "on",
        )
        self.assertEqual(self.appliance_calls(), [])

    async def test_unknown_player_mute_does_not_guess_and_native_tv_uses_toggle(
        self,
    ) -> None:
        for pair in ("tv", "projector"):
            self.reset((pair, "off", "off"))
            await self.scripts["remote_volume"].async_run(
                {"command": "mute", "pair": pair, "native": False}, context=Context()
            )
            self.assertEqual(self.appliance_calls(), [])
        self.hass.states.async_set(TV, "on")
        await self.scripts["remote_volume"].async_run(
            {"command": "mute", "pair": "tv", "native": True}, context=Context()
        )
        self.assertEqual(
            self.appliance_calls(),
            [
                (
                    "remote.send_command",
                    {
                        "entity_id": ["remote.ue48j5500_ue48j5500"],
                        "command": "KEY_MUTE",
                        "delay_secs": 0,
                    },
                )
            ],
        )

    async def test_native_target_backspace_does_not_leak_to_player(self) -> None:
        for pair in ("tv", "projector"):
            self.reset((pair, "off", "on"))
            await self.scripts["t9_backspace"].async_run(
                {"pair": pair, "native": True}, context=Context()
            )
            self.assertEqual(self.appliance_calls(), [])

    async def test_legacy_launch_and_tv_transport_use_fixed_pairs_without_hdmi_switching(
        self,
    ) -> None:
        legacy = yaml.safe_load((PACKAGE.parents[1] / "scripts.yaml").read_text())
        for source, name in product(
            ("chromecast", "pc", "projector"),
            ("vkliuchi_serialy", "vkliuchi_hromkast", "tv_pause", "tv_play"),
        ):
            with self.subTest(source=source, name=name):
                self.reset(("projector", "off", "on"))
                self.hass.states.async_set("input_text.tv_source", source)
                config = legacy[name]
                script = Script(
                    self.hass, cv.SCRIPT_SCHEMA(config["sequence"]), name, "script"
                )
                await script.async_run({}, context=Context())
                action = {"tv_pause": "media_pause", "tv_play": "media_play"}.get(
                    name, "turn_on"
                )
                target = "media_player.projector" if name == "vkliuchi_serialy" else TV
                self.assertEqual(
                    self.appliance_calls(),
                    [("media_player." + action, {"entity_id": [target]})],
                )
                self.assertEqual(
                    self.hass.states.get("input_select.universal_remote_pair").state,
                    "projector",
                )
                self.assertEqual(
                    self.hass.states.get("input_text.tv_source").state, source
                )

    async def test_legacy_music_switches_target_living_room_station_and_named_sources(
        self,
    ) -> None:
        legacy = yaml.safe_load((PACKAGE.parents[1] / "scripts.yaml").read_text())
        for name, source in [
            ("igraj_na_teleke", "Телевизор"),
            ("igraj_na_kolonke", "Станция"),
        ]:
            self.reset()
            config = legacy[name]
            script = Script(
                self.hass, cv.SCRIPT_SCHEMA(config["sequence"]), name, "script"
            )
            await script.async_run({}, context=Context())
            self.assertEqual(
                self.appliance_calls(),
                [
                    (
                        "media_player.select_source",
                        {
                            "entity_id": ["media_player.yandex_station_m0011hm00z86hg"],
                            "source": source,
                        },
                    ),
                ],
            )

    async def test_t9_cycles_are_serialized_text_edit_not_back_navigation(self) -> None:
        self.reset()
        self.set_power_pair("on", "off")
        await self.scripts["t9_send_char"].async_run(
            {"char": "b", "mode": "cycle", "pair": "tv", "native": False},
            context=Context(),
        )
        self.assertEqual(
            self.appliance_calls(),
            [
                (
                    "androidtv_remote.send_text",
                    {
                        "entity_id": ["remote.televizor"],
                        "operation": "replace_previous",
                        "text": "b",
                    },
                ),
            ],
        )

    async def test_explicit_audio_mode_keeps_music_ownership_during_av_playback(
        self,
    ) -> None:
        self.reset(("tv", "on", "off"))
        self.hass.states.async_set(TV, "playing")
        await self.run_automation("universal_remote_dispatcher", "play")
        commands = [
            data
            for action, data in self.calls
            if action == "script.music_remote_command"
            and data.get("command") not in {"beep_on", "beep_off"}
        ]
        self.assertEqual(commands, [{"command": "play_pause"}])
        self.assertEqual(
            [x for x in self.calls if x[0].startswith("media_player.")], []
        )

    async def test_reconnected_receiver_does_not_replay_recent_cached_button(
        self,
    ) -> None:
        now = dt_util.utcnow()
        for identity, button in [
            ("universal_remote_dispatcher", "smart"),
            ("universal_remote_prime", "prime"),
            ("universal_remote_disney", "disney"),
        ]:
            with self.subTest(automation=identity):
                self.reset()
                deadline = self.controller.mode.expires_at
                old = State(
                    "event.ekf_universal_remote",
                    "unavailable",
                    last_updated=now - timedelta(milliseconds=500),
                )
                current = State(
                    old.entity_id,
                    (now - timedelta(seconds=1)).isoformat(),
                    {
                        "event_type": button,
                        "last_received": now.timestamp() - 1,
                        "receiver_enabled": True,
                    },
                )
                await self.run_automation(
                    identity,
                    button,
                    trigger_override={
                        "platform": "state",
                        "entity_id": old.entity_id,
                        "from_state": old,
                        "to_state": current,
                    },
                )
                self.assertEqual(self.appliance_calls(), [])
                self.assertEqual(self.controller.mode.expires_at, deadline)

    def set_power_pair(self, tv, projector, **projector_attributes: Any) -> None:
        self.hass.states.async_set(
            TV,
            tv,
            {
                "power_intent_revision": "tv:0",
                "last_power_intent": None,
                "power_sync_target": None,
            },
        )
        self.hass.states.async_set(
            "media_player.projector",
            projector,
            {
                "power_intent_revision": "projector:0",
                "last_power_intent": None,
                "power_sync_target": None,
                **projector_attributes,
            },
        )

    def clear_explicit_focus(self) -> None:
        self.controller.mode.select("tv", explicit=False)
        self.controller.refresh()

    async def prepare_mode(self, button, **data: Any) -> Any:
        if "received_ms" in data:
            self.controller.physical_press(button, data["received_ms"] / 1000)
        result = await self.scripts["remote_modes_prepare"].async_run(
            {"button": button, **data}, context=Context()
        )
        return result.service_response

    async def test_automatic_route_follows_confirmed_screen_states(self) -> None:
        for tv, projector, expected in [
            ("off", "off", "tv"),
            ("off", "on", "projector"),
            ("on", "off", "tv"),
            ("on", "on", "tv"),
            ("unknown", "on", "tv"),
        ]:
            with self.subTest(tv=tv, projector=projector):
                self.reset()
                self.clear_explicit_focus()
                self.set_power_pair(tv, projector)
                self.assertEqual(
                    await self.prepare_mode("power_state"),
                    {"pair": expected, "native": False},
                )
                self.assertEqual(self.appliance_calls(), [])

    async def test_focus_expires_without_a_following_key_or_yaml_timer(self) -> None:
        self.reset()
        # Accelerate the real asyncio scheduler; deterministic unit tests enforce
        # the production duration of exactly 60 seconds and boundary cases.
        with patch.dict(
            self.controller.mode.select.__globals__, {"LEASE_SECONDS": 0.03}
        ):
            await self.scripts["remote_mode_select_pair"].async_run(
                {"pair": "projector", "native": True, "lease_seconds": 300},
                context=Context(),
            )
        await asyncio.sleep(0.08)
        await self.hass.async_block_till_done()
        self.assertEqual(self.controller.mode.target, "chromecast")
        self.assertEqual(
            self.hass.states.get("input_select.universal_remote_pair").state, "tv"
        )
        self.assertEqual(
            self.hass.states.get("input_boolean.universal_remote_native_target").state,
            "off",
        )
        self.assertEqual(self.appliance_calls(), [])

    async def test_selected_display_off_invalidates_focus_immediately(self) -> None:
        self.reset(("projector", "off", "on"))
        self.set_power_pair("on", "on")
        await self.prepare_mode("power_state")
        self.set_power_pair("on", "off")
        await self.hass.async_block_till_done()
        self.assertEqual(self.controller.mode.target, "chromecast")
        self.assertEqual(self.controller.mode.expires_at, 0)
        self.assertEqual(self.appliance_calls(), [])

    async def test_single_input_wakes_only_the_other_off_pair_with_sixty_second_focus(
        self,
    ) -> None:
        for tv, projector, expected, revision in [
            ("on", "off", PROJECTOR, "projector:0"),
            ("off", "on", TV, "tv:0"),
        ]:
            with self.subTest(tv=tv, projector=projector):
                self.reset()
                self.clear_explicit_focus()
                self.set_power_pair(tv, projector)
                await self.prepare_mode("input")
                route = await self.prepare_mode("up")
                await self.hass.async_block_till_done()
                selected = "projector" if tv == "on" else "tv"
                self.assertEqual(
                    route,
                    {"pair": selected, "native": False, "blocked": f"{selected}_off"},
                )
                self.assertEqual(
                    self.appliance_calls(),
                    [
                        (
                            "ekf_appliances.wake_if_current",
                            {"entity_id": expected, "expected_revision": revision},
                        )
                    ],
                )
                self.assertLessEqual(self.controller.mode.expires_at - time.time(), 60)
                self.assertEqual(
                    self.hass.states.get(
                        "input_text.universal_remote_focus_seconds"
                    ).state,
                    "60",
                )

    async def test_pending_boot_does_not_extend_expired_focus(self) -> None:
        self.reset(("projector", "off", "off"))
        self.set_power_pair("on", "off", power_sync_target=True)
        self.controller.mode.expires_at = time.time() - 1
        self.assertEqual(
            await self.prepare_mode("power_state"), {"pair": "tv", "native": False}
        )
        self.assertEqual(self.appliance_calls(), [])

    async def test_logical_press_rejections_cue_once_and_preserve_fast_taps(
        self,
    ) -> None:
        self.reset()
        played = []

        async def play(item) -> CueResult:
            played.append(item.press_id)
            return CueResult("completed")

        queue = RejectionQueue(play, report=lambda *_: None)

        async def reject(call) -> None:
            queue.submit(call.data["reason"], call.data.get("press_id"))

        self.hass.services.async_register("remote_announcements", "reject", reject)
        mapper = RemoteMapper()
        now = time.time()
        try:
            for command, offset, repeat in [
                (0xEA15, 0.0, False),
                (0xEA15, 0.108, True),
                (0xEA15, 0.216, True),
                (0xE916, 1.0, False),
                (0xE916, 1.220, True),
                (0xE916, 1.300, False),
            ]:
                mapper.feed(
                    [
                        {
                            "address": 0xBF00,
                            "command": command,
                            "repeat": repeat,
                            "offset": 0,
                        }
                    ],
                    now + offset,
                )
                for (button, captured_at), identity in zip(
                    mapper.event_times, mapper.event_press_ids, strict=True
                ):
                    self.controller.physical_press(
                        button, captured_at, press_id=f"Odroid:{identity}"
                    )
                    route = await self.controller.async_prepare(
                        {"button": button, "received_ms": captured_at * 1000}
                    )
                    self.assertEqual(route["blocked"], "tv_off")
            await queue.drain()
            self.assertEqual(len(played), 3)
            self.assertEqual(queue.counts["duplicates"], 2)
            self.assertEqual(self.appliance_calls(), [])
        finally:
            await queue.stop(0.1)

    async def test_voice_search_rejects_off_unknown_and_transitioning_pairs_once(
        self,
    ) -> None:
        for pair, state, target in product(
            ("tv", "projector"),
            ("off", "unknown", "on"),
            (None, True, False),
        ):
            if state == "on" and target is None:
                continue
            with self.subTest(pair=pair, state=state, target=target):
                self.reset((pair, "off", "off"))
                self.hass.states.async_set(
                    TV if pair == "tv" else PROJECTOR,
                    state,
                    {"power_sync_target": target},
                )
                result = await self.scripts["tv_search"].async_run(
                    {"pair": pair, "query": "Interstellar"}, context=Context()
                )
                self.assertIn("blocked", result.service_response)
                self.assertEqual(self.appliance_calls(), [])
                self.assertEqual(
                    len(
                        [
                            call
                            for call in self.calls
                            if call[0] == "remote_announcements.reject"
                        ]
                    ),
                    1,
                )

    async def test_voice_search_stops_if_power_changes_after_opening_search(
        self,
    ) -> None:
        self.reset()
        self.set_power_pair("on", "off")

        async def turn_off_after_search(call) -> None:
            await self.capture(call)
            self.hass.states.async_set(TV, "on", {"power_sync_target": False})

        self.hass.services.async_register(
            "remote", "send_command", turn_off_after_search
        )
        result = await self.scripts["tv_search"].async_run(
            {"query": "Interstellar"}, context=Context()
        )
        self.assertEqual(result.service_response["blocked"], "tv_power_transition")
        self.assertEqual(
            [data["command"] for _, data in self.appliance_calls()],
            ["KEYCODE_SEARCH"],
        )
        self.assertEqual(
            len(
                [
                    call
                    for call in self.calls
                    if call[0] == "remote_announcements.reject"
                ]
            ),
            1,
        )

    async def test_voice_search_preserves_the_three_native_keys_when_tv_is_ready(
        self,
    ) -> None:
        self.reset()
        self.set_power_pair("on", "off")
        await self.scripts["tv_search"].async_run(
            {"query": "Interstellar"}, context=Context()
        )
        self.assertEqual(
            [data["command"] for _, data in self.appliance_calls()],
            ["KEYCODE_SEARCH", "text:Interstellar", "KEYCODE_ENTER"],
        )
        self.assertFalse(
            any(call[0] == "remote_announcements.reject" for call in self.calls)
        )

    async def test_shutdown_blocks_disney_and_legacy_projector_entry_paths(
        self,
    ) -> None:
        self.reset(("projector", "off", "off"))
        self.set_power_pair("off", "on")
        self.runtime.note_projector_off_requested()
        for name, data in [
            ("source_to_pc", {}),
            ("source_to_projector", {}),
            ("kodi_play_next_episode", {}),
            ("t9_kodi_text", {"mode": "add", "char": "a"}),
        ]:
            await self.scripts[name].async_run(data, context=Context())
        for button in ("disney", "disney_hold"):
            await self.run_automation("universal_remote_disney", button)
        await self.hass.async_block_till_done()
        self.assertEqual(self.appliance_calls(), [])
        self.assertEqual(self.controller.mode.target, "chromecast")

    async def test_shutdown_fallback_does_not_wake_the_other_display_with_navigation(
        self,
    ) -> None:
        for button in ("back", "up", "ok_hold"):
            with self.subTest(button=button):
                self.reset(("projector", "off", "on"))
                self.set_power_pair("off", "on")
                self.runtime.note_projector_off_requested()
                await self.run_automation("universal_remote_dispatcher", button)
                self.assertEqual(self.appliance_calls(), [])
                self.assertEqual(self.controller.mode.target, "chromecast")

    async def test_native_kodi_unsupported_response_keeps_error_and_one_cue(
        self,
    ) -> None:
        self.reset(("projector", "off", "off"))
        self.set_power_pair("off", "on")
        # Installed HA 2026.9.3 registers Kodi call_method with the default
        # SupportsResponse.NONE. Its event result is not a service response.
        self.hass.services.async_register("kodi", "call_method", self.capture)
        with self.assertRaises(HomeAssistantError) as raised:
            await self.scripts["kodi_play_next_episode"].async_run(
                {}, context=Context()
            )
        self.assertIn("response", str(raised.exception).lower())
        self.assertEqual(self.appliance_calls(), [])
        cues = [call for call in self.calls if call[0] == "remote_announcements.reject"]
        self.assertEqual(len(cues), 1, cues)
        self.assertEqual(cues[0][1]["reason"], "command_failed")

    async def test_captured_projector_text_edit_never_crosses_to_on_tv_fallback(
        self,
    ) -> None:
        for name in ("t9_backspace", "t9_commit"):
            with self.subTest(script=name):
                self.reset(("projector", "off", "off"))
                self.set_power_pair("on", "on")
                self.runtime.note_projector_off_requested()
                await self.scripts[name].async_run(
                    {"pair": "projector", "native": False}, context=Context()
                )
                self.assertEqual(self.appliance_calls(), [])
                cues = [
                    call
                    for call in self.calls
                    if call[0] == "remote_announcements.reject"
                ]
                self.assertEqual(len(cues), 1, cues)
                self.assertEqual(cues[0][1]["reason"], "projector_shutting_down")

    async def test_shutdown_between_resolve_and_dispatch_drops_the_actual_command(
        self,
    ) -> None:
        async def shutdown_before_dispatch(call) -> Any:
            self.runtime.note_projector_off_requested()
            return await self.controller.async_dispatch(call.data)

        self.hass.services.async_register(
            "ekf_appliances",
            "remote_dispatch",
            shutdown_before_dispatch,
            supports_response=SupportsResponse.OPTIONAL,
        )
        for name, data in [
            ("remote_nav", {"direction": "up"}),
            ("remote_volume", {"command": "volume_up"}),
            ("remote_media", {"action": "play_pause"}),
        ]:
            with self.subTest(script=name):
                self.reset(("projector", "off", "off"))
                self.set_power_pair("off", "on")
                await self.scripts[name].async_run(
                    {"pair": "projector", **data}, context=Context()
                )
                self.assertTrue(self.runtime.projector_shutting_down)
                self.assertEqual(self.appliance_calls(), [])

    async def test_queued_kodi_text_cancels_waiting_first_and_blocks_second_write(
        self,
    ) -> None:
        self.reset(("projector", "off", "off"))
        self.set_power_pair("off", "on")
        entered, release = asyncio.Event(), asyncio.Event()

        async def pending_text(call) -> Any:
            entered.set()
            await release.wait()
            self.calls.append(("rest_command.host_kodi_text_input", dict(call.data)))
            return {"status": 200, "content": {"ok": True, "verified": True}}

        self.hass.services.async_register(
            "rest_command",
            "host_kodi_text_input",
            pending_text,
            supports_response=SupportsResponse.OPTIONAL,
        )
        first = asyncio.create_task(
            self.scripts["t9_kodi_text"].async_run(
                {"mode": "add", "char": "a"}, context=Context()
            )
        )
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            second = asyncio.create_task(
                self.scripts["t9_kodi_text"].async_run(
                    {"mode": "add", "char": "b"}, context=Context()
                )
            )
            await asyncio.sleep(0)
            self.runtime.note_projector_off_requested()
            release.set()
            first_result, second_result = await asyncio.wait_for(
                asyncio.gather(first, second), 0.5
            )
            self.assertEqual(
                first_result.service_response,
                {"sent": False, "blocked": "projector_shutting_down"},
            )
            self.assertEqual(
                second_result.service_response,
                {"sent": False, "blocked": "projector_shutting_down"},
            )
            self.assertEqual(self.appliance_calls(), [])
        finally:
            release.set()
            await asyncio.gather(
                *[task for task in (first, second) if task is not None],
                return_exceptions=True,
            )

    async def test_kodi_library_reads_preserve_native_responses_and_open_the_episode(
        self,
    ) -> None:
        self.reset(("projector", "off", "off"))
        self.set_power_pair("off", "on")
        self.kodi_responses = {
            "VideoLibrary.GetInProgressTVShows": {"tvshows": [{"tvshowid": 3}]},
            "VideoLibrary.GetEpisodes": {
                "episodes": [{"episodeid": 7, "playcount": 0}]
            },
        }
        await self.scripts["kodi_play_next_episode"].async_run({}, context=Context())
        calls = self.appliance_calls()
        self.assertEqual(
            [data["method"] for _, data in calls],
            [
                "VideoLibrary.GetInProgressTVShows",
                "VideoLibrary.GetEpisodes",
                "Player.Open",
            ],
        )
        self.assertEqual(calls[-1][1]["item"], {"episodeid": 7})

    async def test_kodi_text_bridge_response_round_trips_to_the_caller(self) -> None:
        self.reset(("projector", "off", "off"))
        self.set_power_pair("off", "on")
        result = await self.scripts["t9_kodi_text"].async_run(
            {"mode": "cycle", "char": "b"}, context=Context()
        )
        self.assertEqual(
            result.service_response,
            {"status": 200, "content": {"ok": True, "verified": True}},
        )
        self.assertEqual(
            self.appliance_calls(),
            [("rest_command.host_kodi_text_input", {"action": "cycle", "char": "b"})],
        )

    async def test_audio_helper_changes_update_the_canonical_target_without_a_key(
        self,
    ) -> None:
        self.reset(("projector", "off", "off"))
        await self.scripts["audio_mode_activate"].async_run({}, context=Context())
        await self.hass.async_block_till_done()
        self.assertEqual(self.controller.mode.target, "speaker")
        await self.scripts["audio_mode_deactivate"].async_run({}, context=Context())
        await self.hass.async_block_till_done()
        self.assertEqual(self.controller.mode.target, "kodi")

    async def prepare_target_announcements(self) -> Any:
        """Attach the real target sensor and announcement manager to this HA loop."""
        self.hass.config_entries = ConfigEntries(self.hass, {})
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        entry = SimpleNamespace(
            entry_id="remote-fixture",
            runtime_data=SimpleNamespace(remote_controller=self.controller),
        )
        component = EntityComponent(logging.getLogger(__name__), "sensor", self.hass)
        await component.async_add_entities([sensor_module.UniversalRemoteTarget(entry)])
        config = remote_announcements.CONFIG_SCHEMA(
            yaml.safe_load((PACKAGE.parent / "remote_announcements.yaml").read_text())
        )[remote_announcements.DOMAIN]
        self.hass.states.async_set(config["backend_entity"], "Яндекс")
        self.hass.states.async_set(
            config["yandex_entity"],
            "idle",
            {
                "volume_level": 0.5,
                "is_volume_muted": False,
                "alice_state": "IDLE",
            },
        )
        manager = remote_announcements.Manager(self.hass, config)

        async def generate(_text) -> Any:
            return b"nonplayed-cached-fixture", 0.01

        await manager.cache.prepare(generate)
        manager.settings = {
            "dialogs": {
                "skill_id": "11111111-1111-1111-1111-111111111111",
                "sounds": {
                    clip.key: f"22222222-2222-2222-2222-{index:012d}"
                    for index, clip in enumerate(manager.cache.clips.values())
                },
            }
        }
        manager.gate.changed(self.hass.states.get(config["target_entity"]).state)
        unsubscribe = async_track_state_change_event(
            self.hass,
            [config["target_entity"]],
            manager.target_changed,
        )

        async def speak(call: ServiceCall) -> dict[str, str]:
            self.assertTrue(call.return_response)
            self.calls.append(("yandex_station.send_command", dict(call.data)))
            entity = call.data["entity_id"]
            state = self.hass.states.get(entity)
            attrs = dict(state.attributes)
            self.hass.states.async_set(
                entity, state.state, {**attrs, "alice_state": "SPEAKING"}
            )
            await asyncio.sleep(0)
            self.hass.states.async_set(
                entity, state.state, {**attrs, "alice_state": "IDLE"}
            )
            return {"status": "SUCCESS"}

        self.hass.services.async_register(
            "yandex_station",
            "send_command",
            speak,
            supports_response=SupportsResponse.OPTIONAL,
        )
        return manager, component, unsubscribe

    async def test_audio_transitions_only_announce_the_committed_target(self) -> None:
        self.reset(("projector", "off", "off"))
        manager, component, unsubscribe = await self.prepare_target_announcements()
        try:
            for script, target in (
                ("audio_mode_activate", "speaker"),
                ("audio_mode_deactivate", "kodi"),
            ):
                self.calls.clear()
                await self.scripts[script].async_run({}, context=Context())
                await self.hass.async_block_till_done()
                await manager.queue.drain()
                state = self.hass.states.get(controller_module.TARGET)
                self.assertEqual(state.state, target)
                self.assertTrue(state.attributes["initialized"])
                speech = [
                    data
                    for name, data in self.calls
                    if name == "yandex_station.send_command"
                ]
                self.assertEqual(len(speech), 1)
                sound = manager.settings["dialogs"]["sounds"][
                    manager.cache.get(target).key
                ]
                expected_tag = (
                    '<speaker audio="dialogs-upload/'
                    f'11111111-1111-1111-1111-111111111111/{sound}.opus">'
                )
                self.assertEqual(speech[0]["command"], "serverAction")
                slots = speech[0]["serverActionEventPayload"]["payload"]["form_update"][
                    "slots"
                ]
                self.assertEqual(
                    slots,
                    [
                        {
                            "type": "string",
                            "name": "phrase_to_repeat",
                            "value": expected_tag,
                        }
                    ],
                )
                self.assertEqual(manager.status, "ready")
                self.assertEqual(
                    [
                        call
                        for call in self.calls
                        if call[0]
                        in {
                            "script.music_remote_command",
                            "aisis_speaker.send_directive",
                        }
                    ],
                    [],
                )
        finally:
            unsubscribe()
            await manager.close()
            await component.async_remove_entity(controller_module.TARGET)

    async def test_single_audio_press_preserves_normal_play_pause(self) -> None:
        self.reset()
        await self.scripts["audio_mode_handle_press"].async_run({}, context=Context())
        self.assertEqual(self.hass.states.get("input_boolean.audio_mode").state, "off")
        self.assertEqual(
            [call for call in self.calls if call[0] == "script.music_remote_command"],
            [
                ("script.music_remote_command", {"command": "play_pause"}),
            ],
        )

    async def test_repeated_power_is_rejected_during_each_display_transition(
        self,
    ) -> None:
        for pair, initial in product(("tv", "projector"), ("off", "on")):
            with self.subTest(pair=pair, initial=initial):
                self.reset((pair, "off", "off"))
                self.set_power_pair(
                    initial if pair == "tv" else "off",
                    initial if pair == "projector" else "off",
                )
                entered, release = asyncio.Event(), asyncio.Event()

                async def pending_power(call, entered=entered, release=release) -> None:
                    self.calls.append((f"media_player.{call.service}", dict(call.data)))
                    entered.set()
                    await release.wait()

                service = (
                    "toggle"
                    if pair == "tv"
                    else "turn_off"
                    if initial == "on"
                    else "turn_on"
                )
                self.hass.services.async_register(
                    "media_player", service, pending_power
                )
                first = asyncio.create_task(
                    self.run_automation("universal_remote_dispatcher", "power")
                )
                try:
                    await asyncio.wait_for(entered.wait(), 0.5)
                    await self.run_automation("universal_remote_dispatcher", "power")
                    self.assertEqual(len(self.appliance_calls()), 1)
                    cues = [
                        call
                        for call in self.calls
                        if call[0] == "remote_announcements.reject"
                    ]
                    self.assertEqual(len(cues), 1)
                    self.assertFalse(first.done())
                    if pair == "projector" and initial == "on":
                        self.assertEqual(self.controller.mode.target, "chromecast")
                        self.assertEqual(self.hass.states.get(TV).state, "off")
                finally:
                    release.set()
                    await first
                    self.hass.services.async_register(
                        "media_player", service, self.capture
                    )

    async def test_off_display_text_is_rejected_once_before_native_dispatch(
        self,
    ) -> None:
        for pair in ("tv", "projector"):
            with self.subTest(pair=pair):
                self.reset((pair, "off", "off"))
                await self.scripts["t9_send_char"].async_run(
                    {
                        "pair": pair,
                        "native": False,
                        "mode": "add",
                        "char": "a",
                        "digit": "2",
                    },
                    context=Context(),
                )
                self.assertEqual(self.appliance_calls(), [])
                self.assertEqual(
                    len(
                        [
                            call
                            for call in self.calls
                            if call[0] == "remote_announcements.reject"
                        ]
                    ),
                    1,
                )

    async def test_rejected_kodi_library_request_does_not_fall_back_to_play(
        self,
    ) -> None:
        self.reset(("projector", "off", "off"))
        await self.scripts["kodi_play_next_episode"].async_run({}, context=Context())
        self.assertEqual(self.appliance_calls(), [])
        cues = [call for call in self.calls if call[0] == "remote_announcements.reject"]
        self.assertEqual(len(cues), 1, cues)

    async def test_disney_hold_stops_after_one_busy_source_rejection(self) -> None:
        self.reset(("projector", "off", "off"))
        self.hass.states.async_set(
            PROJECTOR,
            "off",
            {
                "power_intent_revision": "projector:1",
                "last_power_intent": True,
                "power_sync_target": True,
            },
        )
        await self.run_automation("universal_remote_disney", "disney_hold")
        self.assertEqual(self.appliance_calls(), [])
        self.assertEqual(
            len(
                [
                    call
                    for call in self.calls
                    if call[0] == "remote_announcements.reject"
                ]
            ),
            1,
        )

    async def test_pending_power_does_not_block_smart_ac_or_input(self) -> None:
        self.reset()
        self.set_power_pair("on", "on")
        entered, release = asyncio.Event(), asyncio.Event()

        async def pending_power(call) -> None:
            self.calls.append(("media_player.toggle", dict(call.data)))
            entered.set()
            await release.wait()

        self.hass.services.async_register("media_player", "toggle", pending_power)
        power = asyncio.create_task(
            self.run_automation("universal_remote_dispatcher", "power")
        )
        try:
            await asyncio.wait_for(entered.wait(), 0.3)
            for button in ("smart", "channel_up", "input", "up"):
                await asyncio.wait_for(
                    self.run_automation("universal_remote_dispatcher", button), 0.3
                )
            self.assertFalse(power.done())
            calls = self.appliance_calls()
            self.assertIn(("light.toggle", {"entity_id": [GLOBUS]}), calls)
            self.assertIn(
                ("climate.set_temperature", {"entity_id": [AC], "temperature": 25}),
                calls,
            )
            self.assertIn(
                (
                    "kodi.call_method",
                    {"entity_id": "media_player.home", "method": "Input.Up"},
                ),
                calls,
            )
            self.assertEqual(
                [x for x in calls if x[0].startswith("media_player.")],
                [("media_player.toggle", {"entity_id": TV})],
            )
        finally:
            release.set()
            await asyncio.wait_for(power, 0.5)

    async def test_non_mapping_legacy_helpers_do_not_disable_global_buttons(
        self,
    ) -> None:
        for corrupt in ("null", "[]", "true", "1", '"text"'):
            with self.subTest(helper_value=corrupt):
                self.reset()
                self.hass.states.async_set(
                    "input_text.universal_remote_wake_context", corrupt
                )
                self.hass.states.async_set(
                    "input_text.universal_remote_input_context", corrupt
                )
                await self.run_automation("universal_remote_dispatcher", "smart")
                self.assertEqual(
                    self.appliance_calls(), [("light.toggle", {"entity_id": [GLOBUS]})]
                )

    async def test_repeated_double_input_restores_player_target_without_power(
        self,
    ) -> None:
        for pair in ("tv", "projector"):
            with self.subTest(pair=pair):
                self.reset((pair, "off", "off"))
                self.set_power_pair("on", "on")
                first = time.time() * 1000
                for delta in (0, 400, 1500, 1700):
                    await self.prepare_mode("input", received_ms=first + delta)
                self.assertEqual(
                    self.controller.mode.resolve(), {"pair": pair, "native": False}
                )
                self.assertEqual(self.appliance_calls(), [])


if __name__ == "__main__":
    unittest.main()
