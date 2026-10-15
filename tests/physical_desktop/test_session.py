import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module():
    spec = importlib.util.spec_from_file_location(
        "physical_session", ROOT / "scripts/physical-desktop/session.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_refuses_to_start_rdp_in_host_network(monkeypatch):
    monkeypatch.delenv("HA_DESKTOP_PARENT_NETNS", raising=False)
    with pytest.raises(RuntimeError, match="private network namespace"):
        await module().serve()


@pytest.mark.asyncio
async def test_refuses_a_false_namespace_claim(monkeypatch):
    import os

    monkeypatch.setenv("HA_DESKTOP_PARENT_NETNS", os.readlink("/proc/self/ns/net"))
    with pytest.raises(RuntimeError, match="private network namespace"):
        await module().serve()


def test_rdp_pins_certificate_and_avoids_file_clipboard(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=desktop-test",
        ],
        check=True,
        capture_output=True,
    )
    args = module().rdp_arguments(
        {"username": "test", "password": "a-secret"}, cert.read_text()
    )
    assert "/v:127.0.0.1:3390" in args
    assert "-clipboard" in args
    assert any(a.startswith("/cert:fingerprint:sha256:") for a in args)
    assert "/cert:ignore" not in args


def test_reconnect_delay_is_capped_and_resets_after_stable_session():
    mod = module()
    assert hasattr(mod, "next_retry_delay")
    delay = 5
    for _ in range(30):
        delay = mod.next_retry_delay(delay, 1)
    assert delay == 30
    assert mod.next_retry_delay(delay, 120) == 5


@pytest.mark.asyncio
async def test_child_output_uses_parent_journal_not_unbounded_files(tmp_path, capfd):
    import sys

    mod = module()
    assert hasattr(mod, "launch_child")
    child = await mod.launch_child(
        "test", sys.executable, "-c", 'print("journal-proof")'
    )
    assert await child.wait() == 0
    assert "journal-proof" in capfd.readouterr().out
    assert not list(tmp_path.glob("*.log"))
