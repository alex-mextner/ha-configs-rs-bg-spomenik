"""Regression checks for moving HA away from the desktop host."""
import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

ROOT = Path(__file__).resolve().parents[2]


def bridge():
    spec = importlib.util.spec_from_file_location("native_bridge_test", ROOT / "scripts/chatgpt-native/bridge.py")
    module = importlib.util.module_from_spec(spec)
    with patch.object(web, "run_app"):
        spec.loader.exec_module(module)
    assert hasattr(module, "create_app"), "Bridge must accept an explicit private peer instead of an old Docker IP"
    return module


def test_backend_is_configurable():
    from custom_components import chatgpt_native as native
    assert hasattr(native, "CONFIG_SCHEMA"), "Migrated HA needs a configurable backend URL"
    config = native.CONFIG_SCHEMA({"chatgpt_native": {"backend_url": "http://100.74.26.95:6081"}})
    assert config["chatgpt_native"]["backend_url"] == "http://100.74.26.95:6081"


@pytest.mark.asyncio
async def test_bridge_allows_configured_peer(tmp_path):
    (tmp_path / "vnc.html").write_text("native-desktop")
    async with TestClient(TestServer(bridge().create_app(tmp_path, {"127.0.0.1"}, 5901))) as client:
        response = await client.get("/vnc.html")
        assert response.status == 200
        assert await response.text() == "native-desktop"


@pytest.mark.asyncio
async def test_bridge_rejects_other_peers(tmp_path):
    (tmp_path / "vnc.html").write_text("private")
    async with TestClient(TestServer(bridge().create_app(tmp_path, {"100.116.57.66"}, 5901))) as client:
        assert (await client.get("/vnc.html")).status == 403


@pytest.mark.asyncio
async def test_bridge_passes_real_rfb_bytes_both_ways(tmp_path):
    received = asyncio.Future()
    async def tcp(reader, writer):
        try:
            writer.write(b"RFB 003.008\n")
            await writer.drain()
            received.set_result(await reader.readexactly(12))
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(tcp, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with TestClient(TestServer(bridge().create_app(tmp_path, {"127.0.0.1"}, port))) as client:
            async with client.ws_connect("/websockify", protocols=["binary"]) as ws:
                message = await ws.receive(timeout=3)
                assert message.type == WSMsgType.BINARY
                assert message.data == b"RFB 003.008\n"
                await ws.send_bytes(message.data)
                assert await asyncio.wait_for(received, 3) == message.data
    finally:
        server.close()
        await server.wait_closed()


def profile_module():
    path = ROOT / "scripts/chatgpt-native/profile.py"
    assert path.exists(), "Startup must handle an absent profile volume explicitly"
    spec = importlib.util.spec_from_file_location("native_profile_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_persistent_profile_is_not_silently_recreated(tmp_path):
    module = profile_module()
    with pytest.raises(RuntimeError, match="volume"):
        module.resolve_profile(tmp_path / "unmounted/profile", tmp_path, ephemeral=False)
    assert not (tmp_path / "unmounted").exists()


def test_ephemeral_profile_requires_ram_filesystem(tmp_path):
    module = profile_module()
    with patch.object(module, "filesystem_type", return_value="ext4"):
        with pytest.raises(RuntimeError, match="tmpfs"):
            module.resolve_profile(tmp_path / "persistent", tmp_path, ephemeral=True)


def test_explicit_ram_profile_never_touches_old_profile(tmp_path):
    module = profile_module()
    with patch.object(module, "filesystem_type", return_value="tmpfs"):
        profile = module.resolve_profile(tmp_path / "persistent", tmp_path, ephemeral=True)
    assert profile.is_dir() and profile.stat().st_mode & 0o777 == 0o700
    assert not (tmp_path / "persistent").exists()
