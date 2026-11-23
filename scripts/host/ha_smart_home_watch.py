#!/usr/bin/env python3
# Copyright (c) 2026 Alex Mextner
"""Linux file events for the existing MA maintenance lock; no periodic checks."""

from __future__ import annotations

import asyncio
import ctypes
import os
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

LOCK_EVENTS = 0x100 | 0x200 | 0x40 | 0x80
EVENT_HEADER = struct.Struct("iIII")


async def watch_marker(
    path: Path, callback: Callable[[str], Awaitable[None]], lease_seconds: float = 900
) -> None:
    """Wake on lock creation/removal and one bounded stale-lock deadline."""
    library = ctypes.CDLL(None, use_errno=True)
    library.inotify_init1.argtypes = [ctypes.c_int]
    library.inotify_add_watch.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    descriptor = library.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "inotify_init")
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue = asyncio.Queue()
    expiry = None

    def read() -> None:
        data = os.read(descriptor, 65536)
        offset = 0
        while offset + EVENT_HEADER.size <= len(data):
            _, _, _, size = EVENT_HEADER.unpack_from(data, offset)
            offset += EVENT_HEADER.size
            name = data[offset : offset + size].split(b"\0", 1)[0]
            offset += size
            if name == os.fsencode(path.name):
                inbox.put_nowait("changed")

    try:
        if (
            library.inotify_add_watch(descriptor, os.fsencode(path.parent), LOCK_EVENTS)
            < 0
        ):
            raise OSError(ctypes.get_errno(), "inotify_watch")
        loop.add_reader(descriptor, read)
        inbox.put_nowait("changed")
        while True:
            event = await inbox.get()
            await callback(event)
            if event == "changed":
                if expiry:
                    expiry.cancel()
                expiry = (
                    loop.call_later(lease_seconds, inbox.put_nowait, "expired")
                    if await asyncio.to_thread(path.exists)
                    else None
                )
    finally:
        if expiry:
            expiry.cancel()
        loop.remove_reader(descriptor)
        os.close(descriptor)
