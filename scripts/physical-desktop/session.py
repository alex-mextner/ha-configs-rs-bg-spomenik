#!/usr/bin/env python3
"""Mirror the active GNOME desktop into a private noVNC transport surface.

No desktop environment or user applications are launched here. Xvfb is only the
framebuffer for FreeRDP, which views the existing GNOME desktop via Desktop Sharing.
RDP and VNC TCP listeners exist only inside an isolated network namespace.
"""

import asyncio
import hashlib
import json
import os
import secrets
import signal
import ssl
import subprocess
from pathlib import Path

BASE = Path.home() / ".local/share/ha-physical-desktop"
STATE = (
    Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    / "ha-physical-desktop"
)
GUI = Path.home() / ".local/share/chatgpt-native-ha/gui-runtime/root"
CONFIG = Path.home() / ".config/ha-physical-desktop"
DISPLAY = ":110"


def rdp_arguments(credentials, certificate, width=1920, height=1080):
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest()
    return [
        "/v:127.0.0.1:3390",
        "/u:" + credentials["username"],
        "/p:" + credentials["password"],
        "/cert:fingerprint:sha256:" + fingerprint,
        f"/size:{width}x{height}",
        "/f",
        "-decorations",
        "/network:lan",
        "/gfx",
        "/bpp:32",
        "-clipboard",
        "/log-level:WARN",
        "/title:Ubuntu physical desktop",
    ]


def next_retry_delay(current, lifetime):
    """Back off failed reconnects, but recover quickly after a stable session."""
    return 5 if lifetime >= 60 else min(current * 2, 30)


async def launch_child(name, *args, env=None, stdin=None):
    """Inherit capped systemd journal output; never append logs in runtime RAM."""
    print(f"Starting physical desktop child: {name}", flush=True)
    return await asyncio.create_subprocess_exec(
        *args, env=env, stdin=stdin, stderr=asyncio.subprocess.STDOUT
    )


async def wait_tcp(port, process, timeout=20):
    async with asyncio.timeout(timeout):
        while process.returncode is None:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.2)
    raise RuntimeError(f"Desktop transport exited before port {port} was ready")


async def relay_vnc(reader, writer):
    tasks = []
    upstream_writer = None
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            "127.0.0.1", 5902
        )

        async def copy(source, destination):
            while data := await source.read(65536):
                destination.write(data)
                await destination.drain()

        tasks = [
            asyncio.create_task(copy(reader, upstream_writer)),
            asyncio.create_task(copy(upstream_reader, writer)),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (OSError, ConnectionError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for target in (writer, upstream_writer):
            if target is not None:
                target.close()
                try:
                    await target.wait_closed()
                except OSError:
                    pass


async def serve():
    parent_netns = os.environ.get("HA_DESKTOP_PARENT_NETNS")
    if not parent_netns or parent_netns == os.readlink("/proc/self/ns/net"):
        raise RuntimeError(
            "Physical desktop transport requires a private network namespace"
        )
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    credentials = json.loads((CONFIG / "credentials.json").read_text())
    auth = STATE / "Xauthority"
    subprocess.run(
        ["/usr/bin/xauth", "-f", str(auth), "add", DISPLAY, ".", secrets.token_hex(16)],
        check=True,
        capture_output=True,
    )
    transport_env = {
        **os.environ,
        "DISPLAY": DISPLAY,
        "XAUTHORITY": str(auth),
        "XDG_SESSION_TYPE": "x11",
    }
    transport_env.pop("WAYLAND_DISPLAY", None)
    gui_env = {
        **transport_env,
        "LD_LIBRARY_PATH": str(GUI / "usr/lib/x86_64-linux-gnu"),
    }
    children = []
    tasks = []
    server = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def launch(name, *args, env=None, stdin=None):
        process = await launch_child(name, *args, env=env, stdin=stdin)
        children.append(process)
        return process

    async def reconnect():
        args = rdp_arguments(credentials, (CONFIG / "tls.crt").read_text())
        retry = 5
        while not stop.is_set():
            started = loop.time()
            process = await launch(
                "rdp",
                str(BASE / "runtime/usr/bin/xfreerdp"),
                "/args-from:stdin",
                env=transport_env,
                stdin=asyncio.subprocess.PIPE,
            )
            process.stdin.write(("\n".join(args) + "\n").encode())
            await process.stdin.drain()
            process.stdin.close()
            code = await process.wait()
            children.remove(process)
            # A disconnected/locked desktop leaves a blank transport, never a
            # second desktop session and never a screenshot of the last frame.
            await asyncio.to_thread(
                subprocess.run,
                ["/usr/bin/xsetroot", "-solid", "black"],
                env=transport_env,
                capture_output=True,
                timeout=3,
            )
            print(
                f"Physical GNOME disconnected ({code}); retry in {retry}s",
                flush=True,
            )
            try:
                await asyncio.wait_for(stop.wait(), retry)
            except TimeoutError:
                pass
            retry = next_retry_delay(retry, loop.time() - started)

    try:
        grd = await launch(
            "gnome",
            "/usr/libexec/gnome-remote-desktop-daemon",
            "--rdp-port=3390",
            "--max-parallel-connections=1",
        )
        xvfb = await launch(
            "framebuffer",
            str(GUI / "usr/bin/Xvfb"),
            DISPLAY,
            "-screen",
            "0",
            "1920x1080x24",
            "-nolisten",
            "tcp",
            "-auth",
            str(auth),
            env=gui_env,
        )
        async with asyncio.timeout(10):
            while not Path("/tmp/.X11-unix/X110").exists():
                if xvfb.returncode is not None:
                    raise RuntimeError("Transport framebuffer failed to start")
                await asyncio.sleep(0.1)
        vnc = await launch(
            "vnc",
            str(GUI / "usr/bin/x11vnc"),
            "-display",
            DISPLAY,
            "-auth",
            str(auth),
            "-localhost",
            "-rfbport",
            "5902",
            "-forever",
            "-shared",
            "-nopw",
            "-quiet",
            env=gui_env,
        )
        await wait_tcp(5902, vnc)
        await wait_tcp(3390, grd)
        socket_path = STATE / "vnc.sock"
        socket_path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(relay_vnc, path=socket_path)
        socket_path.chmod(0o600)
        tasks = [asyncio.create_task(reconnect()), asyncio.create_task(stop.wait())]
        tasks.extend(asyncio.create_task(child.wait()) for child in (grd, xvfb, vnc))
        print(
            "Physical desktop transport ready; RDP and VNC are network-isolated",
            flush=True,
        )
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if not stop.is_set():
            raise RuntimeError("Physical desktop transport child stopped")
    finally:
        stop.set()
        if server:
            server.close()
            await server.wait_closed()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for child in children:
            if child.returncode is None:
                child.terminate()
        for child in children:
            try:
                await asyncio.wait_for(child.wait(), 5)
            except TimeoutError:
                child.kill()
                await child.wait()
        (STATE / "vnc.sock").unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(serve())
