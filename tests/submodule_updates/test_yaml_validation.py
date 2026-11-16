"""Validation must inspect the checkout being committed, not old deployed files."""

# This path-based pytest fixture is intentionally outside an importable package.
# ruff: noqa: INP001

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ha_validate_yaml.sh"


def run(tmp_path: Path, content: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "test.yaml").write_text(content)
    # Fixed argv invokes the tracked validator; YAML content is only file data.
    return subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT), "test.yaml"],  # noqa: S607 - preserve caller Bash lookup
        cwd=tmp_path,
        env={**os.environ, "HA_YAML_PYTHON": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )


def test_validates_local_yaml_when_ha_is_on_another_host(tmp_path: Path) -> None:
    result = run(tmp_path, "value: !secret demo\nitems: !include other.yaml\n")
    # Pytest assertions are the fixture's executable expectations.
    assert result.returncode == 0, result.stdout + result.stderr  # noqa: S101


def test_rejects_invalid_worktree_content(tmp_path: Path) -> None:
    result = run(tmp_path, "value: [unterminated\n")
    assert result.returncode == 1  # noqa: S101 - pytest expectation
    assert "ParserError" in result.stderr  # noqa: S101 - pytest expectation
