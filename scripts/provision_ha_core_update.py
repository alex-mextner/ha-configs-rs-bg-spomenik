#!/usr/bin/env python3
# Copyright (c) 2026 Alex Ultra.
"""Provision the Core update notice using native flows; runtime stores are read-only."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import shlex
import sys
import tempfile
import time
from datetime import datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any, NoReturn
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_SUMMARY_LENGTH = 255
TITLE = "Home Assistant Core"
SUMMARY = (
    "1. Проверить свежий бэкап конфигурации и базы данных вне eMMC.\n"
    "2. Проверить несовместимые изменения от текущей до новой версии.\n"
    "3. Обновить закреплённую версию HA в конфигурации установки.\n"
    "4. Применить; проверить запуск HA, журнал и основные интеграции."
)
ENTRIES = "config/config_entries/entry"
FLOW = "config/config_entries/flow"
LOG = logging.getLogger("ha-core-update-notice")


class ProvisionError(RuntimeError):
    """Sanitized error, without response bodies or credentials."""


class APIUnavailableError(ProvisionError):
    """A transient read failure that readiness loops may retry."""


def fail(message: str) -> NoReturn:
    """Raise only sanitized operational details."""
    raise ProvisionError(message) from None


class NoRedirect(HTTPRedirectHandler):
    """Keep credentials at the local endpoint."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        """Reject all redirects."""
        return


class API:
    """Native HA JSON client with sanitized errors."""

    def __init__(self, token: str) -> None:
        """Use the existing runtime credential."""
        self.token = token
        self.opener = build_opener(NoRedirect)

    def request(self, method: str, path: str, data: Any = None) -> Any:
        """Send a bounded request without logging sensitive response bodies."""
        request = Request(
            f"http://127.0.0.1:8123/api/{path}",
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as error:
            message = f"HA {method} {path}: HTTP {error.code}"
            if method == "GET" and (error.code in {404, 408, 429, 500, 502, 503, 504}):
                raise APIUnavailableError(message) from None
            fail(message)
        except (OSError, HTTPException):
            message = f"HA {method} {path}: temporarily unavailable"
            if method == "GET":
                raise APIUnavailableError(message) from None
            fail(message)
        except ValueError:
            fail(f"HA {method} {path}: invalid JSON")


def read_token(path: Path) -> str:
    """Read only HA_TOKEN; never source or evaluate the rest of the env file."""
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip().removeprefix("export ")
        key, separator, value = line.partition("=")
        if separator and key.strip() == "HA_TOKEN":
            parts = shlex.split(value, comments=True)
            if len(parts) == 1 and parts[0]:
                return parts[0]
            break
    fail("HA_TOKEN is missing or invalid in the runtime env file")


def entry_fingerprint(entries: list[dict[str, Any]]) -> dict[str, tuple[Any, ...]]:
    """Normalize HA's live timestamp and its ISO timestamp in atomic saves."""
    result: dict[str, tuple[Any, ...]] = {}
    for entry in entries:
        if entry["domain"] not in {"version", "template"}:
            continue
        modified = entry["modified_at"]
        stamp = (
            datetime.fromisoformat(modified).timestamp()
            if isinstance(modified, str)
            else float(modified)
        )
        result[entry["entry_id"]] = (
            entry["domain"],
            round(stamp, 6),
            entry.get("disabled_by"),
        )
    return result


