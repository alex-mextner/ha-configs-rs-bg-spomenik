# Copyright 2026 Project contributors
# ruff: noqa: INP001, PT009, S603
"""Real wrapper commands cannot mutate the stack before DNS is reserved."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WrapperTests(unittest.TestCase):
    """Fake only Docker's process boundary, retaining the production wrapper flow."""

    def run_wrapper(
        self, *args: str
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            log = tmp / "docker.log"
            docker = tmp / "docker"
            docker.write_text("""#!/usr/bin/env python3
import json, os, sys
args=sys.argv[1:]
with open(os.environ["WRAPPER_CALL_LOG"],"a") as f: f.write(json.dumps(args)+"\\n")
if "config" in args:
 print(json.dumps({"name":"homeassistant","networks":{"ha-net":{"name":"test-net"}},"services":{"ha-docker-dns":{"networks":{"ha-net":{"ipv4_address":"172.20.0.53"}}}}}))
elif args[:2] == ["network","ls"]:
 pass
elif "up" in args and "ha-docker-dns" in args:
 sys.exit(23)
""")
            docker.chmod(0o755)
            env = dict(
                os.environ,
                PATH=f"{tmp}:{os.environ['PATH']}",
                WRAPPER_CALL_LOG=str(log),
                HA_COMPOSE_ENV_FILE=str(tmp / "no-settings"),
                HA_COMPOSE_OVERLAYS="",
            )
            result = subprocess.run(
                [str(ROOT / "scripts/ha_compose.sh"), *args],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result, calls

    def test_dns_reservation_failure_prevents_other_service_changes(self) -> None:
        result, calls = self.run_wrapper("up", "-d", "wyoming-satellite")
        self.assertEqual(result.returncode, 23, result.stderr)
        mutations = [call for call in calls if "up" in call]
        self.assertEqual(len(mutations), 1)
        self.assertEqual(mutations[0][-1], "ha-docker-dns")
        self.assertIn("--wait", mutations[0])

    def test_exec_command_payload_is_not_a_compose_verb(self) -> None:
        result, calls = self.run_wrapper("exec", "wyoming-satellite", "run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-3:], ["exec", "wyoming-satellite", "run"])


if __name__ == "__main__":
    unittest.main()
