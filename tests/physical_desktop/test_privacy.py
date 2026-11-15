import importlib.util
import os
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]


def verifier():
    spec = importlib.util.spec_from_file_location(
        "physical_verify", ROOT / "scripts/physical-desktop/verify.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "save_private"), "Standalone screenshots must be private"
    return mod


def test_snapshot_is_private_even_with_permissive_umask(tmp_path):
    mod = verifier()
    target = tmp_path / "desktop.png"
    old = os.umask(0o022)
    try:
        mod.save_private(Image.new("RGB", (2, 2)), target)
    finally:
        os.umask(old)
    assert target.stat().st_mode & 0o777 == 0o600


def test_snapshot_does_not_follow_an_existing_symlink(tmp_path):
    mod = verifier()
    victim = tmp_path / "keep"
    victim.write_text("keep")
    target = tmp_path / "desktop.png"
    target.symlink_to(victim)
    mod.save_private(Image.new("RGB", (2, 2)), target)
    assert victim.read_text() == "keep"
    assert not target.is_symlink()
