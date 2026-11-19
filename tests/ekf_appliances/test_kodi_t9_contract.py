"""
Regression for Kodi's whole-field SendText contract, using real HA scripts.

Kodi 21.2-Omega InputOperations.cpp sends GUI_MSG_SET_TEXT. GUIEditControl
SetLabel2 replaces the field; done=True submits that replacement. This boundary
model deliberately implements that external contract, not the desired T9 code.
Hardware acceptance still requires a real Kodi keyboard field/readback.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from homeassistant import loader
from homeassistant.core import Context, HomeAssistant, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import frame
from homeassistant.helpers.script import Script, script_stack_cv
from homeassistant.helpers.template import Template
from homeassistant.helpers.trace import trace_clear, trace_get

from custom_components.ekf_appliances.remote_control import (
    RemoteController,
    async_register_remote_services,
)

PACKAGE = Path(__file__).resolve().parents[2] / "packages/universal_remote.yaml"


class SecretLoader(yaml.SafeLoader):
    """Exercise secret loader contracts."""


SecretLoader.add_constructor("!secret", lambda _loader, _node: "test-placeholder")


class T9ContractFixture:
    """Exercise t9 contract fixture contracts."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.text, self.cursor, self.submitted = "prefix", 6, False
        self.calls = []
        self.bridge_response = None
        self.native_response = None
        self.block_first_native = False
        self.native_lock = asyncio.Lock()
        self.completed_operations = []
        self.submitted_text = None
        self.block_first_bridge = False
        self.bridge_entered = asyncio.Event()
        self.bridge_release = asyncio.Event()
        self.scripts = {}
        doc = yaml.safe_load(PACKAGE.read_text())
        names = ["t9_send_char", "t9_commit", "t9_backspace"]
        if "t9_kodi_text" in doc["script"]:
            names.append("t9_kodi_text")
        for name in names:
            cfg = doc["script"][name]
            sequence = list(cfg["sequence"])
            if name == "t9_send_char":
                sequence.append({"action": "test.record_completed_operation"})
            self.scripts[name] = Script(
                self.hass,
                cv.SCRIPT_SCHEMA(sequence),
                name,
                "script",
                script_mode=cfg.get("mode", "single"),
            )
        for name, script in self.scripts.items():

            async def run_script(call, script=script) -> Any:
                if stack := script_stack_cv.get():
                    script_stack_cv.set(stack.copy())
                result = await script.async_run(dict(call.data), context=call.context)
                return result.service_response if result else None

            self.hass.services.async_register(
                "script", name, run_script, supports_response=SupportsResponse.OPTIONAL
            )
        self.hass.states.async_set("input_select.universal_remote_pair", "projector")
        self.hass.states.async_set(
            "input_boolean.universal_remote_native_target", "off"
        )
        self.hass.services.async_register("kodi", "call_method", self.rpc)
        self.hass.services.async_register(
            "rest_command",
            "host_kodi_text_input",
            self.bridge,
            supports_response=SupportsResponse.OPTIONAL,
        )
        self.hass.services.async_register("remote", "send_command", self.remote)
        self.hass.services.async_register(
            "androidtv_remote",
            "edit_text",
            self.native,
            supports_response=SupportsResponse.OPTIONAL,
        )
        self.hass.services.async_register(
            "androidtv_remote",
            "send_text",
            self.native,
            supports_response=SupportsResponse.OPTIONAL,
        )

        async def completed(_call) -> None:
            self.completed_operations.append(True)

        self.hass.services.async_register(
            "test", "record_completed_operation", completed
        )
        # Editing contracts exercise an available display; off/busy admission is
        # verified separately against the same real controller boundary.
        self.hass.states.async_set("media_player.ue48j5500_ue48j5500", "on")
        self.hass.states.async_set("media_player.projector", "on")
        runtime = SimpleNamespace(
            hass=self.hass,
            entry=SimpleNamespace(data={"tv_kodi_entity_id": "media_player.home"}),
            projector_shutting_down=False,
            shutdown=SimpleNamespace(requested_at=0.0),
            projector_power_state=True,
            projector_power_entity_id="binary_sensor.projector_hdmi_connected",
            projector_power_pair=None,
            tv_power_pair=None,
        )
        self.controller = RemoteController(runtime)
        self.hass.data.setdefault("ekf_appliances", {})["remote_controller"] = (
            self.controller
        )
        async_register_remote_services(self.hass)
        await self.controller.async_start()

    async def asyncTearDown(self) -> None:
        for script in self.scripts.values():
            await script.async_stop()
        await self.controller.async_close()
        await self.hass.async_block_till_done()
        self.temp.cleanup()

    def edit(self, action, char="") -> None:
        if action in ("cycle", "backspace") and self.cursor:
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
            self.cursor -= 1
        if action in ("add", "cycle"):
            self.text = self.text[: self.cursor] + char + self.text[self.cursor :]
            self.cursor += len(char)
        if action == "commit":
            self.submitted = True
            self.submitted_text = self.text

    async def rpc(self, call) -> None:
        data = dict(call.data)
        self.calls.append(("kodi.call_method", data))
        if data["method"] == "Input.SendText":
            self.assertIsInstance(
                data["text"], str, "Kodi Input.SendText requires JSON string text"
            )
            self.text = data["text"]
            self.cursor = len(self.text)
            self.submitted = data.get("done", True)
        elif data["method"] == "Input.ExecuteAction":
            self.edit(
                {"backspace": "backspace", "enter": "commit"}.get(
                    data["action"], "none"
                )
            )
        elif data["method"] == "Input.ButtonEvent":
            # Kodi21.2's JSON-RPC XBMC_BUTTON path does not carry printable Unicode.
            pass
        else:
            error_message = "Unexpected RPC method"
            raise AssertionError(error_message)

    async def bridge(self, call) -> Any:
        data = dict(call.data)
        # Exercise the actual REST payload too: HA natively converts a lone "1"
        # template result to int, while Kodi's one-character endpoint needs str.
        config = yaml.load(
            PACKAGE.with_name("host_actions.yaml").read_text(),
            Loader=SecretLoader,  # noqa: S506 - SafeLoader subclass adds only a literal !secret placeholder.
        )
        payload = Template(
            config["rest_command"]["host_kodi_text_input"]["payload"], self.hass
        )
        body = json.loads(payload.async_render(data, parse_result=False))
        self.calls.append(("rest_command.host_kodi_text_input", body))
        if self.block_first_bridge and not self.bridge_entered.is_set():
            self.bridge_entered.set()
            await self.bridge_release.wait()
        if self.bridge_response is not None:
            return self.bridge_response
        if body["action"] in ("add", "cycle"):
            self.assertIsInstance(body["char"], str)
            self.assertEqual(len(body["char"]), 1)
        self.edit(body["action"], body.get("char", ""))
        return {"status": 200, "content": {"ok": True, "verified": True}}

    async def remote(self, call) -> None:
        self.calls.append(("remote.send_command", dict(call.data)))

    async def native(self, call) -> Any:
        data = dict(call.data)
        async with self.native_lock:
            self.calls.append(("androidtv_remote." + call.service, data))
            if self.block_first_native and not self.bridge_entered.is_set():
                self.bridge_entered.set()
                await self.bridge_release.wait()
            if self.native_response is not None:
                return self.native_response
            operation = data["operation"]
            char = data.get("text", "")
            if operation in ("insert", "replace_previous"):
                self.assertIsInstance(char, str)
                self.assertEqual(len(char), 1)
            self.edit(
                {
                    "insert": "add",
                    "replace_previous": "cycle",
                    "delete_backward": "backspace",
                    "submit": "commit",
                }[operation],
                char,
            )
            return (
                {"remote.televizor": {"sent": True, "verified": False}}
                if call.service == "send_text"
                else {"remote.televizor": {"verified": True, "revision": 1}}
            )

    async def run_t9(self, name, **variables: Any) -> None:
        await self.scripts[name].async_run(variables, context=Context())


