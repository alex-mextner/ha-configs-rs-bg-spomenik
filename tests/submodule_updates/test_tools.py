"""Provision the required secret scanner for owner-UID commits inside HA."""

import importlib.util
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ha_setup_integrations.py"


def module():
    import sys

    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("setup_tools_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_setup_copies_verified_scanner_without_touching_source(tmp_path):
    source = tmp_path / "host-gitleaks"
    source.write_text("#!/bin/sh\necho 8.30.1\n")
    source.chmod(0o755)
    root = tmp_path / "config"
    root.mkdir()
    mod = module()
    assert hasattr(mod, "install_secret_scanner"), (
        "HA commit hooks require a scanner in the config mount"
    )
    target = mod.install_secret_scanner(root, source)
    assert target == root / ".tools/bin/gitleaks"
    assert target.read_bytes() == source.read_bytes()
    assert (
        subprocess.check_output([str(target), "version"], text=True).strip() == "8.30.1"
    )
    mod.install_secret_scanner(root, source)
    assert source.exists()


def test_setup_refuses_broken_scanner(tmp_path):
    import pytest

    source = tmp_path / "host-gitleaks"
    source.write_text("#!/bin/sh\nexit 1\n")
    source.chmod(0o755)
    root = tmp_path / "config"
    root.mkdir()
    mod = module()
    assert hasattr(mod, "install_secret_scanner")
    with pytest.raises(RuntimeError, match="gitleaks"):
        mod.install_secret_scanner(root, source)
    assert not (root / ".tools/bin/gitleaks").exists()


def test_git_commit_discovers_provisioned_scanner(tmp_path, monkeypatch):
    mod = module()
    root = tmp_path / "repo"
    root.mkdir()
    source = tmp_path / "gitleaks"
    source.write_text("#!/bin/sh\necho provisioned-scanner\n")
    source.chmod(0o755)
    mod.install_secret_scanner(root, source)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    mod.git(root, "init", "-b", "main")
    (root / "file").write_text("configuration")
    mod.git(root, "add", "file")
    hook = root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nset -e\ngitleaks version > scanner-proof\n")
    hook.chmod(0o755)
    mod.git(root, "commit", "-m", "verified commit")
    assert (root / "scanner-proof").read_text().strip() == "provisioned-scanner"
