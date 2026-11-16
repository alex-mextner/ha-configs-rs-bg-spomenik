"""Verify the published validator and RPC helper contracts without live services."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch
from urllib.error import HTTPError

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load(filename: str) -> ModuleType:
    name = "legacy_contract_" + Path(filename).stem
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RpcContracts(unittest.TestCase):
    """Exercise real request construction with isolated response fixtures."""

    def setUp(self) -> None:
        """Load a fresh helper with its unchanged configured default."""
        self.rpc = _load("transmission_errors.py")

    def test_invalid_authority_is_rejected_before_network(self) -> None:
        """Invalid configured schemes and authorities cannot reach urlopen."""
        urls = (
            "file:///etc/hosts",
            "ftp://host/rpc",
            "http:///rpc",
            "https://host:bad/rpc",
            "http://bad host/rpc",
        )
        for url in urls:
            with (
                self.subTest(url=url),
                patch.object(self.rpc.urllib.request, "urlopen") as opened,
            ):
                self.rpc.RPC_URL = url
                with self.assertRaises(ValueError):
                    self.rpc.rpc("torrent-get")
                opened.assert_not_called()

    def test_session_negotiation_preserves_request(self) -> None:
        """A conflict retries the same POST once with its session ID."""
        conflict = HTTPError(
            self.rpc.RPC_URL,
            409,
            "Conflict",
            {"X-Transmission-Session-Id": "fixture-session"},
            None,
        )
        with patch.object(self.rpc.urllib.request, "urlopen") as opened:
            response = opened.return_value
            response.__enter__.return_value.read.return_value = b'{"result":"success"}'
            opened.side_effect = [conflict, response]
            self.assertEqual(
                self.rpc.rpc("torrent-get", {"fields": ["id"]}), {"result": "success"}
            )
            first, second = (call.args[0] for call in opened.call_args_list)
            self.assertEqual(first.get_method(), "POST")
            self.assertEqual(second.data, first.data)
            self.assertEqual(
                json.loads(first.data),
                {"method": "torrent-get", "arguments": {"fields": ["id"]}},
            )
            self.assertEqual(
                second.get_header("X-transmission-session-id"), "fixture-session"
            )
            self.assertEqual(opened.call_count, 2)

    def test_json_sensor_output_stays_compatible(self) -> None:
        """Normal torrent errors retain the existing JSON field names and units."""
        data = {
            "arguments": {
                "torrents": [
                    {
                        "id": 4,
                        "name": "fixture",
                        "error": 2,
                        "errorString": "fixture error",
                        "percentDone": 0.125,
                    }
                ]
            }
        }
        output = io.StringIO()
        with patch.object(self.rpc, "rpc", return_value=data), redirect_stdout(output):
            self.assertEqual(self.rpc.main(), 0)
        record = json.loads(output.getvalue())
        self.assertEqual(record["count"], 1)
        self.assertEqual(record["errors"], record["torrents"])
        self.assertEqual(record["errors"][0]["percent_done"], 12.5)
        self.assertEqual(record["errors"][0]["error_string"], "fixture error")

    def test_probe_error_stays_json_data(self) -> None:
        """An RPC failure remains a zero-exit sensor record."""
        output = io.StringIO()
        with (
            patch.object(self.rpc, "rpc", side_effect=OSError("fixture failure")),
            redirect_stdout(output),
        ):
            self.assertEqual(self.rpc.main(), 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "count": 0,
                "errors": [],
                "torrents": [],
                "probe_error": "OSError('fixture failure')",
            },
        )


class LovelaceContracts(unittest.TestCase):
    """Exercise actual YAML loading, traversal and CLI messages."""

    def setUp(self) -> None:
        """Give the helper an isolated configuration directory."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.validator = _load("ha_validate_lovelace.py")
        self.validator.ROOT = self.root
        self.validator.CONFIG_PATH = self.root / "configuration.yaml"

    def test_sections_errors_preserve_order(self) -> None:
        """Section shape errors precede the independent card traversal errors."""
        path = self.root / "ui-test.yaml"
        path.write_text(
            json.dumps(
                {
                    "views": [
                        {
                            "title": 1,
                            "type": "sections",
                            "cards": [None],
                            "sections": [None, {"cards": 3}],
                        }
                    ]
                }
            )
        )
        self.assertEqual(
            self.validator.validate_dashboard(path, set(), set()),
            [
                "ui-test.yaml.views[0]: title must be a string",
                "ui-test.yaml.views[0]: sections view must not also define cards",
                "ui-test.yaml.views[0].sections[0]: section must be a mapping",
                "ui-test.yaml.views[0].sections[1]: cards must be a list",
                "ui-test.yaml.views[0].cards[0]: card must be a mapping",
            ],
        )

    def test_safe_tags_remain_literal(self) -> None:
        """HA tags retain scalar and container values through SafeLoader."""
        path = self.root / "tagged.yaml"
        path.write_text("name: !secret fixture_name\nitems: !fixture [one, two]\n")
        self.assertEqual(
            self.validator.load_yaml(path),
            {"name": "fixture_name", "items": ["one", "two"]},
        )

    def test_resource_definitions_and_cli_success(self) -> None:
        """A local custom element satisfies its card and prints the same CLI result."""
        (self.root / "www").mkdir()
        (self.root / "www/card.js").write_text(
            "customElements.define('fixture-card', Class);"
        )
        config = {
            "lovelace": {
                "resource_mode": "yaml",
                "resources": [{"url": "/local/card.js", "type": "module"}],
                "dashboards": {"fixture": {"filename": "ui-test.yaml"}},
            }
        }
        self.validator.CONFIG_PATH.write_text(json.dumps(config))
        (self.root / "ui-test.yaml").write_text(
            json.dumps({"views": [{"cards": [{"type": "custom:fixture-card"}]}]})
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(self.validator.main([]), 0)
        self.assertEqual(stdout.getvalue(), "OK: Lovelace validation (ui-test.yaml)\n")
        self.assertEqual(stderr.getvalue(), "")
