#!/usr/bin/env python3
"""Verify the actual framebuffer and keyboard input through HA's admin proxy."""

import argparse
import asyncio
import json
import os
import struct
import tempfile
from pathlib import Path

import aiohttp
from PIL import Image, ImageChops


def save_private(image, path):
    """Atomically save a private screenshot without following destination symlinks."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".frame-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            image.save(handle, format="PNG")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class RFB:
    def __init__(self, ws):
        self.ws = ws
        self.buffer = bytearray()
        self.width = self.height = 0

    async def read(self, size):
        while len(self.buffer) < size:
            message = await self.ws.receive(timeout=20)
            if message.type != aiohttp.WSMsgType.BINARY:
                raise RuntimeError(f"Unexpected RFB WebSocket message: {message.type}")
            self.buffer.extend(message.data)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    async def start(self):
        assert (await self.read(12)).startswith(b"RFB ")
        await self.ws.send_bytes(b"RFB 003.008\n")
        count = (await self.read(1))[0]
        modes = await self.read(count)
        assert 1 in modes, "Expected private transport without a second login"
        await self.ws.send_bytes(b"\x01")
        assert await self.read(4) == b"\0\0\0\0"
        await self.ws.send_bytes(b"\x01")
        header = await self.read(24)
        self.width, self.height = struct.unpack(">HH", header[:4])
        name = await self.read(struct.unpack(">I", header[20:])[0])
        fmt = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        await self.ws.send_bytes(b"\0\0\0\0" + fmt)
        await self.ws.send_bytes(struct.pack(">BBHi", 2, 0, 1, 0))
        return name.decode(errors="replace")

    async def frame(self):
        await self.ws.send_bytes(
            struct.pack(">BBHHHH", 3, 0, 0, 0, self.width, self.height)
        )
        image = Image.new("RGB", (self.width, self.height))
        while True:
            kind = (await self.read(1))[0]
            if kind == 0:
                count = struct.unpack(">BH", await self.read(3))[1]
                for _ in range(count):
                    x, y, w, h, encoding = struct.unpack(">HHHHi", await self.read(12))
                    if encoding != 0:
                        raise RuntimeError(f"Unexpected RFB encoding {encoding}")
                    pixels = await self.read(w * h * 4)
                    tile = Image.frombytes("RGB", (w, h), pixels, "raw", "BGRX")
                    image.paste(tile, (x, y))
                if count:
                    return image
            elif kind == 2:
                continue
            elif kind == 3:
                size = struct.unpack(">3xI", await self.read(7))[0]
                await self.read(size)
            else:
                raise RuntimeError(f"Unexpected RFB type {kind}")

    async def key(self, keysym):
        for down in (1, 0):
            await self.ws.send_bytes(struct.pack(">BBHI", 4, down, 0, keysym))
            await asyncio.sleep(0.08)


async def run(args):
    values = {}
    for line in args.env.read_text().splitlines():
        key, sep, value = line.removeprefix("export ").partition("=")
        if sep:
            values[key.strip()] = value.strip().strip("\"'")
    token = values["HA_TOKEN"]
    url = args.url.rstrip("/")
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True)
    ) as client:
        async with client.ws_connect(url + "/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": token})
            assert (await ws.receive_json())["type"] == "auth_ok"
            await ws.send_json({"id": 1, "type": "ha-ingress/session"})
            session = (await ws.receive_json())["result"]
        async with client.post(
            url + "/api/chatgpt_native/session",
            json=session,
            headers={"Authorization": "Bearer " + token},
        ) as response:
            response.raise_for_status()
        async with client.ws_connect(
            url + "/api/chatgpt_native/websockify",
            protocols=["binary"],
            max_msg_size=16 * 1024 * 1024,
        ) as ws:
            rfb = RFB(ws)
            name = await rfb.start()
            before = await rfb.frame()
            save_private(before, args.output.with_suffix(".png"))
            result = {
                "desktop": name,
                "width": rfb.width,
                "height": rfb.height,
                "image": str(args.output.with_suffix(".png")),
            }
            if args.input_test:
                try:
                    await rfb.key(
                        0xFFEB
                    )  # Super: open GNOME overview, no typing into any application.
                    await asyncio.sleep(1)
                    after = await rfb.frame()
                    save_private(
                        after,
                        args.output.with_name(
                            args.output.name + "-overview"
                        ).with_suffix(".png"),
                    )
                    result["input_changed_screen"] = (
                        ImageChops.difference(before, after).getbbox() is not None
                    )
                    if not result["input_changed_screen"]:
                        raise RuntimeError(
                            "Super key did not change the physical desktop"
                        )
                finally:
                    await rfb.key(
                        0xFF1B
                    )  # Escape restores the previously focused window.
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--env", type=Path, default=Path.home() / ".env")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / ".cache/ha-physical-desktop/verification/frame",
    )
    parser.add_argument("--input-test", action="store_true")
    asyncio.run(run(parser.parse_args()))
