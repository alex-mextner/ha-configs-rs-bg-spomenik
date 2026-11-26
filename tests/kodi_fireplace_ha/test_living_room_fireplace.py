"""Run the actual fireplace YAML through HA Script at its power/playback boundaries."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from homeassistant import loader
from homeassistant.core import Context, HomeAssistant, State, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import frame
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.script import Script, script_stack_cv
from homeassistant.helpers.template import Template
from homeassistant.helpers.trace import trace_clear, trace_get
from homeassistant.util import dt as dt_util

ROOT = Path(__file__).resolve().parents[2]
PROJECTOR = "media_player.projector"
KODI = "media_player.home"
READINESS = "sensor.living_room_fireplace"
TITLE = "fireplace-fastily-1080p.webm"


def load_package(path) -> Any:
    class SecretLoader(yaml.SafeLoader):
        """Resolve fixture secret scalars with SafeLoader and no object constructors."""

    SecretLoader.add_constructor(
        "!secret", lambda loader, node: loader.construct_scalar(node)
    )
    # SafeLoader is extended only with a scalar constructor for the fixture tag.
    return yaml.load(path.read_text(), Loader=SecretLoader)  # noqa: S506


def short_timeouts(value) -> Any:
    """Accelerate only wall-clock expiry; keep the real templates/actions."""
    if isinstance(value, dict):
        result = {key: short_timeouts(item) for key, item in value.items()}
        if "wait_template" in result:
            result["timeout"] = {"milliseconds": 120}
        return result
    if isinstance(value, list):
        return [short_timeouts(item) for item in value]
    return value


def playback_attributes(request_id) -> dict[str, Any]:
    """Build a fresh observation; REST and sensor overrides never share a dict."""
    stamp = dt_util.utcnow().isoformat()
    return {
        "owned": True,
        "file_matches": True,
        "repeat": "one",
        "observed_at": stamp,
        "launch_started_at": stamp,
        "error": None,
        "request_id": request_id,
        "source_id": "youtube-example",
        "source_kind": "youtube",
        "title": TITLE,
        "progressing": True,
    }


class ProjectorPublication(Entity):
    """Fake only the device update; HA handles errors and state publication."""

    def __init__(
        self,
        hass: HomeAssistant,
        current: State,
        attributes: dict[str, Any],
        *,
        fail_update: bool,
    ) -> None:
        self.hass = hass
        self.entity_id = PROJECTOR
        self._attr_state = current.state
        self._attr_extra_state_attributes = attributes
        self.fail_update = fail_update

    async def async_update(self) -> None:
        if self.fail_update:
            error_message = "Projector publication failed"
            raise HomeAssistantError(error_message)


class LivingRoomFireplaceTests(unittest.IsolatedAsyncioTestCase):
    """Exercise living room fireplace tests behavior with deterministic native fixtures."""

    async def asyncSetUp(self) -> None:
        package_path = ROOT / "packages/living_room_fireplace.yaml"
        self.assertTrue(package_path.exists(), "Projector fireplace action is missing")
        self.package = load_package(package_path)
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.calls, self.scripts = [], {}
        self.completed, self.last_trace = [], []
        self.power_failure = False
        self.projector_refresh_attributes = None
        self.projector_refresh_error = False
        self.projector_refresh_states = []
        self.power_release = asyncio.Event()
        self.power_release.set()
        self.power_entered = asyncio.Event()
        self.power_returned = asyncio.Event()
        self.ready = True
        self.counter = 0
        self.rest_status = 200
        self.rest_overrides = {}
        self.sensor_state = "playing"
        self.sensor_overrides = {}
        self.native_state = "playing"
        self.native_title = TITLE
        self.current_request = None
        self.set_initial_states()
        self.register_boundaries()
        for name, config in self.package["script"].items():
            self.register_script(name, config)

    def set_initial_states(self) -> None:
        self.hass.states.async_set(KODI, "unavailable")
        self.hass.states.async_set(READINESS, "unavailable")
        self.hass.states.async_set(
            PROJECTOR,
            "off",
            {
                "power_intent_revision": "epoch:0",
                "last_power_intent": False,
                "power_sync_target": None,
                "power_sync_error": None,
            },
        )
        self.hass.states.async_set("binary_sensor.projector_hdmi_connected", "off")
        self.hass.states.async_set("sensor.kodi_service_state", "off")

    def register_boundaries(self) -> None:
        for domain, service in (
            ("media_player", "turn_on"),
            ("homeassistant", "update_entity"),
        ):
            self.hass.services.async_register(domain, service, self.boundary)
        self.hass.services.async_register(
            "script",
            "projector_start_for_playback",
            self.boundary,
            supports_response=SupportsResponse.OPTIONAL,
        )
        self.hass.services.async_register(
            "rest_command",
            "host_kodi_fireplace",
            self.boundary,
            supports_response=SupportsResponse.OPTIONAL,
        )

    async def boundary(self, call) -> Any:
        action = f"{call.domain}.{call.service}"
        self.calls.append((action, deepcopy(dict(call.data))))
        handlers = {
            "script.projector_start_for_playback": self.power_on,
            "rest_command.host_kodi_fireplace": self.start_playback,
            "homeassistant.update_entity": self.refresh_readiness,
        }
        return await handlers.get(action, self.ignore_call)(call)

    async def ignore_call(self, _call) -> None:
        return None

    async def power_on(self, _call) -> dict[str, bool]:
        self.power_entered.set()
        await self.power_release.wait()
        if self.power_failure:
            error_message = "Physical pair did not confirm power"
            raise HomeAssistantError(error_message)
        self.counter += 1
        self.hass.states.async_set(
            PROJECTOR,
            "idle",
            {
                "power_intent_revision": f"epoch:{self.counter}",
                "last_power_intent": True,
                "power_sync_target": None,
                "power_sync_error": None,
            },
        )
        self.hass.states.async_set("binary_sensor.projector_hdmi_connected", "on")
        self.hass.states.async_set("sensor.kodi_service_state", "on")
        if self.ready:
            self.hass.states.async_set(KODI, "idle")
        self.power_returned.set()
        return {"sent": True}

    async def start_playback(self, call) -> dict[str, Any]:
        payload = self.package["rest_command"]["host_kodi_fireplace"]["payload"]
        rendered = Template(payload, self.hass).async_render(
            dict(call.data), parse_result=False
        )
        self.current_request = json.loads(rendered)["request_id"]
        if not isinstance(self.current_request, str):
            return {
                "status": 400,
                "content": {"ok": False, "error": "invalid_fireplace_action"},
            }
        response = {
            "ok": True,
            "state": "playing",
            **playback_attributes(self.current_request),
            **self.rest_overrides,
        }
        if self.native_state is not None:
            self.hass.states.async_set(
                KODI, self.native_state, {"media_title": self.native_title}
            )
        return {"status": self.rest_status, "content": response}

    async def refresh_projector(self) -> None:
        """Exercise native HA refresh, including its swallowed device errors."""
        current = self.hass.states.get(PROJECTOR)
        if current is None:
            return
        attributes = (
            self.projector_refresh_attributes
            if self.projector_refresh_attributes is not None
            else dict(current.attributes)
        )
        entity = ProjectorPublication(
            self.hass, current, attributes, fail_update=self.projector_refresh_error
        )
        await entity.async_update_ha_state(force_refresh=True)
        current = self.hass.states.get(PROJECTOR)
        self.projector_refresh_states.append(
            {
                "projector": current.state if current is not None else None,
                "revision": current.attributes.get("power_intent_revision")
                if current is not None
                else None,
                "kodi": self.hass.states.get(KODI).state,
                "hdmi": self.hass.states.get(
                    "binary_sensor.projector_hdmi_connected"
                ).state,
                "kodi_service": self.hass.states.get("sensor.kodi_service_state").state,
                "power_calls": self.counter,
            }
        )

    async def refresh_readiness(self, call) -> None:
        if PROJECTOR in call.data.get("entity_id", []):
            await self.refresh_projector()
        if READINESS not in call.data.get("entity_id", []):
            return
        # A separate HA readback must not inherit the REST response's verdict.
        attributes = {
            **playback_attributes(self.current_request),
            **self.sensor_overrides,
        }
        self.hass.states.async_set(READINESS, self.sensor_state, attributes)

    def register_script(self, name, config) -> None:
        # Keep the real YAML and Script engine; only physical services are faked.
        sequence = short_timeouts(config["sequence"])
        script = Script(
            self.hass,
            cv.SCRIPT_SCHEMA(sequence),
            name,
            "script",
            script_mode=config.get("mode", "single"),
            max_runs=config.get("max", 10),
        )
        self.scripts[name] = script

        async def run(call) -> Any:
            if stack := script_stack_cv.get():
                script_stack_cv.set(stack.copy())
            result = await script.async_run(dict(call.data), context=call.context)
            response = result.service_response if result else None
            if (
                name == "living_room_fireplace"
                and isinstance(response, dict)
                and response.get("ok") is True
                and response.get("request_id") == self.current_request
            ):
                self.completed.append(True)
            return (response or {}) if call.return_response else None

        self.hass.services.async_register(
            "script", name, run, supports_response=SupportsResponse.OPTIONAL
        )

    async def asyncTearDown(self) -> None:
        if not hasattr(self, "hass"):
            return
        self.power_release.set()
        for script in self.scripts.values():
            await script.async_stop()
        await self.hass.async_stop()
        self.temp.cleanup()

    async def launch(self, request_id="voice-request") -> None:
        trace_clear()
        self.completed.clear()
        # None models a direct UI/Yandex script call with no request variable.
        data = {} if request_id is None else {"request_id": request_id}
        try:
            await self.hass.services.async_call(
                "script",
                "living_room_fireplace",
                data,
                blocking=True,
                context=Context(),
            )
        finally:
            # Contextvars belong to this task; an awaiting parent cannot inspect
            # the trace context of its create_task child directly.
            self.last_trace = [
                item.as_dict()
                for group in (trace_get(clear=False) or {}).values()
                for item in group
            ]

    def launches(self) -> list[Any]:
        return [
            data
            for action, data in self.calls
            if action == "rest_command.host_kodi_fireplace"
        ]

    def assert_aborted(self) -> None:
        self.assertEqual(self.completed, [])
        self.assertTrue(
            any(
                node.get("result", {}).get("error") is True for node in self.last_trace
            ),
            "Actual HA error stop was not reached",
        )

    async def assert_readback_rejected(self, request_id="voice-request") -> None:
        self.calls.clear()
        await self.launch(request_id)
        self.assert_aborted()
        self.assertEqual(len(self.launches()), 1)
        self.assertIn(
            ("homeassistant.update_entity", {"entity_id": [READINESS]}),
            self.calls,
            "A valid REST receipt must reach the independent HA readback",
        )

    async def test_waits_for_pair_and_kodi_then_confirms_same_request_youtube(
        self,
    ) -> None:
        self.ready = False
        self.power_release.clear()
        cached = self.hass.states.get(PROJECTOR)
        reported_before, updated_before = cached.last_reported, cached.last_updated
        operation = asyncio.create_task(self.launch())
        try:
            await asyncio.wait_for(self.power_entered.wait(), 0.5)
            refreshed = self.hass.states.get(PROJECTOR)
            self.assertEqual(refreshed.last_updated, updated_before)
            self.assertGreater(refreshed.last_reported, reported_before)
            self.assertFalse(self.launches())
            self.power_release.set()
            await asyncio.wait_for(self.power_returned.wait(), 0.5)
            await asyncio.sleep(0.01)
            self.assertFalse(operation.done())
            self.assertFalse(self.launches())
            self.hass.states.async_set(KODI, "idle")
            await operation
        finally:
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        self.assertEqual(
            self.calls,
            [
                ("homeassistant.update_entity", {"entity_id": [PROJECTOR]}),
                ("script.projector_start_for_playback", {}),
                (
                    "rest_command.host_kodi_fireplace",
                    {"fireplace_request": {"id": "voice-request"}},
                ),
                ("homeassistant.update_entity", {"entity_id": [READINESS]}),
            ],
        )
        self.assertEqual(self.completed, [True])

    async def test_cold_off_pair_is_published_before_a_native_yandex_launch(
        self,
    ) -> None:
        # The internal pair already exists; HA has not published its attributes.
        attributes = dict(self.hass.states.get(PROJECTOR).attributes)
        self.projector_refresh_attributes = attributes
        self.hass.states.async_set(PROJECTOR, "off", dict.fromkeys(attributes))
        self.hass.states.async_set(KODI, "off")
        await self.launch(None)
        self.assertEqual(self.completed, [True])
        self.assertEqual(
            self.projector_refresh_states,
            [
                {
                    "projector": "off",
                    "revision": "epoch:0",
                    "kodi": "off",
                    "hdmi": "off",
                    "kodi_service": "off",
                    "power_calls": 0,
                }
            ],
        )
        self.assertEqual(
            self.calls[:2],
            [
                ("homeassistant.update_entity", {"entity_id": [PROJECTOR]}),
                ("script.projector_start_for_playback", {}),
            ],
        )
        self.assertEqual(len(self.launches()), 1)
        self.assertIsInstance(self.current_request, str)

    async def test_native_failed_refresh_rejects_valid_cached_pair_before_power(
        self,
    ) -> None:
        cached = self.hass.states.get(PROJECTOR)
        reported_before = cached.last_reported
        self.assertEqual(cached.attributes["power_intent_revision"], "epoch:0")
        self.projector_refresh_error = True
        with self.assertLogs("homeassistant.helpers.entity", level="ERROR") as captured:
            await self.launch(None)
        self.assertTrue(
            any(
                "Update for media_player.projector fails" in line
                for line in captured.output
            )
        )
        self.assert_aborted()
        current = self.hass.states.get(PROJECTOR)
        self.assertEqual(current.last_reported, reported_before)
        self.assertEqual(current.attributes, cached.attributes)
        self.assertEqual(
            self.calls,
            [("homeassistant.update_entity", {"entity_id": [PROJECTOR]})],
        )
        self.assertFalse(self.launches())
        self.assertEqual(self.counter, 0)
        self.assertEqual(self.hass.states.get(PROJECTOR).state, "off")

    async def test_pair_failure_never_opens_media(self) -> None:
        self.power_failure = True
        with self.assertRaises(HomeAssistantError):
            await self.launch()
        self.assertFalse(self.launches())

    async def test_kodi_not_ready_is_an_explicit_failure_without_player_open(
        self,
    ) -> None:
        self.ready = False
        await self.launch()
        self.assert_aborted()
        self.assertFalse(self.launches())

    async def test_later_off_intent_cancels_pending_fireplace_without_playback(
        self,
    ) -> None:
        self.ready = False
        operation = asyncio.create_task(self.launch())
        try:
            await asyncio.wait_for(self.power_returned.wait(), 0.5)
            await asyncio.sleep(0.01)
            self.hass.states.async_set(
                PROJECTOR,
                "off",
                {
                    "power_intent_revision": "epoch:2",
                    "last_power_intent": False,
                    "power_sync_target": None,
                    "power_sync_error": None,
                },
            )
            self.hass.states.async_set(KODI, "idle")
            await operation
            self.assert_aborted()
            self.assertFalse(self.launches())
        finally:
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    async def test_http_success_without_the_observed_unique_title_is_failure_and_never_retried(
        self,
    ) -> None:
        for state, title in ((None, None), ("playing", "/some/other/movie.webm")):
            with self.subTest(state=state, title=title):
                self.native_state, self.native_title = state, title
                await self.assert_readback_rejected()

    async def test_http_200_without_actual_repeat_confirmation_is_an_error_stop(
        self,
    ) -> None:
        self.rest_overrides = {"repeat": "off"}
        await self.launch()
        self.assert_aborted()
        self.assertEqual(len(self.launches()), 1)

    async def test_success_response_preserves_numeric_looking_request_id(self) -> None:
        response = await self.hass.services.async_call(
            "script",
            "living_room_fireplace",
            {"request_id": "00001234"},
            blocking=True,
            return_response=True,
            context=Context(),
        )
        self.assertEqual(response, {"ok": True, "request_id": "00001234"})
        self.assertEqual(self.completed, [True])

    async def test_final_guard_failure_has_no_success_response(self) -> None:
        self.native_title = "a-different-video"
        response = await self.hass.services.async_call(
            "script",
            "living_room_fireplace",
            {"request_id": "final-guard-failure"},
            blocking=True,
            return_response=True,
            context=Context(),
        )
        self.assertEqual(response, {})
        self.assertEqual(self.completed, [])
        sensor = self.hass.states.get(READINESS)
        self.assertEqual(sensor.state, "playing")
        self.assertEqual(sensor.attributes["request_id"], "final-guard-failure")

    async def test_yandex_name_is_projector_specific(self) -> None:
        entity = self.package["yandex_smart_home"]["entity_config"][
            "script.living_room_fireplace"
        ]
        self.assertEqual(entity["name"], "Камин на проекторе")
        self.assertEqual(entity["room"], "Гостиная")

    async def test_fresh_previous_request_rest_receipt_is_rejected(self) -> None:
        self.rest_overrides = {"request_id": "previous-command"}
        await self.launch("current-command")
        self.assert_aborted()
        self.assertEqual(len(self.launches()), 1)

    async def test_rest_receipt_without_advancing_time_is_rejected(self) -> None:
        self.rest_overrides = {"progressing": False}
        await self.launch()
        self.assert_aborted()
        self.assertEqual(len(self.launches()), 1)

    async def test_native_title_must_match_dynamic_verified_source(self) -> None:
        title = "A different verified YouTube fireplace"
        self.rest_overrides = {"title": title}
        self.sensor_overrides = {"title": title}
        self.native_title = title
        await self.launch("dynamic-title")
        self.assertEqual(self.completed, [True])
        self.assertEqual(self.launches()[0]["fireplace_request"]["id"], "dynamic-title")

    async def test_valid_rest_receipt_cannot_hide_invalid_sensor_proof(self) -> None:
        invalid = (
            ("request_id", "previous-command"),
            ("request_id", None),
            ("source_id", "previous-source"),
            ("source_id", None),
            ("observed_at", "2000-01-01T00:00:00+00:00"),
            ("observed_at", "not-a-timestamp"),
            ("observed_at", None),
            ("repeat", "off"),
            ("repeat", "all"),
            ("repeat", None),
            ("progressing", False),
            ("progressing", None),
            ("owned", False),
            ("file_matches", False),
        )
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                self.sensor_overrides = {field: value}
                await self.assert_readback_rejected("current-command")

    async def test_valid_rest_receipt_cannot_hide_unavailable_or_stopped_sensor(
        self,
    ) -> None:
        for state in ("unavailable", "idle", "paused"):
            with self.subTest(state=state):
                self.sensor_state = state
                await self.assert_readback_rejected()

    async def test_native_player_must_be_playing_even_with_the_current_title(
        self,
    ) -> None:
        for state in ("idle", "paused", "buffering", "off", "unavailable"):
            with self.subTest(state=state):
                self.native_state = state
                await self.assert_readback_rejected()

    async def test_direct_ui_or_yandex_calls_generate_fresh_correlated_request_ids(
        self,
    ) -> None:
        for _ in range(2):
            await self.launch(None)
            self.assertEqual(self.completed, [True])
        request_ids = [item["fireplace_request"]["id"] for item in self.launches()]
        self.assertEqual(len(request_ids), 2)
        for request_id in request_ids:
            self.assertIsInstance(request_id, str)
            self.assertRegex(request_id, r"^[A-Za-z0-9_.:+-]{1,96}$")
        self.assertNotEqual(request_ids[0], request_ids[1])

    async def test_queued_commands_keep_distinct_request_ids(self) -> None:
        self.assertEqual(
            self.package["script"]["living_room_fireplace"]["mode"], "queued"
        )
        self.power_release.clear()
        first = asyncio.create_task(self.launch("first-command"))
        await asyncio.wait_for(self.power_entered.wait(), 0.5)
        second = asyncio.create_task(self.launch("second-command"))
        self.power_release.set()
        await asyncio.gather(first, second)
        self.assertEqual(
            [item["fireplace_request"]["id"] for item in self.launches()],
            ["first-command", "second-command"],
        )

    async def test_numeric_looking_request_ids_preserve_the_exact_caller_string(
        self,
    ) -> None:
        for request_id in ("12345", "1.5", "+12345", "1e6"):
            with self.subTest(request_id=request_id):
                await self.launch(request_id)
                self.assertEqual(self.completed, [True])
                self.assertEqual(self.current_request, request_id)

    async def test_missing_pair_contract_never_touches_legacy_tv_power_route(
        self,
    ) -> None:
        for missing in (
            PROJECTOR,
            "binary_sensor.projector_hdmi_connected",
            "sensor.kodi_service_state",
        ):
            with self.subTest(missing=missing):
                self.set_initial_states()
                self.calls.clear()
                if missing == PROJECTOR:
                    self.hass.states.async_set(PROJECTOR, "off", {})
                else:
                    self.hass.states.async_set(missing, "unavailable")
                await self.launch("missing-power-contract")
                self.assert_aborted()
                self.assertEqual(
                    self.calls,
                    [("homeassistant.update_entity", {"entity_id": [PROJECTOR]})],
                )
                self.assertEqual(self.counter, 0)
                self.assertFalse(self.launches())
