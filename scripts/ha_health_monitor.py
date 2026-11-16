#!/usr/bin/env python3
"""
Home Assistant health monitor and fail-safe manager.

Monitors HA availability, orphaned entities, and provides safe restart procedures.
Run via cron: */5 * * * * /opt/scripts/ha_health_monitor.py
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

import httpx


def _private_log_handler() -> logging.FileHandler:
    """Create a private log file whose stream is owned by logging."""
    descriptor, path = tempfile.mkstemp(prefix="ha-health-", suffix=".log")
    os.close(descriptor)
    return logging.FileHandler(path)


_LOG_HANDLER = _private_log_handler()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[_LOG_HANDLER, logging.StreamHandler()],
)
logger = logging.getLogger(__name__)
logger.info("Private health log: %s", _LOG_HANDLER.baseFilename)


@dataclass
class HAConfig:
    """Connection settings for the configured Home Assistant instance."""

    url: str
    token: str | None
    container_name: str


class HAMonitor:
    """Inspect Home Assistant availability, registries, and speech pipelines."""

    def __init__(self, config: HAConfig) -> None:
        """Create the HTTP client for the supplied Home Assistant settings."""
        self.config = config
        self.client = httpx.Client(timeout=10.0)

    def is_ha_available(self) -> bool:
        """Check if HA is accessible."""
        try:
            resp = self.client.get(f"{self.config.url}/", follow_redirects=True)
            available = resp.status_code == HTTPStatus.OK
        except Exception:
            logger.exception("HA availability check failed")
            return False
        else:
            return available

    def get_entities(self) -> list[dict]:
        """Get all entities from HA."""
        headers = (
            {"Authorization": f"Bearer {self.config.token}"}
            if self.config.token
            else {}
        )
        try:
            resp = self.client.get(f"{self.config.url}/api/states", headers=headers)
            return resp.json()
        except Exception:
            logger.exception("Failed to get entities")
            return []

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

    def find_duplicate_tts_entities(self) -> dict[str, list[dict]]:
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

    def _append_pipeline_status(self, p: dict, results: list[dict]) -> None:
        """Append one status using a fresh registry read for its TTS engine."""
        tts_engine = p.get("tts_engine")
        if tts_engine:
            registry = self.get_entity_registry()
            entities = registry.get("data", {}).get("entities", [])
            tts_entity = next(
                (e for e in entities if e["entity_id"] == tts_engine), None
            )
            is_valid = (
                tts_entity
                and not tts_entity.get("orphaned_timestamp")
                and tts_entity.get("config_entry_id")
            )
            results.append(
                {
                    "pipeline": p["name"],
                    "tts_engine": tts_engine,
                    "is_valid": is_valid,
                    "has_config_entry": bool(
                        tts_entity and tts_entity.get("config_entry_id")
                    )
                    if tts_entity
                    else False,
                    "is_orphaned": bool(
                        tts_entity and tts_entity.get("orphaned_timestamp")
                    )
                    if tts_entity
                    else True,
                }
            )
        else:
            results.append(
                {
                    "pipeline": p["name"],
                    "tts_engine": None,
                    "is_valid": True,
                    "has_config_entry": False,
                    "is_orphaned": False,
                }
            )

    def validate_tts_pipeline(self) -> list[dict]:
        """Validate TTS configuration in pipelines."""
        pipelines = self.get_pipelines()
        results = []
        for p in pipelines:
            self._append_pipeline_status(p, results)
        return results

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
            docker = _docker_executable()
            subprocess.run(  # noqa: S603 - Resolved Docker executable, fixed command, container name after --.
                [docker, "restart", "--", self.config.container_name],
                check=True,
            )
            time.sleep(30)
            for _ in range(30):
                if self.is_ha_available():
                    logger.info("HA restarted successfully")
                    return True
                time.sleep(2)
            logger.error("HA did not come back after restart")
        except Exception:
            logger.exception("Failed to restart HA")
            return False
        else:
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


def _docker_executable() -> str:
    """Resolve the same configured Docker executable before subprocess dispatch."""
    executable = shutil.which("docker")
    if executable is None:
        error_message = "Docker executable not found"
        raise FileNotFoundError(error_message)
    return executable


def _backup_registry(registry_path: Path) -> Path:
    """Complete a private backup before allowing the registry to be rewritten."""
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix="core.entity_registry.backup.",
        suffix=".json",
        delete=False,
    ) as backup:
        backup_path = Path(backup.name)
        try:
            with registry_path.open("rb") as source:
                shutil.copyfileobj(source, backup)
            backup.flush()
            os.fsync(backup.fileno())
        except OSError:
            backup_path.unlink(missing_ok=True)
            raise
    return backup_path


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


def main() -> None:
    """Run the configured health checks and print their existing JSON report."""
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

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
