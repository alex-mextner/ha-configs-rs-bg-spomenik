# Copyright (c) 2026 Alex Mextner
"""Real pinned HTTP/WS dispatch and announcement semantics, without devices."""

import asyncio
import inspect
import json
import logging
import types
import unittest
from typing import get_type_hints
from unittest.mock import AsyncMock, patch

from aiohttp import web
from music_assistant.controllers.webserver import controller as http
from music_assistant.controllers.webserver import websocket_client as ws
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    set_current_user,
)
from music_assistant.helpers import ha_recovery_intent as intent
from music_assistant_models.errors import InsufficientPermissions


class NativeIntentTests(unittest.IsolatedAsyncioTestCase):
    """Use production command dispatch while replacing only device and HTTP edges."""

    def setUp(self) -> None:
        self.steps = []
        self.tasks = []
        self.user = types.SimpleNamespace(player_filter=[], username="fixture")
        self.state = types.SimpleNamespace(available=True, enabled=True)
        self.player = types.SimpleNamespace(
            player_id="studio", protocol_parent_id=None, private=False
        )
        self.players = types.SimpleNamespace(
            get_player=lambda _: self.player, get_player_state=lambda _: self.state
        )
        self.mass = types.SimpleNamespace(
            players=self.players,
            command_handlers={},
            translations=types.SimpleNamespace(ensure_locale_loaded=AsyncMock()),
            create_task=lambda c: self.tasks.append(asyncio.create_task(c)),
        )

    def tearDown(self) -> None:
        set_current_user(None)

    async def invoke(
        self,
        transport: str,
        *,
        authorized: bool = True,
        invalid: bool = False,
        fails: bool = False,
        private: bool = False,
    ) -> list:
        async def target(player_id: str) -> None:
            self.steps.append(player_id)
            if fails:
                msg = "device_failure"
                raise RuntimeError(msg)

        handler = types.SimpleNamespace(
            target=target,
            signature=inspect.signature(target),
            type_hints=get_type_hints(target),
            authenticated=True,
            required_scope=None,
            allow_impersonation=False,
        )
        self.mass.command_handlers["players/cmd/stop"] = handler
        if private:

            def denied(_) -> None:
                msg = "fixture_denied"
                raise InsufficientPermissions(msg)

            self.players.get_player_state = denied
        args = {} if invalid else {"player_id": "studio"}
        with patch.object(intent.LOGGER, "info") as records:
            if transport == "http":
                await self.http_call(args, authorized=authorized)
            else:
                await self.ws_call(args, authorized=authorized)
            return [json.loads(c.args[1]) for c in records.call_args_list]

    async def http_call(self, args: dict, *, authorized: bool) -> None:
        controller = types.SimpleNamespace(
            mass=self.mass,
            auth=types.SimpleNamespace(has_users=True),
            logger=logging.getLogger("intent_fixture"),
            _localized_json_response=lambda *_: web.Response(text="{}"),
        )
        controller._authenticate_api_command = types.MethodType(
            http.WebserverController._authenticate_api_command, controller
        )
        request = types.SimpleNamespace(
            remote="127.0.0.1",
            can_read_body=True,
            headers={},
            read=AsyncMock(
                return_value=json.dumps(
                    {
                        "message_id": "fixture",
                        "command": "players/cmd/stop",
                        "args": args,
                    }
                ).encode()
            ),
        )
        with patch.object(
            http,
            "get_authenticated_user",
            AsyncMock(return_value=self.user if authorized else None),
        ):
            await http.WebserverController._handle_jsonrpc_api_command(
                controller, request
            )

    async def ws_call(self, args: dict, *, authorized: bool) -> None:
        client = types.SimpleNamespace(
            mass=self.mass,
            client_id="fixture",
            _authenticated_user=self.user if authorized else None,
            _current_token=None,
            _sendspin_player_id=None,
            _logger=logging.getLogger("intent_fixture"),
            _send_message=AsyncMock(),
        )
        client._run_handler = types.MethodType(
            ws.WebsocketClientHandler._run_handler, client
        )
        await ws.WebsocketClientHandler._handle_command(
            client, ws.CommandMessage("fixture", "players/cmd/stop", args)
        )
        if self.tasks:
            await asyncio.gather(*self.tasks)

    async def test_http_and_websocket_user_controls_have_pending_then_accepted(
        self,
    ) -> None:
        for transport in ("http", "ws"):
            with self.subTest(transport=transport):
                records = await self.invoke(transport)
                self.assertEqual([r["phase"] for r in records], ["pending", "accepted"])
                self.assertEqual(records[0]["correlation"], records[1]["correlation"])
                self.assertEqual(records[0]["player_ids"], ["studio"])

    async def test_unauthenticated_and_invalid_commands_never_produce_intent(
        self,
    ) -> None:
        for transport in ("http", "ws"):
            self.assertEqual(await self.invoke(transport, authorized=False), [])
            self.assertEqual(await self.invoke(transport, invalid=True), [])

    async def test_failed_or_private_command_never_produces_accepted_telemetry(
        self,
    ) -> None:
        for transport in ("http", "ws"):
            records = await self.invoke(transport, fails=True)
            self.assertNotIn("accepted", [r["phase"] for r in records])
            self.assertEqual(await self.invoke(transport, private=True), [])

    async def test_offline_stop_cancels_before_the_failing_hardware_command(
        self,
    ) -> None:
        self.state.available = False
        for transport in ("http", "ws"):
            records = await self.invoke(transport, fails=True)
            self.assertEqual([r["phase"] for r in records], ["pending"])
            self.assertEqual(records[0]["player_ids"], ["studio"])


if __name__ == "__main__":
    unittest.main()
