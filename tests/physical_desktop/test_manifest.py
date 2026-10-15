"""Metadata required by HA's integration validator."""

import json
from pathlib import Path


def test_native_desktop_declares_http_and_documentation():
    manifest = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "custom_components/chatgpt_native/manifest.json"
        ).read_text()
    )
    assert "http" in manifest["dependencies"]
    assert manifest["documentation"].endswith("/docs/physical-ubuntu-desktop.md")
