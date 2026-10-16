"""Expose the host's separate native GUI through existing HA administrator auth."""

import asyncio
from pathlib import Path
from typing import Any

import voluptuous as vol
from aiohttp import WSMsgType, web, ClientTimeout
from homeassistant.auth.models import User
from homeassistant.components import panel_custom
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import config_validation as cv

BASE = "/api/chatgpt_native"
DEFAULT_BACKEND = "http://127.0.0.1:6081"
CONFIG_SCHEMA = vol.Schema(
    {
        "chatgpt_native": vol.Schema(
            {
                vol.Optional("backend_url", default=DEFAULT_BACKEND): cv.url,
            }
        ),
    },
    extra=vol.ALLOW_EXTRA,
)
COOKIE = "ha_ingress_session"


async def require_admin(hass: HomeAssistant, session: str) -> User:
    """Resolve an existing Ingress session and recheck current HA permissions."""
    store = hass.data.get("ingress", {}).get("config")
    info = store.check_user_token(session) if store else None
    if not info:
        raise web.HTTPUnauthorized
    user = await hass.auth.async_get_user(info["id"])
    if not user or not user.is_active or not user.is_admin:
        raise web.HTTPForbidden
    return user


class SessionView(HomeAssistantView):
    """Issue an HttpOnly cookie using an already authenticated HA session."""

    url = BASE + "/session"
    name = "api:chatgpt_native:session"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Store the HA instance."""
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Bind the existing Ingress session to the authenticated administrator."""
        data = await request.json()
        session = data.get("session", "")
        user = await require_admin(self.hass, session)
        if user.id != request["hass_user"].id:
            raise web.HTTPForbidden
        response = web.json_response({"ready": True})
        response.set_cookie(
            COOKIE,
            session,
            path=BASE,
            httponly=True,
            secure=request.secure,
            samesite="Strict",
            max_age=300,
        )
        return response


class ClipboardView(HomeAssistantView):
    """Text clipboard commands require a currently active HA administrator."""

    url = BASE + "/clipboard"
    name = "api:chatgpt_native:clipboard"
    requires_auth = True

    def __init__(self, hass, backend):
        self.client = async_get_clientsession(hass)
        self.backend = backend.rstrip("/")

    @staticmethod
    def authorize(request):
        user = request.get("hass_user")
        if user is None:
            raise web.HTTPUnauthorized()
        if not user.is_active or not user.is_admin:
            raise web.HTTPForbidden()

    async def _proxy(self, method, data=None):
        try:
            async with self.client.request(
                method,
                self.backend + "/clipboard",
                json=data,
                timeout=ClientTimeout(total=12),
                allow_redirects=False,
            ) as response:
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > 400000:
                        raise web.HTTPBadGateway()
                return web.Response(
                    body=body,
                    status=response.status,
                    headers={
                        "Content-Type": "application/json",
                        "Cache-Control": "no-store",
                    },
                )
        except (OSError, TimeoutError):
            raise web.HTTPServiceUnavailable() from None

    async def get(self, request):
        self.authorize(request)
        return await self._proxy("GET")

    async def post(self, request):
        self.authorize(request)
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        body = bytearray()
        async for chunk in request.content.iter_chunked(8192):
            body.extend(chunk)
            if len(body) > 400000:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=400000, actual_size=len(body)
                )
        import json

        try:
            payload = json.loads(body)
            text = payload.get("text") if isinstance(payload, dict) else None
            if (
                not isinstance(text, str)
                or "\0" in text
                or len(text.encode("utf-8")) > 65536
            ):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest() from None
        return await self._proxy("POST", {"text": text})


class GUIView(HomeAssistantView):
    """Proxy GUI assets and VNC WebSocket for current administrators."""

    url = BASE + "/{path:.*}"
    name = "api:chatgpt_native:gui"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, backend: str = DEFAULT_BACKEND) -> None:
        """Store the HA instance."""
        self.hass = hass
        self.client = async_get_clientsession(hass)
        self.backend = backend.rstrip("/")

    async def get(self, request: web.Request, path: str) -> web.StreamResponse:
        """Authorize before serving any asset or upgrading a WebSocket."""
        session = request.cookies.get(COOKIE, "")
        await require_admin(self.hass, session)
        # Relative assets only; the backend address is fixed, never user supplied.
        if path.startswith("/") or ".." in path.split("/"):
            raise web.HTTPBadRequest
        url = self.backend + "/" + path
        if path == "websockify":
            ws = web.WebSocketResponse(protocols=["binary"])
            async with self.client.ws_connect(url, protocols=["binary"]) as upstream:
                await ws.prepare(request)

                async def relay(source: Any, target: Any) -> None:
                    async for message in source:
                        if message.type == WSMsgType.BINARY:
                            await target.send_bytes(message.data)
                        elif message.type == WSMsgType.TEXT:
                            await target.send_str(message.data)

                async def recheck() -> None:
                    while True:
                        await asyncio.sleep(30)
                        await require_admin(self.hass, session)

                tasks = [
                    asyncio.create_task(relay(ws, upstream)),
                    asyncio.create_task(relay(upstream, ws)),
                    asyncio.create_task(recheck()),
                ]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await ws.close()
            return ws
        async with self.client.get(
            url, params=request.query, allow_redirects=False
        ) as upstream:
            return web.Response(
                body=await upstream.read(),
                status=upstream.status,
                headers={
                    "Content-Type": upstream.headers.get(
                        "Content-Type", "application/octet-stream"
                    ),
                    "Cache-Control": "no-store",
                },
            )


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Register the protected proxy and administrator sidebar panel."""
    hass.http.register_view(SessionView(hass))
    hass.http.register_view(
        ClipboardView(hass, _config["chatgpt_native"]["backend_url"])
    )
    hass.http.register_view(GUIView(hass, _config["chatgpt_native"]["backend_url"]))
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                "/chatgpt-native-panel.js",
                str(Path(__file__).with_name("panel.js")),
                cache_headers=False,
            )
        ]
    )
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="chatgpt-native-panel",
        frontend_url_path="chatgpt-native",
        sidebar_title="ChatGPT Native",
        sidebar_icon="mdi:desktop-mac",
        module_url="/chatgpt-native-panel.js",
        require_admin=True,
    )
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="ubuntu-desktop-panel",
        frontend_url_path="ubuntu-desktop",
        sidebar_title="Ubuntu Desktop",
        sidebar_icon="mdi:monitor",
        module_url="/chatgpt-native-panel.js",
        require_admin=True,
    )
    return True
