"""Text-only access to the real Xwayland clipboard, without RDP's FUSE channel.

No clipboard contents or passwords are written to logs, files, or process arguments.
The desktop lock is checked before and after reads; locked access fails closed.
"""

import asyncio
import os
from pathlib import Path

MAX_TEXT_BYTES = 65536


class DesktopLocked(PermissionError):
    """The existing desktop must be unlocked before clipboard access."""


def validate_text(text):
    if not isinstance(text, str) or "\0" in text:
        raise ValueError("Clipboard accepts plain text only")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("Clipboard text exceeds 64 KiB")
    return text


async def read_bounded(stream, limit):
    chunks = bytearray()
    while data := await stream.read(min(8192, limit + 1 - len(chunks))):
        chunks.extend(data)
        if len(chunks) > limit:
            raise ValueError("Clipboard response exceeds size limit")
    return bytes(chunks)


async def capture(*args, limit=MAX_TEXT_BYTES):
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(4):
            data = await read_bounded(proc.stdout, limit)
            if len(data) > limit:
                raise ValueError("Clipboard response exceeds size limit")
            if await proc.wait():
                raise RuntimeError("Desktop clipboard operation unavailable")
            return data
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


class DesktopClipboard:
    def __init__(self, executable: Path):
        self.executable = executable
        self._owner = None
        self._mutex = asyncio.Lock()

    async def locked(self):
        status = await capture(
            "/usr/bin/gdbus",
            "call",
            "--session",
            "--dest",
            "org.gnome.ScreenSaver",
            "--object-path",
            "/org/gnome/ScreenSaver",
            "--method",
            "org.gnome.ScreenSaver.GetActive",
            limit=256,
        )
        if status.strip() not in (b"(true,)", b"(false,)"):
            raise RuntimeError("Cannot determine desktop lock state")
        return status.strip() == b"(true,)"

    async def _environment(self):
        raw = await capture(
            "/usr/bin/systemctl", "--user", "show-environment", limit=262144
        )
        env = dict(os.environ)
        for line in raw.decode().splitlines():
            key, sep, value = line.partition("=")
            if sep and key in ("DISPLAY", "XAUTHORITY"):
                env[key] = value
        if not env.get("DISPLAY") or not env.get("XAUTHORITY"):
            raise RuntimeError("Real desktop display is not ready")
        return env

    async def _read(self):
        env = await self._environment()
        proc = await asyncio.create_subprocess_exec(
            str(self.executable),
            "-selection",
            "clipboard",
            "-out",
            "-target",
            "UTF8_STRING",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(4):
                data = await read_bounded(proc.stdout, MAX_TEXT_BYTES)
                if len(data) > MAX_TEXT_BYTES:
                    raise ValueError("Clipboard text exceeds 64 KiB")
                if await proc.wait():
                    raise RuntimeError("Clipboard does not contain readable text")
                return validate_text(data.decode("utf-8"))
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def _write(self, text):
        env = await self._environment()
        proc = await asyncio.create_subprocess_exec(
            str(self.executable),
            "-selection",
            "clipboard",
            "-in",
            "-quiet",
            "-target",
            "UTF8_STRING",
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            proc.stdin.write(text.encode("utf-8"))
            await asyncio.wait_for(proc.stdin.drain(), 3)
            proc.stdin.close()
            await proc.stdin.wait_closed()
            # xclip owns the selection until it is replaced. It must stay alive.
            for _ in range(10):
                await asyncio.sleep(0.03)
                if proc.returncode is not None:
                    raise RuntimeError("Clipboard writer failed")
                if await self._read() == text:
                    break
            else:
                raise RuntimeError("Clipboard ownership was not confirmed")
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        previous, self._owner = self._owner, proc
        if previous is not None and previous.returncode is None:
            previous.terminate()
            await previous.wait()

    async def get_text(self):
        async with self._mutex:
            if await self.locked():
                raise DesktopLocked()
            text = await self._read()
            if await self.locked():
                raise DesktopLocked()
            return text

    async def set_text(self, text):
        validate_text(text)
        async with self._mutex:
            if await self.locked():
                raise DesktopLocked()
            await self._write(text)
            if await self.locked():
                raise DesktopLocked()

    async def close(self):
        if self._owner is not None and self._owner.returncode is None:
            self._owner.terminate()
            await self._owner.wait()
