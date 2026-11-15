"""Verify HA package dependencies against actual registered host routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[2]


class SecretLoader(yaml.SafeLoader):
    """Keep secret names without loading household credentials."""


SecretLoader.add_constructor("!secret", lambda _loader, node: {"secret": node.value})


def load_package(name) -> dict[str, Any]:
    # SecretLoader inherits SafeLoader and only preserves secret tag names.
    return yaml.load((ROOT / "packages" / name).read_text(), Loader=SecretLoader)  # noqa: S506


def actions(value) -> Iterator[str]:
    if isinstance(value, dict):
        action = value.get("action", value.get("service"))
        if isinstance(action, str):
            yield action
        for nested in value.values():
            yield from actions(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from actions(nested)


class RemotePrerequisiteContracts(unittest.TestCase):
    """Verify published HA dependencies against the actual host route registry."""

    def test_configured_feedback_and_keyboard_routes_are_registered(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "route_host", ROOT / "scripts/host/ha-host-actions.py"
        )
        host = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(ROOT / "scripts/host"), *sys.path]):
            spec.loader.exec_module(host)
        resource = load_package("media_power_pairs.yaml")["rest"][0]
        self.assertIs(
            host.ROUTES[("GET", urlsplit(resource["resource"]).path)], host.kodi_status
        )
        self.assertEqual(
            resource["headers"]["X-HA-Host-Token"], {"secret": "host_actions_token"}
        )
        command = load_package("host_actions.yaml")["rest_command"][
            "host_kodi_text_input"
        ]
        self.assertEqual(command["url"], {"secret": "host_kodi_text_input_url"})
        self.assertEqual(command["method"], "POST")
        for path, fn in (
            ("/kodi/text-input", host.kodi_text_input),
            ("/kodi/keyboard", host.kodi_keyboard),
        ):
            self.assertIn(("POST", path), host.BODY_ROUTES)
            self.assertIs(host.ROUTES[("POST", path)], fn)

    def test_remote_packages_publish_required_script_dependencies(self) -> None:
        docs = [
            load_package(name)
            for name in ("universal_remote.yaml", "projector.yaml", "music_remote.yaml")
        ]
        definitions = {name for doc in docs for name in doc.get("script", {})}
        references = {
            action.removeprefix("script.")
            for doc in docs
            for action in actions(doc)
            if action.startswith(
                ("script.music_remote_", "script.projector_", "script.audio_mode_")
            )
        }
        self.assertTrue(references)
        self.assertEqual(references - definitions, set())
