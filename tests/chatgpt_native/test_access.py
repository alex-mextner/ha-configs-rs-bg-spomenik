"""Authorization regressions with no real account or credential changes."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp import web

spec = importlib.util.spec_from_file_location(
    "native", Path(__file__).parents[2] / "custom_components/chatgpt_native/__init__.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def make_hass() -> tuple[SimpleNamespace, str, SimpleNamespace]:
    """Build an isolated session store and current-user resolver."""
    session = uuid4().hex
    admin = SimpleNamespace(id="a", is_active=True, is_admin=True)
    store = SimpleNamespace(
        check_user_token=lambda value: {"id": "a"} if value == session else None
    )
    auth = SimpleNamespace(async_get_user=AsyncMock(return_value=admin))
    hass = SimpleNamespace(data={"ingress": {"config": store}}, auth=auth)
    return hass, session, admin


@pytest.mark.asyncio
async def test_current_administrator_is_allowed() -> None:
    hass, session, admin = make_hass()
    if await module.require_admin(hass, session) is not admin:
        pytest.fail("The current administrator was not resolved")


@pytest.mark.asyncio
@pytest.mark.parametrize("session", ["", "expired"])
async def test_missing_or_expired_session_is_unauthorized(session: str) -> None:
    hass, _, _ = make_hass()
    with pytest.raises(web.HTTPUnauthorized):
        await module.require_admin(hass, session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user",
    [
        None,
        SimpleNamespace(is_active=False, is_admin=True),
        SimpleNamespace(is_active=True, is_admin=False),
    ],
)
async def test_deleted_inactive_or_non_admin_user_is_forbidden(
    user: SimpleNamespace | None,
) -> None:
    hass, session, _ = make_hass()
    hass.auth.async_get_user.return_value = user
    with pytest.raises(web.HTTPForbidden):
        await module.require_admin(hass, session)


@pytest.mark.asyncio
async def test_role_revocation_invalidates_existing_session() -> None:
    hass, session, admin = make_hass()
    await module.require_admin(hass, session)
    admin.is_admin = False
    with pytest.raises(web.HTTPForbidden):
        await module.require_admin(hass, session)


def test_empty_yaml_configuration_is_accepted() -> None:
    config = {"chatgpt_native": {}}
    if module.CONFIG_SCHEMA(config)["chatgpt_native"]["backend_url"] != module.DEFAULT_BACKEND:
        pytest.fail("The existing empty YAML configuration was changed")
