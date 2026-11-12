"""The retained upstream extension keeps the native lock/password dialog intact."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_upstream_supports_current_shell_and_lock_mode():
    source = ROOT / "submodules/allow-locked-remote-desktop"
    metadata = json.loads((source / "metadata.json").read_text())
    assert "50" in metadata["shell-version"]
    assert "unlock-dialog" in metadata["session-modes"]
    code = (source / "extension.js").read_text()
    assert "inhibit_remote_access" in code
    assert "screenShield.deactivate" not in code
    assert "unlock-session" not in code


def test_installer_preserves_lock_and_existing_extensions():
    path = ROOT / "scripts/physical-desktop/install-lock-screen.py"
    assert path.is_file(), "Locked access needs a reproducible pinned-source installer"
    code = path.read_text()
    assert "enabled-extensions" in code
    assert "lock-enabled" not in code
    assert "unlock-session" not in code
    assert "submodules/allow-locked-remote-desktop" in code
