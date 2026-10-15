#!/usr/bin/env python3
# Copyright 2026 Project contributors
"""Refuse unsafe in-place Docker IPAM drift before Compose changes services."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Any


def validate_network(
    desired: dict[str, str],
    existing: dict[str, Any],
    dns_address: str,
    dns_container: str,
) -> None:
    """Check the actual bridge reservation, not only the desired Compose YAML."""
    configs = existing.get("IPAM", {}).get("Config", [])
    if not any(
        all(current.get(key, "") == value for key, value in desired.items())
        for current in configs
    ):
        message = (
            "Existing ha-net IPAM differs; refusing an in-place migration. "
            "Preserve running services and arrange an explicit network migration first."
        )
        raise ValueError(message)
    for container in (existing.get("Containers") or {}).values():
        if (
            container.get("IPv4Address", "").partition("/")[0] == dns_address
            and container.get("Name") != dns_container
        ):
            message = (
                f"DNS address {dns_address} is already occupied by another container"
            )
            raise ValueError(message)


def docker_output(args: list[str]) -> str:
    """Capture Docker output without exposing interpolated Compose credentials."""
    docker = shutil.which("docker")
    if docker is None:
        message = "Docker CLI is not installed"
        raise OSError(message)
    result = subprocess.run(  # noqa: S603 - fixed docker program, argument vector only
        [docker, *args], capture_output=True, text=True, check=False, timeout=30
    )
    if result.returncode:
        # Do not echo Compose stderr: an interpolation error may contain a secret.
        message = f"Docker network preflight command failed (exit {result.returncode})"
        raise RuntimeError(message)
    return result.stdout


def main() -> int:
    """Read the effective Compose configuration and inspect its existing network."""
    try:
        config = json.loads(
            docker_output(
                [
                    "compose",
                    *sys.argv[1:],
                    "config",
                    "--format",
                    "json",
                    "--no-env-resolution",
                ]
            )
        )
        net = config["networks"]["ha-net"]
        dns = config["services"]["ha-docker-dns"]
        dns_address = dns["networks"]["ha-net"]["ipv4_address"]
        network_name = net["name"]
        # An absent bridge is a normal first install. Daemon/permission failures
        # are not mistaken for absence: docker_output fails closed above.
        names = docker_output(["network", "ls", "--format", "{{.Name}}"]).splitlines()
        if network_name not in names:
            return 0
        existing = json.loads(docker_output(["network", "inspect", network_name]))[0]
        ipam = net["ipam"]["config"][0]
        desired = {
            "Subnet": ipam["subnet"],
            "Gateway": ipam["gateway"],
            "IPRange": ipam["ip_range"],
        }
        dns_container = dns.get("container_name", f"{config['name']}-ha-docker-dns-1")
        validate_network(desired, existing, dns_address, dns_container)
    except (
        KeyError,
        IndexError,
        ValueError,
        RuntimeError,
        OSError,
        subprocess.TimeoutExpired,
    ) as exc:
        sys.stderr.write(f"Bluetooth network preflight failed: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
