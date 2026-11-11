#!/usr/bin/env python3
"""Validate Home Assistant before a bounded restart of its existing container."""
from __future__ import annotations

import argparse
import http.client
import importlib
from pathlib import Path
import subprocess
import sys

CONTAINER = "homeassistant-homeassistant-1"
CHECK_TIMEOUT = 30
RESTART_TIMEOUT = 60


class ValidationError(RuntimeError):
    """A failed gate; the running Home Assistant must remain untouched."""


def validate_yaml(config_dir: Path) -> None:
    """Parse every package in one process, accepting HA-specific YAML tags."""
    try:
        yaml = importlib.import_module("yaml")
    except ImportError:
        # One fallback process for the entire batch, never one process per file.
        subprocess.run(
            ["docker", "exec", CONTAINER, "python3",
             "/config/scripts/ha_safe_reload.py", "--yaml-only", "/config"],
            check=True, timeout=CHECK_TIMEOUT,
        )
        return
    files = sorted((config_dir / "packages").glob("*.yaml"))
    if (config_dir / "configuration.yaml").exists():
        files.insert(0, config_dir / "configuration.yaml")
    if not files:
        raise ValidationError("No YAML configuration files found")
    for path in files:
        try:
            with path.open(encoding="utf-8") as handle:
                # Compose validates syntax without resolving !secret / !include.
                yaml.compose(handle, Loader=yaml.SafeLoader)
        except yaml.YAMLError as error:
            mark = getattr(error, "problem_mark", None)
            line = f" at line {mark.line + 1}" if mark is not None else ""
            raise ValidationError(f"{path.name}: YAML syntax invalid{line}") from None
    print(f"  YAML syntax valid ({len(files)} files).", flush=True)


def read_token(config_dir: Path) -> str:
    """Read only the existing HA_TOKEN; never execute or print the env file."""
    for raw_line in (config_dir / ".env").read_text(encoding="utf-8").splitlines():
        key, sep, value = raw_line.partition("=")
        if sep and key.strip() == "HA_TOKEN":
            token = value.strip()
            if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
                token = token[1:-1]
            if token:
                return token
    raise ValidationError("HA_TOKEN is missing; configuration check cannot run")


def validate_homeassistant(config_dir: Path) -> None:
    """Use the running core's supported check_config service; fail closed."""
    token = read_token(config_dir)
    connection = http.client.HTTPConnection("127.0.0.1", 8123, timeout=CHECK_TIMEOUT)
    try:
        connection.request(
            "POST", "/api/services/homeassistant/check_config", body=b"{}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        status = connection.getresponse().status
        if not 200 <= status < 300:
            raise ValidationError(f"Home Assistant check_config returned HTTP {status}")
    except (OSError, http.client.HTTPException) as error:
        # Do not expose headers, credentials, or potentially sensitive response bodies.
        raise ValidationError(
            f"Home Assistant check_config failed ({type(error).__name__})"
        ) from None
    finally:
        connection.close()
    print("  Home Assistant configuration check passed.", flush=True)


def run(config_dir: Path, *, restart: bool = True) -> None:
    print("Step 1/3: Checking YAML syntax...", flush=True)
    validate_yaml(config_dir)
    print("Step 2/3: Checking Home Assistant configuration...", flush=True)
    validate_homeassistant(config_dir)
    if not restart:
        print("Checks passed; no restart requested.", flush=True)
        return
    print("Step 3/3: Restarting Home Assistant...", flush=True)
    subprocess.run(
        ["docker", "restart", CONTAINER], check=True, timeout=RESTART_TIMEOUT
    )
    print("Home Assistant is restarting.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--yaml-only", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.yaml_only is not None:
            # The HA image must provide PyYAML; do not recursively try Docker.
            importlib.import_module("yaml")
            validate_yaml(args.yaml_only)
        else:
            run(Path(__file__).resolve().parent.parent, restart=not args.check_only)
    except (ValidationError, OSError, ImportError, subprocess.SubprocessError) as error:
        print(f"ABORTED: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
