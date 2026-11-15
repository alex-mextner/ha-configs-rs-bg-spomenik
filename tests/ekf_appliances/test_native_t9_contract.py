"""Real HA Script boundary for relative physical Chromecast T9 operations."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from homeassistant.core import SupportsResponse
from homeassistant.helpers.trace import trace_clear, trace_get
from test_kodi_t9_contract import T9ContractFixture

from custom_components.androidtv_remote.ime import KeyboardSession
from custom_components.androidtv_remote.ime_schema import (
    ClientMessage,
    ServerMessage,
)


class NativeT9ContractTests(T9ContractFixture, unittest.IsolatedAsyncioTestCase):
    """Verify relative Chromecast editing at the actual HA script boundary."""

    async def test_four_text_operations_use_one_generic_native_service_each(
        self,
    ) -> None:
        rows = [
            ("t9_send_char", {"char": "a", "mode": "add"}, "insert"),
            ("t9_send_char", {"char": "b", "mode": "cycle"}, "replace_previous"),
            ("t9_backspace", {}, "delete_backward"),
            ("t9_commit", {}, "submit"),
        ]
        for name, variables, operation in rows:
            with self.subTest(operation=operation):
                self.calls.clear()
                await self.run_t9(name, pair="tv", native=False, **variables)
                self.assertEqual(len(self.calls), 1)
                service, data = self.calls[0]
                self.assertEqual(service, "androidtv_remote.send_text")
                self.assertEqual(data["operation"], operation)
                target = data.get("entity_id")
                self.assertIn(
                    "remote.televizor", target if isinstance(target, list) else [target]
                )

    async def test_cycle_is_one_range_edit_without_a_delete_key_or_kodi_call(
        self,
    ) -> None:
        self.text, self.cursor = "preaFIX", 4
        await self.run_t9(
            "t9_send_char", char="b", mode="cycle", pair="tv", native=False
        )
        self.assertEqual(self.text, "prebFIX")
        self.assertEqual(
            [name for name, _ in self.calls], ["androidtv_remote.send_text"]
        )
        self.assertEqual(self.calls[0][1]["operation"], "replace_previous")

    async def test_literal_space_and_digit_survive_actual_ha_native_templates(
        self,
    ) -> None:
        self.text, self.cursor = "", 0
        for char in ("a", "space", "1"):
            await self.run_t9(
                "t9_send_char", char=char, mode="add", pair="tv", native=False
            )
        self.assertEqual(self.text, "a 1")
        self.assertEqual([data["text"] for _, data in self.calls], ["a", " ", "1"])

    async def test_native_target_does_not_edit_or_submit_android_keyboard(self) -> None:
        for name in ("t9_backspace", "t9_commit"):
            await self.run_t9(name, pair="tv", native=True)
        self.assertEqual(self.calls, [])

    async def assert_unsent_native_response(
        self, response: dict[str, Any], name: str, variables: dict[str, Any]
    ) -> None:
        """Require script failure and no fallback for an unsuccessful provider result."""
        self.native_response = response
        self.completed_operations.clear()
        self.calls.clear()
        trace_clear()
        await self.run_t9(name, pair="tv", native=False, **variables)
        self.assertEqual(self.completed_operations, [])
        nodes = [
            item.as_dict()
            for group in (trace_get(clear=False) or {}).values()
            for item in group
        ]
        self.assertTrue(
            any(
                node.get("result", {}).get("error") is True
                for node in nodes
            )
        )
        self.assertFalse(
            any(
                service == "remote.send_command"
                for service, _ in self.calls
            )
        )

    async def test_unsent_or_missing_entity_response_stops_before_success(self) -> None:
        for response in (
            {"remote.televizor": {"sent": False, "verified": False}},
            {},
            {"sent": True, "verified": False},
            {"remote.televizor": {"verified": True}},
        ):
            for name, variables in (
                ("t9_send_char", {"char": "a", "mode": "add"}),
                ("t9_backspace", {}),
                ("t9_commit", {}),
            ):
                with self.subTest(response=response, operation=name):
                    await self.assert_unsent_native_response(response, name, variables)

    async def test_commit_and_delete_stay_after_already_queued_native_characters(
        self,
    ) -> None:
        for final, expected in (("backspace", "delete_backward"), ("commit", "submit")):
            with self.subTest(final=final):
                self.calls.clear()
                self.block_first_native = True
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
                                pair="tv",
                                native=False,
                            )
                        )
                    )
                    await asyncio.wait_for(self.bridge_entered.wait(), 1)
                    tasks.append(
                        asyncio.create_task(
                            self.run_t9(
                                "t9_send_char",
                                char="d",
                                mode="add",
                                pair="tv",
                                native=False,
                            )
                        )
                    )
                    for _ in range(20):
                        if self.scripts["t9_send_char"].runs == 2:
                            break
                        await asyncio.sleep(0)
                    self.assertEqual(self.scripts["t9_send_char"].runs, 2)
                    tasks.append(
                        asyncio.create_task(
                            self.run_t9("t9_" + final, pair="tv", native=False)
                        )
                    )
                    for _ in range(5):
                        await asyncio.sleep(0)
                finally:
                    self.bridge_release.set()
                    await asyncio.gather(*tasks)
                self.assertEqual(
                    [
                        data["operation"]
                        for service, data in self.calls
                        if service == "androidtv_remote.send_text"
                    ],
                    ["insert", "insert", expected],
                )

    async def test_queued_before_app_change_keys_never_reach_the_new_editor(
        self,
    ) -> None:

        writes, entered = [], []
        session = KeyboardSession(writes.append, timeout=0.2)
        session.claim_transport(object(), writes.append)
        session.set_connected(True)
        initial = ServerMessage()
        initial.field.editor.field_id = 7
        initial.field.editor.input_type = 1
        initial.field.editor.package = "org.before.player"
        initial.field.value.text = "p"
        initial.field.value.start = initial.field.value.end = 1
        initial.field.value.counter = 10
        session.receive(initial.SerializeToString())
        snapshot = await session.async_snapshot()

        async def native(call) -> dict[str, Any]:
            entered.append(dict(call.data))
            result = await session.async_send_text(
                call.data["operation"], call.data.get("text", "")
            )
            return {"remote.televizor": result}

        self.hass.services.async_register(
            "androidtv_remote",
            "send_text",
            native,
            supports_response=SupportsResponse.OPTIONAL,
        )
        phone = asyncio.create_task(
            session.async_edit(
                snapshot["session_id"],
                snapshot["revision"],
                "px",
                {"start": 2, "end": 2},
            )
        )
        await asyncio.sleep(0)
        tasks = [phone]
        try:
            tasks.append(
                asyncio.create_task(
                    self.run_t9(
                        "t9_send_char", char="a", mode="add", pair="tv", native=False
                    )
                )
            )
            for _ in range(30):
                if entered:
                    break
                await asyncio.sleep(0)
            self.assertEqual(len(entered), 1)
            tasks.append(
                asyncio.create_task(
                    self.run_t9(
                        "t9_send_char", char="d", mode="add", pair="tv", native=False
                    )
                )
            )
            for _ in range(30):
                if self.scripts["t9_send_char"].runs == 2:
                    break
                await asyncio.sleep(0)
            self.assertEqual(self.scripts["t9_send_char"].runs, 2)
            next_app = ServerMessage()
            next_app.field.editor.package = "org.after.player"
            session.receive(next_app.SerializeToString())
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 0.5)
            self.assertEqual(
                len(writes), 1, "An old key escaped the HA queue into the new app"
            )
            await self.run_t9(
                "t9_send_char", char="e", mode="add", pair="tv", native=False
            )
            sent = ClientMessage()
            sent.ParseFromString(writes[-1])
            self.assertEqual(sent.batch.edits[0].replace.text, "e")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
