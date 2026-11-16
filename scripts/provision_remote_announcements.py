#!/usr/bin/env python3
"""Provision local Dialogs OAuth or a verified AISIS cache receipt, without playback."""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

OAUTH_URL = "https://oauth.yandex.ru/authorize?client_id=c473ca268cd749d3a8371351a8f2bcbd&response_type=token"
CONSOLE_URL = "https://dialogs.yandex.ru/developer/"
UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
KEY = re.compile(r"^[0-9a-f]{64}$")


def http_base(value: str) -> str:
    """Require an HTTP(S) HA endpoint without URL credentials or query data."""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        message = "Use an HTTP(S) Home Assistant base URL without credentials or query"
        raise ValueError(message)
    return value.rstrip("/")


def parse_args() -> argparse.Namespace:
    """Parse the intended provider and local provisioning paths."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=["dialogs", "aisis"])
    parser.add_argument("--config", type=Path, default=Path.home() / "homeassistant")
    parser.add_argument("--token-file", type=Path, default=Path.home() / ".env")
    parser.add_argument("--base-url", default="http://localhost:8123")
    parser.add_argument("--skill-id")
    parser.add_argument(
        "--receipt",
        type=Path,
        help="AISIS authenticated status response, no credentials",
    )
    parser.add_argument(
        "--no-prepare",
        action="store_true",
        help="Store settings before the HA component is installed",
    )
    return parser.parse_args()


def load_core() -> ModuleType:
    """Load cache identity helpers without importing the HA integration."""
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "remote_announcements_core",
        root / "custom_components/remote_announcements/core.py",
    )
    core = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = core
    spec.loader.exec_module(core)
    return core


def configure_dialogs(
    args: argparse.Namespace, settings: dict[str, Any], report: Callable[[str], None]
) -> None:
    """Validate hidden OAuth entry against the intended owned skill."""
    if not sys.stdin.isatty():
        message = "A private interactive terminal is required for hidden token entry"
        raise RuntimeError(message)
    report("Use the same Yandex account that owns your draft skill.")
    report("OAuth: " + OAUTH_URL)
    report("Create or reuse a draft skill and copy its ID: " + CONSOLE_URL)
    skill = args.skill_id or input("Owned skill ID: ").strip()
    if not UUID.fullmatch(skill):
        message = "invalid skill ID"
        raise ValueError(message)
    token = getpass.getpass("Dialogs OAuth token (hidden): ").strip()
    if not token:
        message = "missing token"
        raise ValueError(message)
    headers = {"Authorization": "OAuth " + token}
    for route in ["/status", f"/skills/{skill}/sounds"]:
        with urlopen(
            Request("https://dialogs.yandex.net/api/v1" + route, headers=headers),
            timeout=20,
        ) as response:
            if response.status != HTTPStatus.OK:
                message = "Dialogs permission validation failed"
                raise RuntimeError(message)
    previous = settings.get("dialogs", {})
    settings["dialogs"] = {
        "oauth_token": token,
        "skill_id": skill,
        "sounds": previous.get("sounds", {})
        if previous.get("skill_id") == skill
        else {},
    }
    report("Dialogs authorization and owned skill verified.")


def configure_aisis(
    args: argparse.Namespace,
    settings: dict[str, Any],
    core: ModuleType,
    path: Path,
    report: Callable[[str], None],
) -> None:
    """Accept only a ready receipt matching all five HA clip identities."""
    if not args.receipt:
        message = "--receipt is required for AISIS"
        raise ValueError(message)
    receipt = json.loads(args.receipt.read_text())
    keys = receipt.get("keys", [])
    if (
        receipt.get("installed") is not True
        or receipt.get("ready") is not True
        or receipt.get("voice_id") != core.VOICE_ID
        or len(set(keys)) != len(core.PHRASES)
        or not all(KEY.fullmatch(key) for key in keys)
    ):
        message = "AISIS cache is not ready"
        raise ValueError(message)
    expected = {
        core.ClipCache(path.parent, core.VOICE_ID, core.DEFAULT_TEMPO).key(target)
        for target in core.PHRASES
    }
    if set(keys) != expected:
        message = "AISIS cache does not match the prepared HA clips"
        raise ValueError(message)
    settings["aisis"] = {"ready": True, "keys": sorted(keys)}
    report("AISIS cache receipt verified; the dedicated selector chooses the speaker.")


def prepare_home_assistant(
    args: argparse.Namespace, report: Callable[[str], None]
) -> None:
    """Request preparation using the locally stored HA credential."""
    ha_token = None
    for line in args.token_file.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "HA_TOKEN":
            ha_token = value.strip().strip("\"'")
    if not ha_token:
        message = "HA_TOKEN missing"
        raise ValueError(message)
    base = http_base(args.base_url)
    request = Request(  # noqa: S310 -- base is restricted to HTTP(S) by http_base.
        base + "/api/services/remote_announcements/prepare",
        data=b"{}",
        headers={
            "Authorization": "Bearer " + ha_token,
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=180) as response:  # noqa: S310 -- validated HA HTTP(S) request.
        response.read(16384)
    report("Home Assistant preparation finished; inspect sensor.remote_announcements.")


def provision(args: argparse.Namespace, report: Callable[[str], None]) -> None:
    """Persist private provider settings and optionally prepare HA resources."""
    core = load_core()
    path = args.config / ".tts/remote_announcements/providers.json"
    settings = json.loads(path.read_text()) if path.exists() else {}
    if args.provider == "dialogs":
        configure_dialogs(args, settings, report)
    else:
        configure_aisis(args, settings, core, path, report)
    core.atomic_write(path, json.dumps(settings).encode())
    report("Private provider configuration saved.")
    if not args.no_prepare:
        prepare_home_assistant(args, report)


def main() -> None:
    """Provision the chosen provider and log only sanitized progress."""
    args = parse_args()
    os.umask(0o077)
    fd, log_name = tempfile.mkstemp(
        prefix="remote-announcements-provision-", suffix=".log"
    )
    os.close(fd)
    started = time.monotonic()

    def report(message: str) -> None:
        """Write nonsecret progress to the terminal and the private task log."""
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
        with Path(log_name).open("a") as log:
            log.write(f"{time.time():.3f} {message}\n")

    report(f"Log: {log_name}")
    try:
        provision(args, report)
        report(f"Completed in {time.monotonic() - started:.3f}s. Log: {log_name}")
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        ImportError,
    ) as error:
        reason = type(error).__name__
        if isinstance(error, HTTPError):
            reason += f" HTTP {error.code}"
        report(f"Provisioning failed: {reason}. Log: {log_name}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
