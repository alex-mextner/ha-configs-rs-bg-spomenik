"""Validation must inspect the checkout being committed, not old deployed files."""
import os
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/ha_validate_yaml.sh'


def run(tmp_path, content):
    (tmp_path / 'test.yaml').write_text(content)
    return subprocess.run(['bash', str(SCRIPT), 'test.yaml'], cwd=tmp_path,
                          env={**os.environ, 'HA_YAML_PYTHON': sys.executable}, capture_output=True, text=True)


def test_validates_local_yaml_when_ha_is_on_another_host(tmp_path):
    result = run(tmp_path, 'value: !secret demo\nitems: !include other.yaml\n')
    assert result.returncode == 0, result.stdout + result.stderr


def test_rejects_invalid_worktree_content(tmp_path):
    result = run(tmp_path, 'value: [unterminated\n')
    assert result.returncode == 1 and 'ParserError' in result.stderr
