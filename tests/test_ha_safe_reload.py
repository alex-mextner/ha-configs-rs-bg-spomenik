"""Regression coverage: invalid/unverified configuration must never restart HA."""
import contextlib
import http.client
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ha_safe_reload.py"
SPEC = importlib.util.spec_from_file_location("ha_safe_reload", SCRIPT)
reload_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reload_script)


class SafeReloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "packages").mkdir()
        (self.root / "configuration.yaml").write_text("homeassistant: {}\n")
        (self.root / "packages" / "valid.yaml").write_text(
            "test: !secret private_value\ninclude: !include another.yaml\n"
        )
        (self.root / ".env").write_text("HA_TOKEN='test-token'\n")
        self.connection = MagicMock()
        self.connection.getresponse.return_value.status = 200
        self.http = patch.object(reload_script.http.client, "HTTPConnection",
                                 return_value=self.connection).start()
        self.docker = patch.object(reload_script.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0)).start()
        self.addCleanup(patch.stopall)
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def test_invalid_yaml_never_calls_api_or_restart(self):
        (self.root / "packages" / "broken.yaml").write_text("value: [\n")
        with self.assertRaisesRegex(reload_script.ValidationError, "YAML syntax invalid"):
            reload_script.run(self.root)
        self.http.assert_not_called()
        self.docker.assert_not_called()

    def test_api_error_never_restarts(self):
        self.connection.getresponse.return_value.status = 500
        with self.assertRaisesRegex(reload_script.ValidationError, "HTTP 500"):
            reload_script.run(self.root)
        self.docker.assert_not_called()

    def test_api_timeout_never_restarts(self):
        self.connection.getresponse.side_effect = TimeoutError()
        with self.assertRaisesRegex(reload_script.ValidationError, "TimeoutError"):
            reload_script.run(self.root)
        self.docker.assert_not_called()
        self.connection.close.assert_called_once()

    def test_missing_token_never_calls_api_or_restart(self):
        (self.root / ".env").write_text("UNRELATED=value\n")
        with self.assertRaisesRegex(reload_script.ValidationError, "HA_TOKEN is missing"):
            reload_script.run(self.root)
        self.http.assert_not_called()
        self.docker.assert_not_called()

    def test_success_restarts_exactly_once(self):
        reload_script.run(self.root)
        self.connection.request.assert_called_once_with(
            "POST", "/api/services/homeassistant/check_config", body=b"{}",
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        )
        self.docker.assert_called_once_with(
            ["docker", "restart", "homeassistant-homeassistant-1"],
            check=True, timeout=60,
        )

    def test_check_only_never_restarts(self):
        reload_script.run(self.root, restart=False)
        self.connection.request.assert_called_once()
        self.docker.assert_not_called()

    def test_yaml_fallback_uses_one_bounded_container_process(self):
        with patch.object(reload_script.importlib, "import_module", side_effect=ImportError):
            reload_script.validate_yaml(self.root)
        self.docker.assert_called_once_with(
            ["docker", "exec", "homeassistant-homeassistant-1", "python3",
             "/config/scripts/ha_safe_reload.py", "--yaml-only", "/config"],
            check=True, timeout=30,
        )


if __name__ == "__main__":
    unittest.main()
