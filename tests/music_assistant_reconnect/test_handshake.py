# Copyright (c) 2026 Alex Ultra.
# ruff: noqa: E402 -- imports follow optional verified isolated candidate loading.
"""
Behavioral regressions for HA handshake closure and Music Assistant cleanup.

Uses installed upstream packages, a loopback WebSocket server, and isolated
provider state. No household server, credentials, playback, or runtime data.
Set MA_CANDIDATE_PATCHER to exercise patched copies in this process only.
"""

import asyncio
import atexit
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web


# Candidate source is loaded into this standalone test process, never the server.
def load_candidate() -> None:
    path = os.environ.get("MA_CANDIDATE_PATCHER")
    if not path:
        return
    import hass_client  # noqa: PLC0415 -- optional isolated candidate loader
    import hass_client.client as client_module  # noqa: PLC0415
    import music_assistant.providers.hass as provider_module  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location("candidate_guard", path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    source_dir = tempfile.TemporaryDirectory(prefix="ma-reconnect-candidate-")
    atexit.register(source_dir.cleanup)
    client_copy = Path(source_dir.name) / "client.py"
    provider_copy = Path(source_dir.name) / "provider.py"
    client_copy.write_bytes(Path(client_module.__file__).read_bytes())
    provider_copy.write_bytes(Path(provider_module.__file__).read_bytes())
    guard.apply(client_copy)
    guard.apply_provider(provider_copy)
    client_module.__file__ = str(client_copy)
    provider_module.__file__ = str(provider_copy)
    exec(  # noqa: S102 -- complete module hashes verified above
        compile(client_copy.read_bytes(), str(client_copy), "exec"),
        client_module.__dict__,
    )
    hass_client.HomeAssistantClient = client_module.HomeAssistantClient
    exec(  # noqa: S102 -- complete module hashes verified above
        compile(provider_copy.read_bytes(), str(provider_copy), "exec"),
        provider_module.__dict__,
    )


load_candidate()

from hass_client.client import HomeAssistantClient
from hass_client.exceptions import AuthenticationFailed as HassAuthenticationFailed
from hass_client.exceptions import CannotConnect
from music_assistant.mass import MusicAssistant
from music_assistant.providers.hass import (
    SEARCH_CONTROL_ENTITIES_COMMAND,
    HomeAssistantProvider,
)
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import AuthenticationFailed, SetupFailedError
from music_assistant_models.provider import ProviderManifest


class LocalHandshakeTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real client against an isolated loopback server."""

    async def run_handshake(self, scenario, expected) -> None:
        async def endpoint(request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            if scenario == "before_greeting":
                await ws.close(code=1000)
                return ws
            await ws.send_json({"type": "auth_required", "ha_version": "2026.9.3"})
            await ws.receive_json()
            if scenario == "after_auth":
                await ws.close(code=1000)
                return ws
            await ws.send_json(
                {"type": "auth_invalid", "message": "Invalid access token"}
            )
            await ws.close(code=1000)
            return ws

        app = web.Application()
        app.router.add_get("/api/websocket", endpoint)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                client = HomeAssistantClient(
                    f"ws://127.0.0.1:{port}/api/websocket",
                    "test-token-not-a-secret",
                    session,
                )
                try:
                    with self.assertRaises(expected) as caught:
                        await asyncio.wait_for(client.connect(), 2)
                    self.assertIs(type(caught.exception), expected)
                finally:
                    await client.disconnect()
                self.assertFalse(client.connected)
        finally:
            await runner.cleanup()

    async def test_close_before_greeting_is_retryable(self) -> None:
        await self.run_handshake("before_greeting", CannotConnect)

    async def test_close_after_auth_is_retryable(self) -> None:
        await self.run_handshake("after_auth", CannotConnect)

    async def test_real_auth_invalid_remains_authentication_failure(self) -> None:
        await self.run_handshake("invalid", HassAuthenticationFailed)

    async def test_unrelated_type_error_is_not_network_failure(self) -> None:
        async with aiohttp.ClientSession() as session:
            client = HomeAssistantClient(
                "ws://unit.test/api/websocket", "test", session
            )
            with (
                patch.object(
                    session,
                    "ws_connect",
                    AsyncMock(side_effect=TypeError("programming bug")),
                ),
                self.assertRaises(TypeError),
            ):
                await client.connect()
            await client.disconnect()


class ProviderHarness:
    """Retain registration and retry state without a Music Assistant server."""

    def __init__(self) -> None:
        self.registry = {}
        self.timers = []
        self._tracked_timers = {}
        self.cache = None
        self.http_session = None
        self.http_session_no_ssl = None
        self.providers = []
        self.setup_data = {
            "url": "http://unit.test",
            "token": "test-token-not-a-secret",
        }
        self.provider_config = ProviderConfig(
            values={}, type=ProviderType.PLUGIN, domain="hass", instance_id="hass"
        )
        self.config = types.SimpleNamespace(
            get=lambda _key: self.setup_data,
            decrypt_string=lambda value: value,
            get_provider_config=AsyncMock(return_value=self.provider_config),
            update_provider_last_error=lambda _instance_id, _error: None,
        )
        self.load_provider = self.reload

    async def reload(self, *args: object, **kwargs: object) -> object:
        return await MusicAssistant.load_provider(self, *args, **kwargs)

    def register_api_command(self, command, handler, **_kwargs: object) -> object:
        if command in self.registry:
            message = f"already registered: {command}"
            raise RuntimeError(message)
        self.registry[command] = handler
        return lambda: self.registry.pop(command, None)

    def create_task(self, coro) -> asyncio.Task:
        return asyncio.create_task(coro)

    def call_later(self, *args: object, **kwargs: object) -> None:
        self.timers.append((args, kwargs))

    def provider(self) -> HomeAssistantProvider:
        manifest = ProviderManifest(
            type=ProviderType.PLUGIN,
            domain="hass",
            name="Home Assistant",
            description="test",
            codeowners=[],
        )
        provider = HomeAssistantProvider(self, manifest, self.provider_config)
        provider._subscribe_entity_registry = AsyncMock()
        provider._resolve_startup_features = AsyncMock()
        return provider


class FakeClient:
    """Model connect failures and cleanup without household connections."""

    def __init__(self, error=None) -> None:
        self.error = error
        self.disconnected = False
        self.connected = False

    async def connect(self) -> None:
        if self.error:
            raise self.error
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True
        self.connected = False

    async def start_listening(self) -> None:
        await asyncio.Event().wait()


class ProviderCleanupTests(unittest.IsolatedAsyncioTestCase):
    """Exercise actual provider cleanup and loader retry decisions."""

    async def failing_init(self, error, expected) -> tuple[ProviderHarness, Exception]:
        mass = ProviderHarness()
        provider = mass.provider()
        client = FakeClient(error)
        with (
            patch(
                "music_assistant.providers.hass.HomeAssistantClient",
                return_value=client,
            ),
            self.assertRaises(expected) as caught,
        ):
            await provider.handle_async_init()
        self.assertIs(type(caught.exception), expected)
        self.assertEqual(mass.registry, {})
        self.assertTrue(client.disconnected)
        self.assertTrue(provider._control_entity_search._closed)
        # The exact command can be registered on a subsequent successful setup.
        next_provider = mass.provider()
        next_client = FakeClient()
        with patch(
            "music_assistant.providers.hass.HomeAssistantClient",
            return_value=next_client,
        ):
            await next_provider.handle_async_init()
            self.assertEqual(list(mass.registry), [SEARCH_CONTROL_ENTITIES_COMMAND])
            await next_provider.unload()
        self.assertEqual(mass.registry, {})
        self.assertTrue(next_client.disconnected)
        self.assertEqual(mass.timers, [])
        return mass, caught.exception

    async def assert_retry_count(self, mass, error, expected_count) -> None:
        mass.load_provider_config = AsyncMock(side_effect=error)
        with self.assertLogs("music_assistant", level="WARNING"):
            await MusicAssistant.load_provider(mass, "hass", allow_retry=True)
        self.assertEqual(len(mass.timers), expected_count)
        if expected_count:
            args, kwargs = mass.timers[0]
            self.assertEqual(args[0], 120)
            self.assertEqual(args[2], "hass")
            self.assertTrue(args[3])
            self.assertEqual(kwargs["task_id"], "load_provider_hass")

    async def test_transport_failure_cleans_up_and_schedules_one_retry(self) -> None:
        mass, error = await self.failing_init(
            CannotConnect("closed during auth"), SetupFailedError
        )
        await self.assert_retry_count(mass, error, 1)

    async def test_unexpected_error_cleans_up_and_is_not_retried(self) -> None:
        mass, error = await self.failing_init(TypeError("programming bug"), TypeError)
        await self.assert_retry_count(mass, error, 0)

    async def test_invalid_credentials_clean_up_and_do_not_retry(self) -> None:
        mass, error = await self.failing_init(
            HassAuthenticationFailed("invalid token"), AuthenticationFailed
        )
        await self.assert_retry_count(mass, error, 0)

    async def test_cancelled_connect_cleans_up_without_retry(self) -> None:
        mass = ProviderHarness()
        provider = mass.provider()
        entered = asyncio.Event()
        client = FakeClient()

        async def waiting_connect() -> None:
            entered.set()
            await asyncio.Event().wait()

        client.connect = waiting_connect
        with patch(
            "music_assistant.providers.hass.HomeAssistantClient", return_value=client
        ):
            task = asyncio.create_task(provider.handle_async_init())
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(mass.registry, {})
        self.assertTrue(client.disconnected)
        self.assertEqual(mass.timers, [])


if __name__ == "__main__":
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="ma-hass-handshake-regression-", suffix=".log", delete=False
    ) as log:

        class Tee:
            """Save standalone test output while displaying progress."""

            def write(self, text) -> None:
                sys.stderr.write(text)
                log.write(text)

            def flush(self) -> None:
                sys.stderr.flush()
                log.flush()

        output = Tee()
        output.write(f"{datetime.now(UTC).isoformat()} Log {log.name}\n")
        suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
        output.write(
            f"{datetime.now(UTC).isoformat()} EXIT {int(not result.wasSuccessful())}\n"
        )
        output.flush()
        sys.exit(not result.wasSuccessful())
