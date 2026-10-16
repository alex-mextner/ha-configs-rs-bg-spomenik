#!/usr/bin/env python3
"""Install pinned GNOME locked-desktop support without disabling the password lock.

A known unchanged extension can be enabled without logging out.
A newly staged UUID is enabled for the next graphical login; no logout is forced.
"""

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def main():
    if os.getuid() == 0:
        raise RuntimeError("Run as the existing desktop user")
    os.umask(0o077)
    source = (
        Path(__file__).resolve().parents[2] / "submodules/allow-locked-remote-desktop"
    )
    metadata = json.loads((source / "metadata.json").read_text())
    uuid = metadata["uuid"]
    shell_version = (
        subprocess.check_output(["gnome-shell", "--version"], text=True)
        .strip()
        .split()[-1]
        .split(".")[0]
    )
    if shell_version not in metadata["shell-version"]:
        raise RuntimeError("The pinned extension does not support this GNOME version")
    target = Path.home() / ".local/share/gnome-shell/extensions" / uuid
    backup = (
        Path.home()
        / ".local/share/ha-physical-desktop/backups"
        / ("lock-extension-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    )
    backup.mkdir(parents=True, mode=0o700)
    if target.exists():
        shutil.copytree(target, backup / "previous-extension")
    settings = subprocess.check_output(
        ["gsettings", "get", "org.gnome.shell", "enabled-extensions"], text=True
    )
    (backup / "enabled-extensions.txt").write_text(settings)
    known = (
        subprocess.run(
            ["gnome-extensions", "info", uuid], capture_output=True
        ).returncode
        == 0
    )
    unchanged = (target / "extension.js").is_file() and (
        target / "extension.js"
    ).read_bytes() == (source / "extension.js").read_bytes()
    target.mkdir(parents=True, exist_ok=True)
    for name in ("extension.js", "metadata.json", "COPYING", "LICENSE"):
        if (source / name).is_file():
            shutil.copy2(source / name, target / name)
    # GI is already part of GNOME. Avoid shell-evaluating the GVariant list.
    import gi
    from gi.repository import Gio

    config = Gio.Settings.new("org.gnome.shell")
    enabled = list(config.get_strv("enabled-extensions"))
    if uuid not in enabled:
        config.set_strv("enabled-extensions", enabled + [uuid])
        Gio.Settings.sync()
    if known and unchanged:
        subprocess.run(["gnome-extensions", "enable", uuid], check=True, timeout=15)
        info = subprocess.check_output(
            ["gnome-extensions", "info", uuid],
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        active = "State: ACTIVE" in info
    else:
        active = False
    print(
        json.dumps(
            {
                "extension": uuid,
                "active_now": active,
                "next_login_required": not active,
                "backup": str(backup),
            }
        )
    )


if __name__ == "__main__":
    main()
