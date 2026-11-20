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
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn
from urllib.error import HTTPError, URLError
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
OPTIONS = "config/config_entries/options/flow"
LOG = logging.getLogger("ha-core-update-notice")


class ProvisionError(RuntimeError):
    """Sanitized error, without response bodies or credentials."""


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
            fail(f"HA {method} {path}: HTTP {error.code}")
        except (URLError, TimeoutError, ValueError):
            fail(f"HA {method} {path}: unavailable or invalid JSON")


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
        except (OSError, ValueError, KeyError, ProvisionError):
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


def update_options(api: API, helper: dict[str, Any], desired: dict[str, Any]) -> None:
    """Use the current native schema and refuse concurrent options changes."""
    flow = api.request(
        "POST", OPTIONS, {"handler": helper["entry_id"], "show_advanced_options": False}
    )
    if flow.get("type") != "form" or flow.get("step_id") != "update":
        fail("Unexpected native Core helper options form")
    live = api.request("GET", ENTRIES)
    if entry_fingerprint([helper]).get(helper["entry_id"]) != entry_fingerprint(
        live
    ).get(helper["entry_id"]):
        fail("Core helper changed while opening its options; retry later")
    editable = {field["name"]: field for field in flow["data_schema"]}
    existing = helper["options"]
    payload = {key: value for key, value in existing.items() if key in editable}
    for key, value in payload.items():
        description = editable[key].get("description", {})
        if "suggested_value" in description and description["suggested_value"] != value:
            fail("Native Core helper options differ from the checked snapshot")
    if not desired.keys() <= editable.keys():
        fail("Native Core helper options schema does not support the declared fields")
    payload.update(desired)
    result = api.request("POST", f"{OPTIONS}/{flow['flow_id']}", payload)
    if result.get("type") != "create_entry":
        fail("Native Core helper options did not save successfully")


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


def ensure_sources(
    api: API, config: Path, sources: dict[str, Any], *, check: bool, wait: float
) -> tuple[dict[str, str], bool]:
    """Create missing Version entries and wait for registry records."""
    changed = False
    sensors: dict[str, str] = {}
    for source, initial_entry in sources.items():
        if initial_entry is None:
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
            entries, registry = snapshot(api, config, wait)
            entry = version_entry(entries, source)
            sensor = entity_for(registry, entry, "sensor") if entry else None
            if sensor:
                # Refuse a stale registry instead of using a missing/renamed entity.
                api.request("GET", f"states/{sensor}")
                sensors[source] = sensor
                break
            if time.monotonic() >= deadline:
                fail(f"Version {source} entity is not yet saved; retry later")
            time.sleep(1)

    return sensors, changed


def reconcile(
    api: API, config: Path, *, check: bool = False, wait: float = 60
) -> dict[str, Any]:
    """Reuse restored entries or provision missing native helpers."""
    entries, registry = snapshot(api, config, wait)
    helper = notice_entry(entries, registry)
    # Find ambiguities in both sources before the first write.
    sources = {
        source: version_entry(entries, source) for source in ("local", "supervisor")
    }
    if helper and helper["options"].get("install"):
        fail("Existing helper installs updates; leaving it unchanged")
    sensors, changed = ensure_sources(api, config, sources, check=check, wait=wait)
    entries, registry = snapshot(api, config, wait)

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
        if any(existing.get(key) != value for key, value in desired.items()):
            if check:
                fail("Core helper options differ from the declared notice")
            update_options(api, helper, desired)
            changed = True

    return verify_notice(api, config, desired, wait=wait, changed=changed)


def verify_notice(
    api: API, config: Path, desired: dict[str, Any], *, wait: float, changed: bool
) -> dict[str, Any]:
    """Confirm the saved helper loaded and cannot install updates."""
    deadline = time.monotonic() + wait
    while True:
        entries, registry = snapshot(api, config, wait)
        helper = notice_entry(entries, registry)
        entity = entity_for(registry, helper, "update") if helper else None
        saved = helper and all(
            helper["options"].get(k) == v for k, v in desired.items()
        )
        if entity and saved:
            state = api.request("GET", f"states/{entity}")
            attrs = state["attributes"]
            if (
                attrs.get("release_summary") == SUMMARY
                and not attrs.get("supported_features", 0) & 1
                and not attrs.get("auto_update", False)
            ):
                return {
                    "changed": changed,
                    "entity_id": entity,
                    "state": state["state"],
                }
        if time.monotonic() >= deadline:
            fail("Core helper has not finished saving/loading; retry later")
        time.sleep(1)


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
    descriptor, log_path = tempfile.mkstemp(
        prefix="ha-core-update-notice-", suffix=".log", dir="/tmp"
    )
    os.close(descriptor)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_path)],
    )
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
