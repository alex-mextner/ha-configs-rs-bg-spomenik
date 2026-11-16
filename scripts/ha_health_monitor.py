#!/usr/bin/env python3
"""
Home Assistant health monitor and fail-safe manager.

Monitors HA availability, orphaned entities, and provides safe restart procedures.
Run via cron: */5 * * * * /opt/scripts/ha_health_monitor.py
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

_HTTP_PORTS = {"http": 80, "https": 443}
_MAX_PORT = 65535


def _http_origin(value: str) -> tuple[str, str, int]:
    """Validate an HTTP(S) authority without accepting URL credentials."""
    message = "Expected an HTTP(S) URL with a valid authority and no credentials"
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError(message) from None
    if (
        parsed.scheme not in _HTTP_PORTS
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(character.isspace() for character in parsed.netloc)
        or any(character in value for character in "\r\n\t")
        or (port is not None and not 0 < port <= _MAX_PORT)
    ):
        raise ValueError(message)
    return parsed.scheme, parsed.hostname, port or _HTTP_PORTS[parsed.scheme]


@contextmanager
def _logging_context() -> Iterator[None]:
    """Keep one private opened log and report its path at both boundaries."""
    descriptor, name = tempfile.mkstemp(prefix="ha-health-monitor-", suffix=".log")
    with os.fdopen(descriptor, "a") as stream:
        handler = logging.StreamHandler(stream)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[handler, logging.StreamHandler()],
        )
        logger.info("Log: %s", name)
        try:
            yield
        finally:
            logger.info("Log: %s", name)
            logging.getLogger().removeHandler(handler)
            handler.close()


def _backup_registry(path: Path) -> Path:
    """Create and flush a private backup before allowing a registry write."""
    descriptor, name = tempfile.mkstemp(
        prefix=f"core.entity_registry.backup.{int(time.time())}."
    )
    backup = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as destination, path.open("rb") as source:
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    return backup


@dataclass
class HAConfig:
    """Connection settings and the managed HA container name."""

    url: str
    token: str | None
    container_name: str


class HAMonitor:
    """Inspect HA and its local registry through the configured boundaries."""

    def __init__(self, config: HAConfig) -> None:
        """Retain the settings and create the bounded HTTP client."""
        self.config = config
        self.client = httpx.Client(timeout=10.0)

    def is_ha_available(self) -> bool:
        """Check if HA is accessible."""
        try:
            _http_origin(self.config.url)
            resp = self.client.get(f"{self.config.url}/", follow_redirects=True)
            available = resp.status_code == HTTPStatus.OK
        except Exception as error:  # noqa: BLE001 -- Availability probes return a status for all client failures.
            logger.error("HA availability check failed: %s", error)  # noqa: TRY400 -- Preserve the concise monitoring log contract.
            return False
        return available

    def get_entities(self) -> list[dict]:
        """Get all entities from HA."""
        headers = (
            {"Authorization": f"Bearer {self.config.token}"}
            if self.config.token
            else {}
        )
        try:
            _http_origin(self.config.url)
            resp = self.client.get(f"{self.config.url}/api/states", headers=headers)
            entities = resp.json()
        except Exception as error:  # noqa: BLE001 -- Retrieval has an existing empty-result recovery contract.
            logger.error("Failed to get entities: %s", error)  # noqa: TRY400 -- Preserve the concise monitoring log contract.
            return []
        return entities

    def get_entity_registry(self) -> dict:
        """Get entity registry from storage."""
        registry_path = Path("/home/ultra/homeassistant/.storage/core.entity_registry")
        if registry_path.exists():
            with registry_path.open() as f:
                return json.load(f)
        return {}

    def find_orphaned_entities(self) -> list[dict]:
        """Find orphaned entities in registry."""
        registry = self.get_entity_registry()
        entities = registry.get("data", {}).get("entities", [])
        return [e for e in entities if e.get("orphaned_timestamp")]

    def find_duplicate_tts_entities(self) -> list[dict]:
        """Find duplicate TTS entities (same entity_id)."""
        registry = self.get_entity_registry()
        entities = registry.get("data", {}).get("entities", [])
        tts_entities = [
            e for e in entities if e.get("entity_id", "").startswith("tts.")
        ]
        by_id = {}
        for e in tts_entities:
            eid = e["entity_id"]
            if eid not in by_id:
                by_id[eid] = []
            by_id[eid].append(e)
        return {k: v for k, v in by_id.items() if len(v) > 1}

    def get_pipelines(self) -> list[dict]:
        """Get assist pipelines."""
        path = Path("/home/ultra/homeassistant/.storage/assist_pipeline.pipelines")
        if path.exists():
            with path.open() as f:
                data = json.load(f)
                return data.get("data", {}).get("items", [])
        return []

    def validate_tts_pipeline(self) -> dict:
        """Validate TTS configuration in pipelines."""
        pipelines = self.get_pipelines()
        return [self._tts_pipeline_status(p) for p in pipelines]

    def _tts_pipeline_status(self, pipeline: dict) -> dict:
        """Keep one pipeline's original registry lookup and status values."""
        tts_engine = pipeline.get("tts_engine")
        if not tts_engine:
            return {
                "pipeline": pipeline["name"],
                "tts_engine": None,
                "is_valid": True,
                "has_config_entry": False,
                "is_orphaned": False,
            }
        registry = self.get_entity_registry()
        entities = registry.get("data", {}).get("entities", [])
        tts_entity = next((e for e in entities if e["entity_id"] == tts_engine), None)
        is_valid = (
            tts_entity
            and not tts_entity.get("orphaned_timestamp")
            and tts_entity.get("config_entry_id")
        )
        return {
            "pipeline": pipeline["name"],
            "tts_engine": tts_engine,
            "is_valid": is_valid,
            "has_config_entry": bool(tts_entity and tts_entity.get("config_entry_id"))
            if tts_entity
            else False,
            "is_orphaned": bool(tts_entity and tts_entity.get("orphaned_timestamp"))
            if tts_entity
            else True,
        }

    def fix_orphaned_entity(self, entity_id: str) -> bool:
        """Remove orphaned entity from registry."""
        registry_path = Path("/home/ultra/homeassistant/.storage/core.entity_registry")
        registry = self.get_entity_registry()
        entities = registry.get("data", {}).get("entities", [])
        original_count = len(entities)
        entities = [
            e
            for e in entities
            if e.get("entity_id") != entity_id or e.get("config_entry_id")
        ]
        if len(entities) < original_count:
            backup_path = _backup_registry(registry_path)
            registry["data"]["entities"] = entities
            with registry_path.open("w") as f:
                json.dump(registry, f, indent=2)
            logger.info(
                "Fixed orphaned entity: %s (backup: %s)", entity_id, backup_path
            )
            return True
        return False

    def restart_ha(self, reason: str = "manual") -> bool:
        """Safely restart HA container."""
        logger.info("Restarting HA: %s", reason)
        try:
            subprocess.run(  # noqa: S603 -- Application-owned container argument; no shell.
                ["docker", "restart", self.config.container_name],  # noqa: S607 -- Preserve the owner-controlled Docker CLI lookup.
                check=True,
            )
            time.sleep(30)
            for _ in range(30):
                if self.is_ha_available():
                    logger.info("HA restarted successfully")
                    return True
                time.sleep(2)
            logger.error("HA did not come back after restart")
        except Exception as error:  # noqa: BLE001 -- The restart API reports all command/wait failures as False.
            logger.error("Failed to restart HA: %s", error)  # noqa: TRY400 -- Preserve the concise recovery log contract.
        return False

    def check_and_fix_tts(self) -> dict:
        """Check and fix TTS configuration."""
        result = {"orphaned_removed": 0, "duplicates_found": {}, "pipeline_status": []}
        orphaned = self.find_orphaned_entities()
        if orphaned:
            tts_orphaned = [
                e for e in orphaned if e.get("entity_id", "").startswith("tts.")
            ]
            for e in tts_orphaned:
                self.fix_orphaned_entity(e["entity_id"])
                result["orphaned_removed"] += 1
        duplicates = self.find_duplicate_tts_entities()
        result["duplicates_found"] = {k: len(v) for k, v in duplicates.items()}
        result["pipeline_status"] = self.validate_tts_pipeline()
        return result


