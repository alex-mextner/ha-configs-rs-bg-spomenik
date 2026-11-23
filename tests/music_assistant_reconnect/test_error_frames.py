# Copyright (c) 2026 Alex Ultra.
"""
Regression cases from python-hass-client ab1bd37043a0447b526654112b9d8594b12757de.

Exercise the real 1.3.1 listener with aiohttp ERROR frames. Authentication is
mocked; no Home Assistant, network connection, or Music Assistant data is used.
"""

import asyncio
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from hass_client.client import HomeAssistantClient
from hass_client.exceptions import ConnectionFailed, ConnectionFailedDueToLargeMessage


class ErrorFrameTests(unittest.IsolatedAsyncioTestCase):
    """Exercise upstream ERROR-frame listener behavior."""

    async def check_error(self, error, expected, max_msg_size=1024) -> ConnectionFailed:
        websocket = MagicMock()
        websocket.receive_json = AsyncMock(
            side_effect=[
                {"type": "auth_required", "ha_version": "2026.9.3"},
                {"type": "auth_ok", "ha_version": "2026.9.3"},
            ]
        )
        websocket.send_json = AsyncMock()
        websocket.receive = AsyncMock(
            return_value=aiohttp.WSMessage(aiohttp.WSMsgType.ERROR, error, None)
        )
        websocket.closed = False

        async def close() -> None:
            websocket.closed = True

        websocket.close = AsyncMock(side_effect=close)
        session = MagicMock(spec=aiohttp.ClientSession)
        session.ws_connect = AsyncMock(return_value=websocket)
        client = HomeAssistantClient(
            "ws://unit.test/api/websocket",
            "unit-test-placeholder",
            session,
            max_msg_size=max_msg_size,
        )
        with self.assertRaises(expected) as caught:
            await asyncio.wait_for(client.start_listening(), timeout=1)
        self.assertIs(type(caught.exception), expected)
        self.assertIs(caught.exception.error, error)
        websocket.close.assert_awaited_once()
        self.assertFalse(client.connected)
        self.assertFalse(client._listening)
        return caught.exception

    async def test_heartbeat_timeout_is_reconnectable(self) -> None:
        await self.check_error(
            aiohttp.ServerTimeoutError("No PONG received after 27.5 seconds"),
            ConnectionFailed,
        )

    async def test_connection_reset_is_reconnectable(self) -> None:
        await self.check_error(
            ConnectionResetError("Connection reset by peer"),
            ConnectionFailed,
        )

    async def test_websocket_protocol_error_stays_reconnectable(self) -> None:
        await self.check_error(
            aiohttp.WebSocketError(
                aiohttp.WSCloseCode.PROTOCOL_ERROR, "protocol error"
            ),
            ConnectionFailed,
        )

    async def test_message_too_large_keeps_distinct_exception(self) -> None:
        error = await self.check_error(
            aiohttp.WebSocketError(aiohttp.WSCloseCode.MESSAGE_TOO_BIG, "too large"),
            ConnectionFailedDueToLargeMessage,
        )
        self.assertEqual(error.max_msg_size, 1024)


if __name__ == "__main__":
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="ma-hass-client-regression-", suffix=".log", delete=False
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
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ErrorFrameTests)
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
        output.write(
            f"{datetime.now(UTC).isoformat()} EXIT {int(not result.wasSuccessful())}\n"
        )
        output.flush()
        sys.exit(not result.wasSuccessful())
