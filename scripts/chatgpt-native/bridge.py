"""Private noVNC bridge. HA performs user authentication before proxying here."""

import asyncio
import os
from pathlib import Path

from aiohttp import WSMsgType, web

ROOT = Path.home() / ".local/share/chatgpt-native-ha/gui-runtime/root/usr/share/novnc"


def create_app(
    root: Path,
    allowed_peers: set[str],
    vnc_port: int = 5901,
    *,
    vnc_socket: str | None = None,
) -> web.Application:
    """Serve only explicitly allowed source addresses, never forwarded headers."""
    if not allowed_peers or not 1 <= vnc_port <= 65535:
        raise ValueError("An explicit peer allowlist and valid VNC port are required")

    @web.middleware
    async def private_peer(request, handler):
        if request.remote not in allowed_peers:
            raise web.HTTPForbidden()
        return await handler(request)

    async def connect():
        if vnc_socket:
            return await asyncio.open_unix_connection(vnc_socket)
        return await asyncio.open_connection("127.0.0.1", vnc_port)

    async def health(request):
        try:
            reader, writer = await asyncio.wait_for(connect(), 2)
            try:
                banner = await asyncio.wait_for(reader.readexactly(12), 2)
                return web.json_response(
                    {"ready": banner.startswith(b"RFB ")},
                    status=200 if banner.startswith(b"RFB ") else 503,
                )
            finally:
                writer.close()
                await writer.wait_closed()
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            return web.json_response({"ready": False}, status=503)

    async def websocket(request):
        try:
            reader, writer = await asyncio.wait_for(connect(), 3)
        except (OSError, TimeoutError) as exc:
            raise web.HTTPServiceUnavailable(text="Desktop VNC is not running") from exc
        ws = web.WebSocketResponse(protocols=["binary"], heartbeat=30)
        tasks = []
        try:
            await ws.prepare(request)

            async def incoming():
                async for message in ws:
                    if message.type == WSMsgType.BINARY:
                        writer.write(message.data)
                        await writer.drain()

            async def outgoing():
                while data := await reader.read(65536):
                    await ws.send_bytes(data)

            tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            await ws.close()
        return ws

    app = web.Application(middlewares=[private_peer])
    app.router.add_get("/health", health)
    app.router.add_get("/websockify", websocket)
    app.router.add_static("/", root)
    return app


if __name__ == "__main__":
    peers = {
        p.strip()
        for p in os.environ.get("CHATGPT_NATIVE_ALLOWED_PEERS", "127.0.0.1").split(",")
        if p.strip()
    }
    web.run_app(
        create_app(
            Path(os.environ.get("CHATGPT_NATIVE_NOVNC_ROOT", str(ROOT))),
            peers,
            int(os.environ.get("CHATGPT_NATIVE_VNC_PORT", "5901")),
            vnc_socket=os.environ.get("CHATGPT_NATIVE_VNC_SOCKET"),
        ),
        host=os.environ.get("CHATGPT_NATIVE_LISTEN_HOST", "127.0.0.1"),
        port=int(os.environ.get("CHATGPT_NATIVE_LISTEN_PORT", "6081")),
        print=None,
    )