class KodiT9ContractTests(T9ContractFixture, unittest.IsolatedAsyncioTestCase):
    """Exercise kodi t9 contract tests contracts."""

    async def test_add_preserves_existing_word_and_text_after_cursor(self) -> None:
        self.text, self.cursor = "preFIX", 3
        await self.run_t9(
            "t9_send_char", char="a", mode="add", pair="projector", native=False
        )
        self.assertEqual(self.text, "preaFIX")
        self.assertEqual(self.cursor, 4)

    async def test_cycle_replaces_only_preceding_character(self) -> None:
        self.text, self.cursor = "preaFIX", 4
        await self.run_t9(
            "t9_send_char", char="b", mode="cycle", pair="projector", native=False
        )
        self.assertEqual(self.text, "prebFIX")
        self.assertEqual(self.cursor, 4)

    async def test_commit_submits_without_replacing_word_with_empty_string(
        self,
    ) -> None:
        await self.run_t9("t9_commit", pair="projector", native=False)
        self.assertEqual(self.text, "prefix")
        self.assertTrue(self.submitted)

    async def test_actual_profile_alphabet_space_and_one_append(self) -> None:
        self.text, self.cursor = "", 0
        for char in "abcdefghijklmnopqrstuvwxyz":
            await self.run_t9(
                "t9_send_char", char=char, mode="add", pair="projector", native=False
            )
        await self.run_t9(
            "t9_send_char", char="space", mode="add", pair="projector", native=False
        )
        await self.run_t9(
            "t9_send_char", char="1", mode="add", pair="projector", native=False
        )
        self.assertEqual(self.text, "abcdefghijklmnopqrstuvwxyz 1")

    async def test_explicit_pair_survives_focus_helper_change(self) -> None:
        self.hass.states.async_set("input_select.universal_remote_pair", "tv")
        await self.run_t9(
            "t9_send_char", char="a", mode="add", pair="projector", native=False
        )
        self.assertEqual(self.text, "prefixa")
        self.assertFalse(any(name == "remote.send_command" for name, _ in self.calls))

    async def test_backspace_keeps_suffix_and_stays_in_text_field(self) -> None:
        self.text, self.cursor = "preaFIX", 4
        await self.run_t9("t9_backspace", pair="projector", native=False)
        self.assertEqual(self.text, "preFIX")
        self.assertFalse(self.submitted)

    async def test_native_target_does_not_submit_kodi_keyboard(self) -> None:
        await self.run_t9("t9_commit", pair="projector", native=True)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.submitted)

    async def test_http_failure_or_unverified_result_is_reported_as_script_error(
        self,
    ) -> None:
        for response in (
            {"status": 500, "content": {"ok": False}},
            {"status": 200, "content": {"ok": True, "verified": False}},
        ):
            for name, data in (
                ("t9_send_char", {"char": "a", "mode": "add"}),
                ("t9_backspace", {}),
                ("t9_commit", {}),
            ):
                with self.subTest(response=response, script=name):
                    self.bridge_response = response
                    self.completed_operations.clear()
                    trace_clear()
                    await self.run_t9(name, pair="projector", native=False, **data)
                    trace_nodes = [
                        item.as_dict()
                        for entries in (trace_get(clear=False) or {}).values()
                        for item in entries
                    ]
                    self.assertTrue(
                        any(
                            node.get("result", {}).get("error") is True
                            and "not confirmed"
                            in node.get("result", {}).get("stop", "")
                            for node in trace_nodes
                        ),
                        "No actual HA error stop was executed",
                    )
                    self.assertEqual(
                        self.completed_operations,
                        [],
                        "Failed edit reached the success continuation",
                    )
                    self.assertEqual(self.text, "prefix")
                    self.assertFalse(self.submitted)

    async def test_backspace_and_commit_cannot_overtake_an_already_queued_character(
        self,
    ) -> None:
        for final in ("backspace", "commit"):
            with self.subTest(final_operation=final):
                await self.assert_queued_character_order(final)

    async def assert_queued_character_order(self, final: str) -> None:
        self.text, self.cursor, self.submitted, self.submitted_text = (
            "p",
            1,
            False,
            None,
        )
        self.calls.clear()
        self.block_first_bridge = True
        self.bridge_entered = asyncio.Event()
        self.bridge_release = asyncio.Event()
        tasks = []
        try:
            tasks.append(
                asyncio.create_task(
                    self.run_t9(
                        "t9_send_char",
                        char="a",
                        mode="add",
                        pair="projector",
                        native=False,
                    )
                )
            )
            await asyncio.wait_for(self.bridge_entered.wait(), 2)
            tasks.append(
                asyncio.create_task(
                    self.run_t9(
                        "t9_send_char",
                        char="d",
                        mode="add",
                        pair="projector",
                        native=False,
                    )
                )
            )
            # Verify the second char has entered the actual HA script queue,
            # then enqueue the later backspace/submit from another caller.
            for _ in range(20):
                if self.scripts["t9_send_char"].runs == 2:
                    break
                await asyncio.sleep(0)
            self.assertEqual(self.scripts["t9_send_char"].runs, 2)
            tasks.append(
                asyncio.create_task(
                    self.run_t9("t9_" + final, pair="projector", native=False)
                )
            )
            for _ in range(5):
                await asyncio.sleep(0)
        finally:
            self.bridge_release.set()
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 2)
        actions = [
            body["action"]
            for name, body in self.calls
            if name == "rest_command.host_kodi_text_input"
        ]
        self.assertEqual(actions, ["add", "add", final])
        if final == "backspace":
            self.assertEqual(self.text, "pa")
            self.assertFalse(self.submitted)
        else:
            self.assertEqual(self.submitted_text, "pad")

    async def test_chromecast_space_survives_real_ha_template_rendering(self) -> None:
        for mode in ("add", "cycle"):
            with self.subTest(mode=mode):
                self.calls.clear()
                await self.run_t9(
                    "t9_send_char", char="space", mode=mode, pair="tv", native=False
                )
                self.assertEqual(len(self.calls), 1)
                name, data = self.calls[0]
                self.assertEqual(name, "androidtv_remote.send_text")
                self.assertEqual(
                    data["operation"], "insert" if mode == "add" else "replace_previous"
                )
                self.assertEqual(data["text"], " ")