def snapshot(
    api: API, config: Path, wait: float = 60
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Compare IDs and modification times against live metadata around disk reads.

    HTTP omits data/options. These runtime files are ONLY READ, never modified.
    Inspect only Version/Template entries; never log other entries or credentials.
    """
    deadline = time.monotonic() + wait
    while True:
        try:
            live = api.request("GET", ENTRIES)
            before = entry_fingerprint(live)
            entries = json.loads((config / ".storage/core.config_entries").read_text())[
                "data"
            ]["entries"]
            entries = [e for e in entries if e["domain"] in {"version", "template"}]
            registry = json.loads(
                (config / ".storage/core.entity_registry").read_text()
            )["data"]["entities"]
            after = entry_fingerprint(api.request("GET", ENTRIES))
            if before == entry_fingerprint(entries) == after:
                return entries, registry
        except (OSError, ValueError, KeyError, APIUnavailableError):
            pass
        if time.monotonic() >= deadline:
            fail("HA metadata and runtime stores are not ready/in sync")
        time.sleep(1)


def single(entries: list[dict[str, Any]], description: str) -> dict[str, Any] | None:
    """Select one enabled record, refusing ambiguity."""
    if len(entries) > 1:
        fail(f"Ambiguous {description}; no entries will be replaced or deleted")
    if entries and entries[0].get("disabled_by"):
        fail(f"Existing {description} is disabled; leaving it unchanged")
    return entries[0] if entries else None


def version_entry(entries: list[dict[str, Any]], source: str) -> dict[str, Any] | None:
    """Find the local or stable Version entry."""
    return single(
        [
            e
            for e in entries
            if e["domain"] == "version"
            and e["data"].get("source") == source
            and (
                source == "local"
                or (
                    e["data"].get("channel") == "stable"
                    and e["data"].get("image") == "default"
                )
            )
        ],
        f"Version {source}",
    )


def entity_for(
    registry: list[dict[str, Any]], entry: dict[str, Any], domain: str
) -> str | None:
    """Resolve the preserved entity ID."""
    rows = [
        r
        for r in registry
        if r.get("config_entry_id") == entry["entry_id"]
        and r["entity_id"].startswith(domain + ".")
    ]
    row = single(rows, f"{domain} entity for {entry['entry_id']}")
    return row["entity_id"] if row else None


def source_entities(
    entries: list[dict[str, Any]], registry: list[dict[str, Any]]
) -> dict[str, str] | None:
    """Resolve both enabled sources and their current registry IDs from one snapshot."""
    sensors: dict[str, str] = {}
    for source in ("local", "supervisor"):
        entry = version_entry(entries, source)
        sensor = entity_for(registry, entry, "sensor") if entry else None
        if sensor is None:
            return None
        sensors[source] = sensor
    return sensors


def notice_entry(
    entries: list[dict[str, Any]], registry: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Find the existing Core update helper."""
    known = {
        r.get("config_entry_id")
        for r in registry
        if r["entity_id"] == "update.home_assistant_core"
    }
    return single(
        [
            e
            for e in entries
            if e["domain"] == "template"
            and e.get("options", {}).get("template_type") == "update"
            and (
                e["entry_id"] in known
                or e["options"].get("title") == TITLE
                or e["options"].get("name") == TITLE
            )
        ],
        "Home Assistant Core helper",
    )


def finish_flow(
    api: API, endpoint: str, handler: str, steps: list[tuple[str, dict[str, Any]]]
) -> None:
    """Submit only the expected native flow steps."""
    flow = api.request(
        "POST", endpoint, {"handler": handler, "show_advanced_options": False}
    )
    for step, payload in steps:
        if flow.get("step_id") != step or flow.get("errors"):
            fail(f"Unexpected native flow for {handler}")
        flow = api.request("POST", f"{endpoint}/{flow['flow_id']}", payload)
    if flow.get("type") != "create_entry":
        fail(f"Native flow for {handler} did not save successfully")


def desired_options(current: str, latest: str) -> dict[str, Any]:
    """Build a notice from runtime sensors, with no installer."""
    return {
        "installed_version": "{{ states('" + current + "') }}",
        "latest_version": "{{ states('" + latest + "') }}",
        "title": TITLE,
        "release_url": "https://github.com/home-assistant/core/releases/tag/{{ states('"
        + latest
        + "') }}",
        "release_summary": SUMMARY,
        "additional_options": {
            "availability": "{{ has_value('"
            + current
            + "') and has_value('"
            + latest
            + "') }}"
        },
    }


def ensure_sources(api: API, config: Path, *, check: bool, wait: float) -> bool:
    """Create missing Version entries and wait for registry records."""
    changed = False
    for source in ("local", "supervisor"):
        # A previous source's setup can take time; never create from the old snapshot.
        entries, _registry = snapshot(api, config, wait)
        if version_entry(entries, source) is None:
            if check:
                fail(f"Version {source} has not been provisioned")
            steps = [("user", {"version_source": "Local installation"})]
            if source == "supervisor":
                steps = [
                    ("user", {"version_source": "Home Assistant Versions"}),
                    (
                        "version_source",
                        {"channel": "Stable", "image": "default", "board": "ODROID N2"},
                    ),
                ]
            finish_flow(api, FLOW, "version", steps)
            changed = True
        deadline = time.monotonic() + wait
        while True:
            entries, registry = snapshot(
                api, config, max(0, deadline - time.monotonic())
            )
            entry = version_entry(entries, source)
            sensor = entity_for(registry, entry, "sensor") if entry else None
            if sensor:
                try:
                    api.request("GET", f"states/{sensor}")
                except APIUnavailableError:
                    pass
                else:
                    break
            if time.monotonic() >= deadline:
                fail(f"Version {source} entity is not yet saved; retry later")
            time.sleep(1)

    return changed


def reconcile(
    api: API, config: Path, *, check: bool = False, wait: float = 60
) -> dict[str, Any]:
    """Reuse restored entries or provision missing native helpers."""
    entries, registry = snapshot(api, config, wait)
    helper = notice_entry(entries, registry)
    # Find ambiguities in both sources before the first write.
    for source in ("local", "supervisor"):
        version_entry(entries, source)
    if helper and helper["options"].get("install"):
        fail("Existing helper installs updates; leaving it unchanged")
    changed = ensure_sources(api, config, check=check, wait=wait)
    entries, registry = snapshot(api, config, wait)
    # Either source may have been renamed while the other one was loading.
    sensors = source_entities(entries, registry)
    if sensors is None:
        fail("Version sources or entity IDs changed during setup; retry later")

    helper = notice_entry(entries, registry)
    desired = desired_options(sensors["local"], sensors["supervisor"])
    if helper is None:
        if check:
            fail("Home Assistant Core helper has not been provisioned")
        finish_flow(
            api,
            FLOW,
            "template",
            [
                ("user", {"next_step_id": "update"}),
                ("update", {"name": TITLE, **desired}),
            ],
        )
        changed = True
    else:
        existing = helper["options"]
        if existing.get("install"):
            fail("Existing helper installs updates; leaving it unchanged")
        desired["additional_options"] = {
            **existing.get("additional_options", {}),
            **desired["additional_options"],
        }
        differing = [
            key for key, value in desired.items() if existing.get(key) != value
        ]
        if differing:
            # HA options flows replace a copy taken when the form opened, without CAS.
            # Even our fields could overwrite a concurrent user's options.
            fail(
                "Core helper options differ from the declared notice ("
                + ", ".join(differing)
                + "); leaving existing options unchanged"
            )

    result = verify_notice(api, config, wait=wait)
    return {"changed": changed, **result}


def verify_notice(api: API, config: Path, *, wait: float) -> dict[str, Any]:
    """Confirm current source identities, available versions and no installer."""
    deadline = time.monotonic() + wait
    while True:
        entries, registry = snapshot(api, config, max(0, deadline - time.monotonic()))
        sensors = source_entities(entries, registry)
        helper = notice_entry(entries, registry)
        entity = entity_for(registry, helper, "update") if helper else None
        if helper and sensors and entity:
            desired = desired_options(sensors["local"], sensors["supervisor"])
            desired["additional_options"] = {
                **helper["options"].get("additional_options", {}),
                **desired["additional_options"],
            }
            saved = not helper["options"].get("install") and all(
                helper["options"].get(k) == v for k, v in desired.items()
            )
            try:
                if saved:
                    state = rendered_notice(api, entity, sensors)
                    if state is not None:
                        return {"entity_id": entity, "state": state}
            except APIUnavailableError:
                pass
        if time.monotonic() >= deadline:
            fail("Core helper or Version sources are not ready/current; retry later")
        time.sleep(1)


def rendered_notice(api: API, entity: str, sensors: dict[str, str]) -> str | None:
    """Check the rendered notice against the currently configured Version sensors."""
    state = api.request("GET", f"states/{entity}")
    attrs = state["attributes"]
    versions = {
        source: api.request("GET", f"states/{sensor}").get("state")
        for source, sensor in sensors.items()
    }
    valid_versions = all(
        isinstance(value, str) and value not in {"", "unknown", "unavailable"}
        for value in versions.values()
    )
    release_url = "https://github.com/home-assistant/core/releases/tag/" + str(
        versions["supervisor"]
    )
    if (
        state["state"] in {"on", "off"}
        and valid_versions
        and attrs.get("installed_version") == versions["local"]
        and attrs.get("latest_version") == versions["supervisor"]
        and attrs.get("release_url") == release_url
        and attrs.get("release_summary") == SUMMARY
        and not attrs.get("supported_features", 0) & 1
        and not attrs.get("auto_update", False)
    ):
        return state["state"]
    return None


def main() -> int:
    """Run provisioning or a read-only check with a private log."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-dir", type=Path, default=Path("/home/ultra/homeassistant")
    )
    parser.add_argument("--env-file", type=Path, default=Path("/home/ultra/.env"))
    parser.add_argument(
        "--check", action="store_true", help="Read-only verification; no flows"
    )
    parser.add_argument(
        "--lock-file", type=Path, default=Path("/run/ha-core-update-notice/lock")
    )
    args = parser.parse_args()
    try:
        log_dir = Path(tempfile.gettempdir()) / "ha-core-update-notice"
        log_dir.mkdir(mode=0o700, exist_ok=True)
        metadata = log_dir.lstat()
        if (
            log_dir.is_symlink()
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            fail("Private log directory has an unexpected owner or permissions")
        descriptor, log_path = tempfile.mkstemp(
            prefix="run-", suffix=".log", dir=log_dir
        )
        os.close(descriptor)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[logging.StreamHandler(), logging.FileHandler(log_path)],
        )
    except (ProvisionError, OSError) as error:
        # An unsafe log directory must not be opened, even to report its own failure.
        sys.stderr.write(f"Provisioning failed: {error}\n")
        return 1
    LOG.info("Started; log: %s", log_path)
    started = time.monotonic()
    try:
        if len(SUMMARY) > MAX_SUMMARY_LENGTH:
            fail("Update reminder exceeds HA's 255 character limit")
        api = API(read_token(args.env_file))
        if args.check:
            result = reconcile(api, args.config_dir, check=True)
        else:
            # systemd owns this runtime directory; no writes or locks live in .storage.
            with args.lock_file.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = reconcile(api, args.config_dir)
        LOG.info(
            "Configuration verified: %s; changed=%s; state=%s",
            result["entity_id"],
            result["changed"],
            result["state"],
        )
    except (ProvisionError, OSError, ValueError) as error:
        LOG.log(logging.ERROR, "Provisioning failed: %s", error)
        return 1
    else:
        return 0
    finally:
        LOG.info("Finished in %.1fs; log: %s", time.monotonic() - started, log_path)


if __name__ == "__main__":
    raise SystemExit(main())