def get_ha_token() -> str | None:
    """Get HA token from environment or file."""
    token = os.environ.get("HA_LONG_LIVE_TOKEN")
    if token:
        return token
    token_file = Path("/home/ultra/homeassistant/secrets.yaml")
    if token_file.exists():
        with token_file.open() as f:
            for line in f:
                if line.startswith("ha_token:"):
                    return line.split(":", 1)[1].strip()
    return None


def _run_checks() -> None:
    """Run the monitor and print its existing JSON status report."""
    config = HAConfig(
        url="http://localhost:8123",
        token=get_ha_token(),
        container_name="homeassistant-homeassistant-1",
    )

    monitor = HAMonitor(config)

    if not monitor.is_ha_available():
        logger.error("HA is not available!")
        sys.exit(1)

    orphaned = monitor.find_orphaned_entities()
    if orphaned:
        logger.warning("Found %s orphaned entities", len(orphaned))
        for e in orphaned[:5]:
            logger.info("  - %s (%s)", e["entity_id"], e.get("platform"))

    result = monitor.check_and_fix_tts()
    logger.info("TTS status: %s", result)

    print(json.dumps(result, indent=2))  # noqa: T201 -- JSON on stdout is the CLI report.


def main() -> None:
    """Run the existing checks with a private log and unchanged JSON stdout."""
    with _logging_context():
        _run_checks()


if __name__ == "__main__":
    main()
