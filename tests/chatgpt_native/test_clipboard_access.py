"""Clipboard access must retain HA administrator authorization on every call."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from custom_components import chatgpt_native as native


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user",
    [
        None,
        SimpleNamespace(is_admin=False, is_active=True),
        SimpleNamespace(is_admin=True, is_active=False),
    ],
)
async def test_clipboard_refuses_non_administrators_before_proxying(user):
    assert hasattr(native, "ClipboardView"), (
        "An authenticated clipboard route is required"
    )
    view = native.ClipboardView.__new__(native.ClipboardView)
    view._proxy = AsyncMock()
    for method in (view.get, view.post):
        with pytest.raises((web.HTTPUnauthorized, web.HTTPForbidden)):
            await method({"hass_user": user})
    view._proxy.assert_not_awaited()


@pytest.mark.asyncio
async def test_clipboard_get_is_authenticated_and_not_public():
    assert hasattr(native, "ClipboardView"), (
        "An authenticated clipboard route is required"
    )
    assert native.ClipboardView.requires_auth
    view = native.ClipboardView.__new__(native.ClipboardView)
    view._proxy = AsyncMock(return_value="OK")
    assert (
        await view.get({"hass_user": SimpleNamespace(is_admin=True, is_active=True)})
        == "OK"
    )
    view._proxy.assert_awaited_once()
