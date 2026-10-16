"""The desktop clipboard is text-only, bounded, and inaccessible while locked."""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader and Path(spec.origin).is_file(), (
        "Clipboard support is missing"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clip_module():
    return load("text_clipboard", "scripts/physical-desktop/text_clipboard.py")


def test_unicode_text_and_limits():
    mod = clip_module()
    assert mod.validate_text("Привет 👌\nsecond line") == "Привет 👌\nsecond line"
    for invalid in (None, 42, "bad\0text", "я" * 40000):
        with pytest.raises(ValueError):
            mod.validate_text(invalid)


@pytest.mark.asyncio
async def test_locked_desktop_never_reads_or_changes_clipboard():
    mod = clip_module()
    clipboard = mod.DesktopClipboard(Path("/missing/xclip"))
    clipboard.locked = AsyncMock(return_value=True)
    clipboard._read = AsyncMock()
    clipboard._write = AsyncMock()
    for operation in (clipboard.get_text(), clipboard.set_text("private")):
        with pytest.raises(mod.DesktopLocked):
            await operation
    clipboard._read.assert_not_awaited()
    clipboard._write.assert_not_awaited()


@pytest.mark.asyncio
async def test_lock_during_read_does_not_disclose_old_text():
    mod = clip_module()
    clipboard = mod.DesktopClipboard(Path("/missing/xclip"))
    clipboard.locked = AsyncMock(side_effect=[False, True])
    clipboard._read = AsyncMock(return_value="must not leave the host")
    with pytest.raises(mod.DesktopLocked):
        await clipboard.get_text()


@pytest.mark.asyncio
async def test_backend_clipboard_uses_only_trusted_peer_and_json(tmp_path):
    bridge = load("clipboard_bridge", "scripts/chatgpt-native/bridge.py")
    clipboard = AsyncMock()
    clipboard.get_text.return_value = "Привет 👌"
    app = bridge.create_app(tmp_path, {"127.0.0.1"}, clipboard=clipboard)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/clipboard")
        assert response.status == 200
        assert (await response.json())["text"] == "Привет 👌"
        assert response.headers["Cache-Control"] == "no-store"
        assert (
            await client.post("/clipboard", json={"text": "из телефона"})
        ).status == 200
        clipboard.set_text.assert_awaited_once_with("из телефона")
        assert (
            await client.post(
                "/clipboard",
                data='{"text":"CSRF"}',
                headers={"Content-Type": "text/plain"},
            )
        ).status == 415
        assert (
            await client.post("/clipboard", json={"text": "я" * 40000})
        ).status == 400
    app = bridge.create_app(tmp_path, {"100.116.57.66"}, clipboard=clipboard)
    async with TestClient(TestServer(app)) as client:
        assert (
            await client.get("/clipboard", headers={"X-Forwarded-For": "100.116.57.66"})
        ).status == 403


@pytest.mark.asyncio
async def test_backend_reports_lock_without_returning_clipboard(tmp_path):
    mod = clip_module()
    bridge = load("clipboard_bridge", "scripts/chatgpt-native/bridge.py")
    clipboard = AsyncMock()
    clipboard.get_text.side_effect = mod.DesktopLocked()
    async with TestClient(
        TestServer(bridge.create_app(tmp_path, {"127.0.0.1"}, clipboard=clipboard))
    ) as client:
        response = await client.get("/clipboard")
        assert response.status == 423
        assert "text" not in await response.json()


@pytest.mark.asyncio
async def test_expired_queued_write_never_changes_the_clipboard():
    import asyncio

    mod = clip_module()
    clipboard = mod.DesktopClipboard(Path("/missing/xclip"))
    clipboard.operation_timeout = 0.05
    clipboard.locked = AsyncMock(return_value=False)
    clipboard._write = AsyncMock()
    async with clipboard._mutex:
        pending = asyncio.create_task(clipboard.set_text("must expire in the queue"))
        await asyncio.sleep(0.08)
    with pytest.raises(TimeoutError):
        await pending
    clipboard._write.assert_not_awaited()


@pytest.mark.asyncio
async def test_lock_during_write_revokes_the_owned_selection():
    mod = clip_module()
    clipboard = mod.DesktopClipboard(Path("/missing/xclip"))
    clipboard.locked = AsyncMock(side_effect=[False, True])

    async def acquire(text):
        clipboard._owner = object()

    clipboard._write = AsyncMock(side_effect=acquire)
    clipboard.close = AsyncMock()
    with pytest.raises(mod.DesktopLocked):
        await clipboard.set_text("in-flight text")
    clipboard.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_failure_before_acquiring_clipboard_preserves_previous_owner():
    mod = clip_module()
    clipboard = mod.DesktopClipboard(Path("/missing/xclip"))
    previous = object()
    clipboard._owner = previous
    clipboard.locked = AsyncMock(return_value=False)
    clipboard._write = AsyncMock(side_effect=RuntimeError("Display unavailable"))
    clipboard.close = AsyncMock()
    with pytest.raises(RuntimeError, match="Display unavailable"):
        await clipboard.set_text("not delivered")
    assert clipboard._owner is previous
    clipboard.close.assert_not_awaited()
