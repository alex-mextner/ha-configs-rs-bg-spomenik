"""Kodi provider hooks the existing native connection without text-bearing HA services."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import jsonrpc_base
from aiohttp import StreamReader
from aiohttp.base_protocol import BaseProtocol
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame
from jsonrpc_websocket import Server

if TYPE_CHECKING:
    from collections.abc import Coroutine


ROOT = Path(__file__).resolve().parents[2]


def safe_snapshot(text: str = "preFIX", revision: int = 1) -> dict:
    return {
        "session_id": "a" * 32,
        "revision": revision,
        "focused": True,
        "sensitive": False,
        "text": text,
        "selection": None,
        "capabilities": {
            "edit": True,
            "commit": True,
            "close": True,
            "selection": False,
        },
        "status": "ready",
        "fresh_at": "2026-11-13T18:00:00+00:00",
    }


class NativeServer:
    """Model native JSON-RPC handler registration through attributes."""

    def __init__(self) -> None:
        object.__setattr__(self, "_server_request_handlers", {})

    def __setattr__(self, name: str, value: Any) -> None:
        """Register handlers using the native server assignment convention."""
        self._server_request_handlers[name] = value


class KodiProviderClientTests(unittest.IsolatedAsyncioTestCase):
    """Cover Kodi callbacks, bounded HTTP reads, and guarded edits."""

    async def asyncSetUp(self) -> None:
        path = ROOT / "custom_components/ekf_appliances/keyboard_kodi.py"
        self.assertTrue(
            path.exists(), "Kodi keyboard client provider is not implemented"
        )
        spec = importlib.util.spec_from_file_location(
            "kodi_keyboard_provider_contract", path
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tasks = []

        def create_task(
            coroutine: Coroutine[Any, Any, Any], *_args: Any, **_kwargs: Any
        ) -> asyncio.Task[Any]:
            task = asyncio.create_task(coroutine)
            self.tasks.append(task)
            return task

        self.hass = SimpleNamespace(
            async_create_task=create_task,
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        self.server = NativeServer()
        self.connection = SimpleNamespace(
            server=self.server, connected=True, can_subscribe=True
        )
        self.provider = self.module.KodiKeyboardProvider(
            self.hass,
            "media_player.home",
            "http://127.0.0.1:8787",
            "private-test-token",
        )
        self.provider._resolve_connection = Mock(return_value=self.connection)
        self.provider._post = AsyncMock(return_value={"snapshot": safe_snapshot()})
        with patch.object(
            self.module, "async_track_time_interval", return_value=lambda: None
        ):
            await self.provider.async_start()

    async def asyncTearDown(self) -> None:
        if hasattr(self, "provider"):
            await self.provider.async_shutdown()
        for task in getattr(self, "tasks", []):
            if not task.done():
                task.cancel()
        await asyncio.gather(*getattr(self, "tasks", []), return_exceptions=True)

    async def event(self, name: str, data: Any = None) -> None:
        self.server._server_request_handlers[name]("kodi", data)
        await asyncio.sleep(0)
        await asyncio.gather(*[t for t in self.tasks if not t.done()])

    async def test_fragmented_http_snapshot_is_read_through_eof(self) -> None:
        protocol = BaseProtocol(asyncio.get_running_loop())
        protocol.transport = Mock(spec=asyncio.Transport)
        stream = StreamReader(protocol, limit=131072)
        body = json.dumps(
            {"ok": True, "snapshot": safe_snapshot("fragmented")}
        ).encode()
        stream.feed_data(body[:15])
        loop = asyncio.get_running_loop()
        loop.call_soon(stream.feed_data, body[15:])
        loop.call_soon(stream.feed_eof)
        response = SimpleNamespace(content=stream, status=200)
        request = AsyncMock()
        request.__aenter__.return_value = response
        session = SimpleNamespace(post=Mock(return_value=request))
        with patch.object(self.module, "async_get_clientsession", return_value=session):
            reply = await type(self.provider)._post(
                self.provider, {"action": "snapshot"}
            )
        self.assertEqual(reply["snapshot"]["text"], "fragmented")
        self.assertTrue(stream.at_eof())

    async def test_fragmented_http_body_still_has_cumulative_size_limit(self) -> None:
        protocol = BaseProtocol(asyncio.get_running_loop())
        protocol.transport = Mock(spec=asyncio.Transport)
        stream = StreamReader(protocol, limit=262144)
        stream.feed_data(b'{"ok": true, "padding": "')
        loop = asyncio.get_running_loop()
        loop.call_soon(stream.feed_data, b"x" * 131072)
        loop.call_soon(stream.feed_data, b'"}')
        loop.call_soon(stream.feed_eof)
        response = SimpleNamespace(content=stream, status=200)
        request = AsyncMock()
        request.__aenter__.return_value = response
        session = SimpleNamespace(post=Mock(return_value=request))
        with (
            patch.object(self.module, "async_get_clientsession", return_value=session),
            self.assertRaises(self.module.KodiKeyboardError) as caught,
        ):
            await type(self.provider)._post(self.provider, {"action": "snapshot"})
        self.assertEqual(caught.exception.code, "unavailable")

    async def test_actual_input_event_sends_only_type_and_returns_observed_field(
        self,
    ) -> None:
        await self.event(
            "Input.OnInputRequested",
            {
                "type": "keyboard",
                "title": "Private title",
                "value": "private raw value",
            },
        )
        self.provider._post.assert_awaited_once_with(
            {"action": "context", "kind": "keyboard"}
        )
        self.assertEqual((await self.provider.async_snapshot())["text"], "preFIX")
        self.hass.services.async_call.assert_not_called()

    async def test_password_event_never_publishes_text_even_if_host_reply_is_wrong(
        self,
    ) -> None:
        observed = []
        self.provider.subscribe(observed.append)
        await self.event(
            "Input.OnInputRequested", {"type": "password", "value": "PASSWORD"}
        )
        self.assertTrue(observed)
        self.assertTrue(all("text" not in snapshot for snapshot in observed))
        self.assertNotIn("text", await self.provider.async_snapshot())
        self.assertEqual(
            self.provider._post.await_args.args[0],
            {"action": "context", "kind": "password"},
        )

    async def test_phone_edit_calls_private_host_directly_and_does_not_use_ha_services(
        self,
    ) -> None:
        await self.event("Input.OnInputRequested", {"type": "keyboard"})
        self.provider._post.reset_mock()
        self.provider._post.return_value = {
            "snapshot": safe_snapshot("preNEWFIX", 2),
            "verified": True,
        }
        result = await self.provider.async_edit("a" * 32, 1, "preNEWFIX")
        self.assertEqual(result["text"], "preNEWFIX")
        self.provider._post.assert_awaited_once_with(
            {
                "action": "edit",
                "session_id": "a" * 32,
                "expected_revision": 1,
                "text": "preNEWFIX",
            }
        )
        self.hass.services.async_call.assert_not_called()

    async def test_queued_edit_is_rejected_when_projector_shutdown_starts(self) -> None:
        await self.event("Input.OnInputRequested", {"type": "keyboard"})
        self.provider._post.reset_mock()
        allowed = True
        self.provider.command_allowed = lambda: allowed
        await self.provider._lock.acquire()
        operation = asyncio.create_task(self.provider.async_edit("a" * 32, 1, "new"))
        await asyncio.sleep(0)
        allowed = False
        self.provider._lock.release()
        with self.assertRaises(self.module.KodiKeyboardError) as caught:
            await operation
        self.assertEqual(caught.exception.code, "unavailable")
        self.provider._post.assert_not_awaited()

    async def test_unknown_type_and_finished_event_revoke_cached_text(self) -> None:
        await self.event("Input.OnInputRequested", {"type": "keyboard"})
        await self.event("Input.OnInputRequested", {"type": "numeric", "value": "1234"})
        self.assertNotIn("text", await self.provider.async_snapshot())
        await self.event("Input.OnInputFinished")
        self.assertNotIn("text", await self.provider.async_snapshot())
        self.assertFalse((await self.provider.async_snapshot())["capabilities"]["edit"])

    async def test_shutdown_removes_only_callbacks_that_this_provider_still_owns(
        self,
    ) -> None:
        foreign = Mock()
        self.server._server_request_handlers["Input.OnInputRequested"] = foreign
        await self.provider.async_shutdown()
        self.assertIs(
            self.server._server_request_handlers["Input.OnInputRequested"], foreign
        )
        self.assertNotIn("Input.OnInputFinished", self.server._server_request_handlers)

    async def test_terminal_ack_does_not_overwrite_a_newly_opened_native_field(
        self,
    ) -> None:
        await self.event("Input.OnInputRequested", {"type": "keyboard"})
        entered, release = asyncio.Event(), asyncio.Event()
        second = safe_snapshot("new field", 1)
        second["session_id"] = "b" * 32
        terminal = {
            "session_id": "c" * 32,
            "revision": 0,
            "focused": False,
            "sensitive": False,
            "selection": None,
            "capabilities": {
                "edit": False,
                "commit": False,
                "close": False,
                "selection": False,
            },
            "status": "idle",
            "fresh_at": None,
        }

        async def post(payload: dict) -> dict:
            if payload["action"] == "commit":
                entered.set()
                await release.wait()
                return {"snapshot": terminal, "verified": True}
            return {"snapshot": second}

        self.provider._post.side_effect = post
        commit = asyncio.create_task(self.provider.async_commit("a" * 32, 1))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.server._server_request_handlers["Input.OnInputFinished"]("kodi", None)
            self.server._server_request_handlers["Input.OnInputRequested"](
                "kodi", {"type": "keyboard"}
            )
        finally:
            release.set()
        ack = await commit
        await asyncio.gather(*[task for task in self.tasks if not task.done()])
        self.assertEqual(ack["status"], "idle")
        current = await self.provider.async_snapshot()
        self.assertEqual(current["session_id"], "b" * 32)
        self.assertEqual(current["text"], "new field")

    async def test_rebinding_connected_native_server_clears_old_text_and_rejects_late_callbacks(
        self,
    ) -> None:
        await self.event("Input.OnInputRequested", {"type": "keyboard"})
        old_requested = self.server._server_request_handlers["Input.OnInputRequested"]
        old_finished = self.server._server_request_handlers["Input.OnInputFinished"]
        self.server = NativeServer()
        self.connection = SimpleNamespace(
            server=self.server, connected=True, can_subscribe=True
        )
        self.provider._resolve_connection.return_value = self.connection
        published = []
        self.provider.subscribe(published.append)
        self.assertTrue(self.provider._ensure_connection())
        with self.subTest(boundary="connected_to_connected_rebind"):
            current = await self.provider.async_snapshot()
            self.assertNotIn("text", current)
            self.assertFalse(current["capabilities"]["edit"])
            self.assertTrue(published)
        await self.event("Input.OnInputRequested", {"type": "password"})
        before = await self.provider.async_snapshot()
        self.provider._post.reset_mock()
        old_requested("old-kodi", {"type": "keyboard", "value": "old-private-value"})
        old_finished("old-kodi", None)
        await asyncio.sleep(0)
        await asyncio.gather(*[task for task in self.tasks if not task.done()])
        with self.subTest(boundary="late_old_callbacks"):
            self.provider._post.assert_not_called()
            self.assertEqual(await self.provider.async_snapshot(), before)
            self.assertNotIn("text", await self.provider.async_snapshot())

    async def test_real_native_jsonrpc_notification_does_not_log_keyboard_content_at_debug(
        self,
    ) -> None:

        private_text = "PRIVATE-NATIVE-KEYBOARD-FIXTURE"
        server = Server("ws://127.0.0.1:9090/jsonrpc", session=SimpleNamespace())
        connection = SimpleNamespace(server=server, connected=True, can_subscribe=True)
        self.provider._resolve_connection.return_value = connection
        self.provider._ensure_connection()
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        loggers = [
            logging.getLogger(name)
            for name in (
                "jsonrpc_websocket.jsonrpc",
                "jsonrpc_base.jsonrpc",
                "pykodi.kodi",
            )
        ]
        previous = [(logger, logger.level, logger.propagate) for logger in loggers]
        try:
            for logger in loggers:
                logger.setLevel(logging.DEBUG)
                logger.propagate = False
                logger.addHandler(handler)
            request = jsonrpc_base.Request.parse(
                {
                    "jsonrpc": "2.0",
                    "method": "Input.OnInputRequested",
                    "params": {
                        "sender": "xbmc",
                        "data": {
                            "type": "keyboard",
                            "title": private_text,
                            "value": private_text,
                        },
                    },
                }
            )
            await server._receive_request(request)
            await asyncio.gather(*[task for task in self.tasks if not task.done()])
            self.assertEqual(
                self.provider._post.await_args.args[0],
                {"action": "context", "kind": "keyboard"},
            )
            self.assertNotIn(private_text, output.getvalue())
            self.hass.services.async_call.assert_not_called()
        finally:
            for logger, level, propagate in previous:
                logger.removeHandler(handler)
                logger.setLevel(level)
                logger.propagate = propagate
            await server.close()

    async def test_actual_homeassistant_interval_dispatch_keeps_poll_on_event_loop(
        self,
    ) -> None:

        with tempfile.TemporaryDirectory() as config_dir:
            hass = HomeAssistant(config_dir)
            loader.async_setup(hass)
            frame.async_setup(hass)
            provider = self.module.KodiKeyboardProvider(
                hass, "media_player.home", "http://127.0.0.1:8787", "private-test-token"
            )
            server = NativeServer()
            connection = SimpleNamespace(
                server=server, connected=True, can_subscribe=True
            )
            provider._resolve_connection = Mock(return_value=connection)
            changed = asyncio.Event()
            owner_thread = threading.get_ident()
            observed_threads = []
            original_ensure = provider._ensure_connection

            def ensure_connection() -> bool:
                observed_threads.append(threading.get_ident())
                return original_ensure()

            async def post(payload: dict) -> dict:
                if payload["action"] == "context":
                    return {"snapshot": safe_snapshot("", 1)}
                changed.set()
                return {"snapshot": safe_snapshot("a", 2)}

            provider._ensure_connection = ensure_connection
            provider._post = AsyncMock(side_effect=post)
            try:
                # Real HA async_track_time_interval and HassJob dispatch. Never
                # call _tick ourselves or replace the scheduler with a lambda.
                await provider.async_start()
                server._server_request_handlers["Input.OnInputRequested"](
                    "kodi", {"type": "keyboard"}
                )
                await hass.async_block_till_done()
                observed_threads.clear()
                await asyncio.wait_for(changed.wait(), 1.5)
                await hass.async_block_till_done()
                self.assertTrue(observed_threads)
                self.assertTrue(
                    all(thread == owner_thread for thread in observed_threads)
                )
                current = await provider.async_snapshot()
                self.assertEqual(current["text"], "a")
                self.assertEqual(current["revision"], 2)
            finally:
                await provider.async_shutdown()
                await hass.async_block_till_done()
                await hass.async_stop()
