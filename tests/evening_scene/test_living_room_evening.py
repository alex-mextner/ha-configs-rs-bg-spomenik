"""Execute the real Evening YAML at music and named-child failure boundaries."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from homeassistant import loader
from homeassistant.core import (
    Context,
    HomeAssistant,
    ServiceCall,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import frame
from homeassistant.helpers.script import Script, script_stack_cv
from homeassistant.helpers.trace import trace_clear, trace_get

ROOT = Path(__file__).resolve().parents[2]
STATION = "media_player.yandex_station_m0011hm00z86hg"
FIREPLACE = "sensor.living_room_fireplace"
WEATHER = "weather.forecast_home_assistant"
MUSIC_ACTIONS = (
    "media_player.select_source",
    "media_player.volume_set",
    "media_player.play_media",
)
SUCCESS_RESPONSE = "{{ {'ok': true, 'request_id': request_id} }}"
EVENT_TIMEOUT = 2.0
QUEUE_LIMIT = 3


class LivingRoomEveningTests(unittest.IsolatedAsyncioTestCase):
    """Keep the Script engine real and replace only physical service boundaries."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.scripts: list[Script] = []
        self.tasks: list[asyncio.Task[Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.child_calls: list[dict[str, Any]] = []
        self.accepted_ids: list[str] = []
        self.completed: list[bool] = []
        self.last_trace: list[dict[str, Any]] = []
        self.music_failure_action: str | None = None
        self.music_failure: Exception | None = None
        self.child_failure: Exception | None = None
        self.sensor_state = "playing"
        self.sensor_request_override: str | None = None
        self.fireplace_entered = asyncio.Event()
        self.fireplace_release = asyncio.Event()
        self.fireplace_release.set()
        self.music_failed = asyncio.Event()
        self.queue_full = asyncio.Event()
        self.hass.states.async_set(
            FIREPLACE, "playing", {"request_id": "previous-command"}
        )
        self.hass.states.async_set(
            WEATHER, "partlycloudy", {"temperature": 9.5, "temperature_unit": "°C"}
        )
        for action in (
            *MUSIC_ACTIONS,
            "light.turn_on",
            "climate.set_temperature",
            "test.fireplace_boundary",
            "test.evening_completed",
        ):
            domain, service = action.split(".")
            self.hass.services.async_register(domain, service, self.boundary)
        self.register_child(self.child_sequence())
        package = yaml.safe_load(
            (ROOT / "packages/living_room_evening.yaml").read_text()
        )
        config = package["script"]["living_room_evening"]
        sequence = deepcopy(config["sequence"])
        sequence.append({"action": "test.evening_completed"})
        self.register_script("living_room_evening", sequence, mode=config["mode"])

    async def asyncTearDown(self) -> None:
        self.fireplace_release.set()
        for script in self.scripts:
            await script.async_stop()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.hass.async_stop()
        self.temp.cleanup()

    @callback
    def queue_changed(self) -> None:
        if self.child.runs == QUEUE_LIMIT:
            self.queue_full.set()

    def register_script(
        self, name: str, sequence: list[dict[str, Any]], *, mode: str = "single"
    ) -> Script:
        script = Script(
            self.hass,
            cv.SCRIPT_SCHEMA(deepcopy(sequence)),
            name,
            "script",
            script_mode=mode,
            max_runs=QUEUE_LIMIT,
            change_listener=self.queue_changed if mode == "queued" else None,
        )
        self.scripts.append(script)

        async def run(call: ServiceCall) -> dict[str, Any] | None:
            if name == "living_room_fireplace":
                self.child_calls.append(deepcopy(dict(call.data)))
            if stack := script_stack_cv.get():
                script_stack_cv.set(stack.copy())
            result = await script.async_run(dict(call.data), context=call.context)
            response = result.service_response if result else None
            # Match ScriptEntity._service_handler, including silent abort/rejection.
            return (response or {}) if call.return_response else None

        self.hass.services.async_register(
            "script", name, run, supports_response=SupportsResponse.OPTIONAL
        )
        return script

    def register_child(self, sequence: list[dict[str, Any]]) -> None:
        self.child = self.register_script(
            "living_room_fireplace", sequence, mode="queued"
        )

    @staticmethod
    def child_sequence(response: str | None = SUCCESS_RESPONSE) -> list[dict[str, Any]]:
        sequence: list[dict[str, Any]] = [
            {
                "action": "test.fireplace_boundary",
                "data": {"request_id": "{{ request_id }}"},
            }
        ]
        if response is not None:
            sequence.extend(
                [
                    {"variables": {"fireplace_success": response}},
                    {
                        "stop": "Fixture fireplace confirmed",
                        "response_variable": "fireplace_success",
                    },
                ]
            )
        return sequence

    async def boundary(self, call: ServiceCall) -> None:
        action = f"{call.domain}.{call.service}"
        self.calls.append((action, deepcopy(dict(call.data))))
        if action == self.music_failure_action:
            self.music_failed.set()
            failure = self.music_failure
            if failure is None:
                self.fail("Music failure fixture is missing its exception")
            raise failure
        if action == "test.fireplace_boundary":
            request_id = call.data["request_id"]
            self.accepted_ids.append(request_id)
            self.fireplace_entered.set()
            await self.fireplace_release.wait()
            if self.child_failure is not None:
                raise self.child_failure
            self.hass.states.async_set(
                FIREPLACE,
                self.sensor_state,
                {"request_id": self.sensor_request_override or request_id},
            )
        if action == "test.evening_completed":
            self.completed.append(True)

    async def launch(self) -> None:
        trace_clear()
        self.completed.clear()
        try:
            await self.hass.services.async_call(
                "script", "living_room_evening", blocking=True, context=Context()
            )
        finally:
            # Trace ContextVars must be captured in this task, not its awaiting parent.
            self.last_trace = [
                item.as_dict()
                for group in (trace_get(clear=False) or {}).values()
                for item in group
            ]

    def music_calls(self) -> list[tuple[str, dict[str, Any]]]:
        return [call for call in self.calls if call[0] in MUSIC_ACTIONS]

    def assert_matching_playback_sensor(self) -> None:
        self.assertEqual(len(self.accepted_ids), 1)
        request_id = self.child_calls[-1]["request_id"]
        self.assertIsInstance(request_id, str)
        self.assertTrue(request_id.startswith("evening-"))
        self.assertEqual(self.accepted_ids, [request_id])
        state = self.hass.states.get(FIREPLACE)
        self.assertEqual(state.state, "playing")
        self.assertEqual(state.attributes["request_id"], request_id)

    def assert_rejected(self) -> None:
        self.assertEqual(self.completed, [])
        self.assertTrue(
            any(
                node.get("result", {}).get("error") is True for node in self.last_trace
            ),
            "Evening did not reach an actual HA error stop",
        )

    async def test_success_preserves_room_settings_and_orders_station_music(
        self,
    ) -> None:
        await self.launch()
        self.assertEqual(
            self.calls[:3],
            [
                ("light.turn_on", {"entity_id": ["light.girlianda"]}),
                (
                    "climate.set_temperature",
                    {
                        "entity_id": ["climate.kondei_1"],
                        "hvac_mode": "heat",
                        "temperature": 30,
                    },
                ),
                ("light.turn_on", {"entity_id": ["light.gostinaia_globus_socket_1"]}),
            ],
        )
        self.assertEqual(
            self.music_calls(),
            [
                (MUSIC_ACTIONS[0], {"entity_id": [STATION], "source": "Станция"}),
                (MUSIC_ACTIONS[1], {"entity_id": [STATION], "volume_level": 0.4}),
                (
                    MUSIC_ACTIONS[2],
                    {
                        "entity_id": [STATION],
                        "media_content_type": "command",
                        "media_content_id": "Включи спокойный лёгкий джаз",
                    },
                ),
            ],
        )
        self.assert_matching_playback_sensor()
        self.assertEqual(self.completed, [True])
        self.assertEqual(self.calls[-1][0], "test.evening_completed")

    async def test_outdoor_temperature_selects_heat_or_cool(self) -> None:
        for temperature, unit, mode, target in (
            (-5, "°C", "heat", 30),
            (23.9, "°C", "heat", 30),
            (24.1, "°C", "cool", 16),
            (35, "°C", "cool", 16),
            ("9.5", "°C", "heat", 30),
            (75.1, "°F", "heat", 30),
            (75.3, "°F", "cool", 16),
        ):
            with self.subTest(temperature=temperature, unit=unit):
                self.calls.clear()
                self.hass.states.async_set(
                    WEATHER,
                    "partlycloudy",
                    {"temperature": temperature, "temperature_unit": unit},
                )
                await self.launch()
                self.assertEqual(
                    [
                        data
                        for action, data in self.calls
                        if action == "climate.set_temperature"
                    ],
                    [
                        {
                            "entity_id": ["climate.kondei_1"],
                            "hvac_mode": mode,
                            "temperature": target,
                        }
                    ],
                )
                self.assertEqual(self.completed, [True])

    async def test_threshold_or_invalid_weather_preserves_climate(self) -> None:
        for state, temperature, unit in (
            ("partlycloudy", 24, "°C"),
            ("partlycloudy", 75.2, "°F"),
            ("unavailable", 9.5, "°C"),
            ("unknown", 28, "°C"),
            ("partlycloudy", None, "°C"),
            ("partlycloudy", "invalid", "°C"),
            ("partlycloudy", True, "°C"),
            ("partlycloudy", float("nan"), "°C"),
            ("partlycloudy", float("inf"), "°C"),
            ("partlycloudy", 9.5, "K"),
            ("partlycloudy", 9.5, None),
        ):
            with self.subTest(state=state, temperature=temperature, unit=unit):
                self.calls.clear()
                self.hass.states.async_set(
                    WEATHER,
                    state,
                    {"temperature": temperature, "temperature_unit": unit},
                )
                await self.launch()
                self.assertEqual(
                    [call for call in self.calls if call[0].startswith("climate.")], []
                )
                self.assertEqual(
                    [
                        data["entity_id"]
                        for action, data in self.calls
                        if action == "light.turn_on"
                    ],
                    [["light.girlianda"], ["light.gostinaia_globus_socket_1"]],
                )
                self.assertEqual(len(self.music_calls()), 3)
                self.assertEqual(self.completed, [True])
                self.assertEqual(
                    self.hass.states.get(FIREPLACE).attributes["request_id"],
                    self.child_calls[-1]["request_id"],
                )

    async def test_raw_music_error_waits_for_the_other_branch_to_finish(self) -> None:
        self.fireplace_release.clear()
        self.music_failure_action = MUSIC_ACTIONS[0]
        self.music_failure = RuntimeError("Music transport broke")
        operation = asyncio.create_task(self.launch())
        self.tasks.append(operation)
        try:
            await asyncio.wait_for(self.music_failed.wait(), EVENT_TIMEOUT)
            await asyncio.wait_for(self.fireplace_entered.wait(), EVENT_TIMEOUT)
            self.assertFalse(operation.done(), "Music failure cancelled the fireplace")
            self.fireplace_release.set()
            with self.assertRaisesRegex(RuntimeError, "Music transport broke"):
                await operation
            self.assert_matching_playback_sensor()
            self.assertEqual(self.completed, [])
        finally:
            self.fireplace_release.set()

    async def test_volume_failure_prevents_play_but_still_finishes_fireplace(
        self,
    ) -> None:
        self.music_failure_action = MUSIC_ACTIONS[1]
        self.music_failure = HomeAssistantError("Volume was not confirmed")
        with self.assertRaisesRegex(HomeAssistantError, "Volume was not confirmed"):
            await self.launch()
        self.assertEqual(
            [action for action, _ in self.music_calls()], list(MUSIC_ACTIONS[:2])
        )
        self.assert_matching_playback_sensor()

    async def test_missing_music_service_still_finishes_fireplace(self) -> None:
        self.hass.services.async_remove("media_player", "select_source")
        with self.assertRaises(ServiceNotFound):
            await self.launch()
        self.assertEqual(self.music_calls(), [])
        self.assert_matching_playback_sensor()

    async def test_child_error_stop_after_matching_playing_sensor_is_rejected(
        self,
    ) -> None:
        sequence = self.child_sequence(None)
        sequence.append({"stop": "Native playback was not confirmed", "error": True})
        self.register_child(sequence)
        await self.launch()
        self.assert_matching_playback_sensor()
        self.assert_rejected()

    async def test_child_no_op_cannot_reuse_a_previous_playing_sensor(self) -> None:
        self.register_child([{"stop": "No child run admitted"}])
        await self.launch()
        self.assertEqual(self.accepted_ids, [])
        self.assertEqual(
            self.hass.states.get(FIREPLACE).attributes["request_id"], "previous-command"
        )
        self.assert_rejected()

    async def test_actual_full_queue_rejection_is_not_fireplace_success(self) -> None:
        self.fireplace_release.clear()
        for number in range(QUEUE_LIMIT):
            task = asyncio.create_task(
                self.hass.services.async_call(
                    "script",
                    "living_room_fireplace",
                    {"request_id": f"busy-{number}"},
                    blocking=True,
                    context=Context(),
                )
            )
            self.tasks.append(task)
        try:
            await asyncio.wait_for(self.fireplace_entered.wait(), EVENT_TIMEOUT)
            await asyncio.wait_for(self.queue_full.wait(), EVENT_TIMEOUT)
            await self.launch()
            self.assertEqual(self.child.runs, QUEUE_LIMIT)
            self.assertEqual(self.accepted_ids, ["busy-0"])
            self.assertTrue(self.child_calls[-1]["request_id"].startswith("evening-"))
            self.assert_rejected()
        finally:
            self.fireplace_release.set()

    async def test_raw_child_exception_surfaces_after_music_branch_finishes(
        self,
    ) -> None:
        self.child_failure = HomeAssistantError("Fireplace host failed")
        with self.assertRaisesRegex(HomeAssistantError, "Fireplace host failed"):
            await self.launch()
        self.assertEqual(
            [action for action, _ in self.music_calls()], list(MUSIC_ACTIONS)
        )
        self.assertEqual(self.completed, [])

    async def test_missing_wrong_or_negative_child_receipt_is_rejected(self) -> None:
        for response in (
            None,
            "{{ {'ok': true, 'request_id': 'wrong-request'} }}",
            "{{ {'ok': false, 'request_id': request_id} }}",
            "{{ {'ok': true} }}",
        ):
            with self.subTest(response=response):
                self.register_child(self.child_sequence(response))
                await self.launch()
                self.assert_rejected()

    async def test_success_receipt_requires_matching_playing_sensor(self) -> None:
        for state, request in (("idle", None), ("playing", "previous-command")):
            with self.subTest(state=state, request=request):
                self.sensor_state, self.sensor_request_override = state, request
                await self.launch()
                self.assert_rejected()
