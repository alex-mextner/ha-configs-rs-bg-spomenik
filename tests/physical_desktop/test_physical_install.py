import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def installer():
    spec = importlib.util.spec_from_file_location(
        "physical_installer", ROOT / "scripts/physical-desktop/install.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "disable_legacy_service"), (
        "A fresh host must not require the legacy service"
    )
    return mod


@pytest.fixture
def systemctl(tmp_path, monkeypatch):
    stub = tmp_path / "systemctl"
    stub.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$COMMAND_LOG"\ncase "$*" in\n *show*) echo "$LOAD_STATE"; test "$LOAD_STATE" = loaded;;\n *) exit 0;;\nesac\n'
    )
    stub.chmod(0o755)
    log = tmp_path / "commands"
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + os.environ["PATH"])
    monkeypatch.setenv("COMMAND_LOG", str(log))
    return log


def test_absent_legacy_unit_does_not_abort_fresh_setup(systemctl, monkeypatch):
    monkeypatch.setenv("LOAD_STATE", "not-found")
    assert installer().disable_legacy_service() is False
    assert "disable" not in systemctl.read_text()


def test_existing_legacy_unit_is_disabled(systemctl, monkeypatch):
    monkeypatch.setenv("LOAD_STATE", "loaded")
    assert installer().disable_legacy_service() is True
    assert "disable --now chatgpt-native-ha.service" in systemctl.read_text()


def test_unexpected_systemctl_failure_is_not_ignored(systemctl, monkeypatch):
    monkeypatch.setenv("LOAD_STATE", "")
    with pytest.raises(RuntimeError):
        installer().disable_legacy_service()


@pytest.fixture
def simulated_host(tmp_path, monkeypatch):
    from types import SimpleNamespace

    mod = installer()
    monkeypatch.setattr(mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(mod.os, "getuid", lambda: 1000)
    actual_exists = mod.Path.exists
    prerequisites = {
        "/usr/bin/bwrap",
        "/usr/libexec/gnome-remote-desktop-daemon",
        "/usr/bin/xauth",
        "/usr/bin/xsetroot",
        "/usr/bin/openssl",
        "/usr/bin/python3",
    }
    monkeypatch.setattr(
        mod.Path,
        "exists",
        lambda p: True if str(p) in prerequisites else actual_exists(p),
    )
    base = tmp_path / ".local/share/ha-physical-desktop"
    config = tmp_path / ".config/ha-physical-desktop"
    units = tmp_path / ".config/systemd/user"
    gui = tmp_path / ".local/share/chatgpt-native-ha/gui-runtime/root"
    for file in (
        base / "runtime/usr/bin/xfreerdp",
        base / "runtime/usr/bin/xclip",
        gui / "usr/bin/Xvfb",
        gui / "usr/bin/x11vnc",
        gui / "usr/share/novnc/vnc.html",
        config / "tls.crt",
        config / "tls.key",
    ):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("test-runtime")
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout="old-settings", returncode=0)

    monkeypatch.setattr(mod, "command", command)
    monkeypatch.setattr(mod, "disable_legacy_service", lambda: False)
    return mod, base, config, units, calls


def test_existing_rdp_is_stopped_before_credentials_or_enable(simulated_host):
    mod, _, _, _, calls = simulated_host
    mod.install("100.74.26.95", "100.116.57.66", 6082)
    stop = ("systemctl", "--user", "stop", "gnome-remote-desktop.service")
    assert stop in calls
    assert calls.index(stop) < calls.index(("grdctl", "rdp", "enable"))
    assert calls.index(stop) < next(
        i for i, c in enumerate(calls) if c[:3] == ("grdctl", "rdp", "set-tls-cert")
    )


def test_rerun_preserves_network_and_service_settings_in_backup(simulated_host):
    import json

    mod, base, config, units, _ = simulated_host
    units.mkdir(parents=True, exist_ok=True)
    old_env = "CHATGPT_NATIVE_LISTEN_PORT=6082\n"
    old_unit = "previous-web-unit\n"
    (config / "bridge.env").write_text(old_env)
    (units / "ha-physical-desktop-web.service").write_text(old_unit)
    mod.install("100.74.26.95", "100.116.57.66", 6081)
    backup = next((base / "backups").iterdir())
    assert (backup / "bridge.env").read_text() == old_env
    assert (backup / "ha-physical-desktop-web.service").read_text() == old_unit
    manifest = json.loads((backup / "files.json").read_text())
    assert manifest[str(config / "bridge.env")] == "bridge.env"
    assert manifest[str(base / "session.py")] is None


def test_root_installation_is_rejected_before_changes(monkeypatch):
    mod = installer()
    monkeypatch.setattr(mod.os, "getuid", lambda: 0)
    with pytest.raises(RuntimeError, match="not root"):
        mod.install("100.74.26.95", "100.116.57.66", 6081)


def test_missing_runtime_is_rejected_before_service_changes(tmp_path, monkeypatch):
    mod = installer()
    calls = []
    monkeypatch.setattr(mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(mod.os, "getuid", lambda: 1000)
    monkeypatch.setattr(mod, "command", lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError, match="dependency is absent"):
        mod.install("100.74.26.95", "100.116.57.66", 6081)
    assert calls == []


@pytest.mark.parametrize("missing", ["/usr/bin/xauth", "/usr/bin/xsetroot"])
def test_missing_capture_utility_is_rejected_before_service_changes(
    simulated_host, monkeypatch, missing
):
    mod, _, _, _, calls = simulated_host
    exists = mod.Path.exists
    monkeypatch.setattr(
        mod.Path, "exists", lambda p: False if str(p) == missing else exists(p)
    )
    with pytest.raises(RuntimeError, match="dependency is absent"):
        mod.install("100.74.26.95", "100.116.57.66", 6081)
    assert calls == []


def test_previous_keyring_credentials_are_preserved_privately(simulated_host):
    mod, base, _, _, calls = simulated_host
    mod.install("100.74.26.95", "100.116.57.66", 6082)
    backup = next((base / "backups").iterdir()) / "rdp-credentials.txt"
    assert backup.read_text() == "old-settings"
    assert backup.stat().st_mode & 0o777 == 0o600
    assert calls.index(("grdctl", "status", "--show-credentials")) < next(
        i for i, c in enumerate(calls) if c[:3] == ("grdctl", "rdp", "set-credentials")
    )
