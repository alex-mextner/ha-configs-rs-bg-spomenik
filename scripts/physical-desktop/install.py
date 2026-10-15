#!/usr/bin/env python3
"""Install the private physical-desktop bridge as the existing desktop user."""

import argparse
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def command(*args, **kwargs):
    return subprocess.run(args, check=True, timeout=120, **kwargs)


def disable_legacy_service():
    """Disable the old separate session only when its unit actually exists."""
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            "chatgpt-native-ha.service",
            "-p",
            "LoadState",
            "--value",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.stdout.strip() == "not-found":
        return False
    if result.returncode or result.stdout.strip() != "loaded":
        raise RuntimeError("Could not determine the legacy desktop service state")
    command("systemctl", "--user", "disable", "--now", "chatgpt-native-ha.service")
    return True


def install(listen, peer, port):
    if os.getuid() == 0:
        raise RuntimeError("Run this installer as the logged-in desktop user, not root")
    for address in (listen, peer):
        value = ipaddress.ip_address(address)
        if value.is_unspecified or value.is_multicast:
            raise ValueError("Explicit unicast host and peer addresses are required")
    if not 1 <= port <= 65535:
        raise ValueError("Invalid bridge port")
    os.umask(0o077)
    source = Path(__file__).resolve().parent
    base = Path.home() / ".local/share/ha-physical-desktop"
    config = Path.home() / ".config/ha-physical-desktop"
    units = Path.home() / ".config/systemd/user"
    for directory in (base, config, units):
        directory.mkdir(parents=True, exist_ok=True)
    config.chmod(0o700)
    dependencies = [
        base / "runtime/usr/bin/xfreerdp",
        Path("/usr/bin/bwrap"),
        Path("/usr/libexec/gnome-remote-desktop-daemon"),
    ]
    gui = Path.home() / ".local/share/chatgpt-native-ha/gui-runtime/root"
    dependencies += [
        gui / "usr/bin/Xvfb",
        gui / "usr/bin/x11vnc",
        gui / "usr/share/novnc/vnc.html",
    ]
    for item in dependencies:
        if not item.exists():
            raise RuntimeError(f"Required desktop runtime dependency is absent: {item}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = base / "backups" / stamp
    backup.mkdir(parents=True, exist_ok=True)
    before = command(
        "gsettings",
        "list-recursively",
        "org.gnome.desktop.remote-desktop.rdp",
        capture_output=True,
        text=True,
    )
    (backup / "rdp-settings.txt").write_text(before.stdout)
    saved_files = {}
    snapshot = [(base / name, name) for name in ("session.py", "run.sh", "bridge.py")]
    snapshot.extend(
        [
            (config / "bridge.env", "bridge.env"),
            (
                units / "ha-physical-desktop-web.service",
                "ha-physical-desktop-web.service",
            ),
            (
                units / "gnome-remote-desktop.service.d/ha-private-desktop.conf",
                "ha-private-desktop.conf",
            ),
        ]
    )
    for path, name in snapshot:
        saved_files[str(path)] = name if path.exists() else None
        if path.exists():
            shutil.copy2(path, backup / name)
    (backup / "files.json").write_text(json.dumps(saved_files, indent=2))
    # Stop the old daemon before changing RDP settings; daemon-reload alone
    # does not move a running process into the private network namespace.
    command("systemctl", "--user", "stop", "gnome-remote-desktop.service")
    for name in ("session.py", "run.sh"):
        shutil.copy2(source / name, base / name)
    shutil.copy2(source.parent / "chatgpt-native/bridge.py", base / "bridge.py")
    (base / "run.sh").chmod(0o700)
    credentials = config / "credentials.json"
    if not credentials.exists():
        credentials.write_text(
            json.dumps(
                {
                    "username": "ha-physical-desktop",
                    "password": secrets.token_urlsafe(36),
                }
            )
        )
    credentials.chmod(0o600)
    cert, key = config / "tls.crt", config / "tls.key"
    if not cert.exists() or not key.exists():
        command(
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "3650",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=ha-physical-desktop.local",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    key.chmod(0o600)
    dropins = units / "gnome-remote-desktop.service.d"
    dropins.mkdir(parents=True, exist_ok=True)
    dropin = dropins / "ha-private-desktop.conf"
    if dropin.exists():
        shutil.copy2(dropin, backup / dropin.name)
    shutil.copy2(source / "gnome-service.conf", dropin)
    shutil.copy2(
        source / "ha-physical-desktop-web.service",
        units / "ha-physical-desktop-web.service",
    )
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    (config / "bridge.env").write_text(
        f"CHATGPT_NATIVE_LISTEN_HOST={listen}\nCHATGPT_NATIVE_LISTEN_PORT={port}\nCHATGPT_NATIVE_ALLOWED_PEERS={peer}\nCHATGPT_NATIVE_VNC_SOCKET={runtime}/ha-physical-desktop/vnc.sock\n"
    )
    (config / "bridge.env").chmod(0o600)
    command("systemctl", "--user", "daemon-reload")
    command("grdctl", "rdp", "set-tls-cert", str(cert))
    command("grdctl", "rdp", "set-tls-key", str(key))
    data = json.loads(credentials.read_text())
    command(
        "grdctl",
        "rdp",
        "set-credentials",
        data["username"],
        input=data["password"] + "\n",
        text=True,
        stdout=subprocess.DEVNULL,
    )
    command(
        "gsettings",
        "set",
        "org.gnome.desktop.remote-desktop.rdp",
        "screen-share-mode",
        "mirror-primary",
    )
    command("grdctl", "rdp", "set-port", "3390")
    command("grdctl", "rdp", "disable-port-negotiation")
    command("grdctl", "rdp", "disable-view-only")
    command("grdctl", "rdp", "enable")
    command(
        "systemctl",
        "--user",
        "enable",
        "gnome-remote-desktop.service",
        "ha-physical-desktop-web.service",
    )
    command("systemctl", "--user", "restart", "gnome-remote-desktop.service")
    if port == 6081:
        disable_legacy_service()
    command("systemctl", "--user", "restart", "ha-physical-desktop-web.service")
    print(f"Physical desktop services installed. Previous configuration: {backup}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", required=True)
    parser.add_argument("--peer", required=True)
    parser.add_argument("--port", type=int, default=6081)
    args = parser.parse_args()
    install(args.listen, args.peer, args.port)
