import asyncio
import importlib.util
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[2]


def module():
    spec = importlib.util.spec_from_file_location(
        "desktop_bridge", ROOT / "scripts/chatgpt-native/bridge.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_private_unix_socket_passes_rfb_and_input(tmp_path):
    sock = tmp_path / "vnc.sock"
    echoed = asyncio.Future()

    async def rfb(reader, writer):
        writer.write(b"RFB 003.008\n")
        await writer.drain()
        echoed.set_result(await reader.readexactly(12))
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(rfb, path=sock)
    try:
        async with TestClient(
            TestServer(
                module().create_app(tmp_path, {"127.0.0.1"}, vnc_socket=str(sock))
            )
        ) as client:
            async with client.ws_connect("/websockify", protocols=["binary"]) as ws:
                msg = await ws.receive(timeout=3)
                assert msg.data == b"RFB 003.008\n"
                await ws.send_bytes(msg.data)
                assert await asyncio.wait_for(echoed, 3) == msg.data
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_absent_socket_returns_service_unavailable_not_a_fake_desktop(tmp_path):
    async with TestClient(
        TestServer(
            module().create_app(
                tmp_path, {"127.0.0.1"}, vnc_socket=str(tmp_path / "missing.sock")
            )
        )
    ) as client:
        assert (await client.get("/health")).status == 503
        assert (await client.get("/websockify")).status == 503


@pytest.mark.asyncio
async def test_physical_desktop_does_not_trust_forwarded_peer(tmp_path):
    async with TestClient(
        TestServer(
            module().create_app(
                tmp_path, {"100.116.57.66"}, vnc_socket=str(tmp_path / "missing.sock")
            )
        )
    ) as client:
        response = await client.get(
            "/health", headers={"X-Forwarded-For": "100.116.57.66"}
        )
        assert response.status == 403
