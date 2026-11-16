#!/usr/bin/env python3
"""
End-to-end check of HA's external URL (Dataplicity tunnel), every 5 min from cron.

Log every probe to ~/dataplicity-watchdog/probes.jsonl. After two failed probes
in a row, reload the Dataplicity config entry once per outage and raise an HA
notification. On recovery, dismiss it and log the outage length.

Cron runs this script every five minutes as ultra and appends output to
/tmp/dataplicity_watchdog.log.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

FAILURES_BEFORE_RELOAD = 2

HA = "http://127.0.0.1:8123"
CONFIG = Path("/home/ultra/homeassistant/.storage")
STATE_DIR = Path.home() / "dataplicity-watchdog"
NID = "dataplicity_watchdog"


class _HTTPOnlyHandler(urllib.request.BaseHandler):
    """Reject non-HTTP schemes before proxy or protocol dispatch."""

    def default_open(self, request: urllib.request.Request) -> None:
        """Apply the scheme boundary to initial and redirected requests."""
        if request.type not in {"http", "https"}:
            error_message = "Only HTTP and HTTPS URLs are supported"
            raise urllib.error.URLError(error_message)


def _http_opener() -> urllib.request.OpenerDirector:
    """Retain standard HTTP behavior without file, FTP, or data handlers."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        _HTTPOnlyHandler(),
        urllib.request.ProxyHandler(),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPRedirectHandler(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.HTTPSHandler(),
    ):
        opener.add_handler(handler)
    return opener


def ha_token() -> str:
    """Read the configured Home Assistant token from the user's environment file."""
    for line in (Path.home() / ".env").read_text().splitlines():
        if line.startswith("HA_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    error_message = "HA_TOKEN missing in ~/.env"
    raise SystemExit(error_message)


def ha(service: str, payload: dict) -> None:
    """Call a service on the configured local Home Assistant instance."""
    req = urllib.request.Request(  # noqa: S310 - Dispatched only by the HTTP(S)-only opener.
        f"{HA}/api/services/{service}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {ha_token()}",
            "Content-Type": "application/json",
        },
    )
    _http_opener().open(req, timeout=30).read()


def probe(url: str) -> tuple[bool, str, float]:
    """Measure whether the external URL serves the Home Assistant manifest."""
    started = time.monotonic()
    try:
        with _http_opener().open(
            url.rstrip("/") + "/manifest.json", timeout=20
        ) as resp:
            ok = resp.status == HTTPStatus.OK and b"Home Assistant" in resp.read(4096)
            return ok, str(resp.status), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return False, str(exc.code), time.monotonic() - started
    except Exception as exc:
        _LOGGER.exception("External manifest probe failed")
        return False, type(exc).__name__, time.monotonic() - started


def _reload_outage(entry: str, url: str, code: str, record: dict) -> None:
    """Keep the existing reload and notification action in one failure boundary."""
    try:
        ha("homeassistant/reload_config_entry", {"entry_id": entry})
        ha(
            "persistent_notification/create",
            {
                "notification_id": NID,
                "title": "Внешний доступ к HA пропал",
                "message": (
                    f"Dataplicity ({url}) не отвечает: {code}. "
                    "Интеграция перезапущена автоматически."
                ),
            },
        )
        record["action"] = "reloaded"
    except Exception as exc:
        _LOGGER.exception("Dataplicity reload failed")
        record["action"] = f"reload failed: {exc}"


def main() -> None:
    """Record tunnel health and apply the existing once-per-outage recovery."""
    url = json.loads((CONFIG / "core.config").read_text())["data"].get("external_url")
    entry = next(
        e["entry_id"]
        for e in json.loads((CONFIG / "core.config_entries").read_text())["data"][
            "entries"
        ]
        if e["domain"] == "dataplicity"
    )
    STATE_DIR.mkdir(exist_ok=True)
    state_file = STATE_DIR / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {"fails": 0}
    ok, code, seconds = probe(url)
    now = time.time()
    record = {"ts": int(now), "ok": ok, "code": code, "seconds": round(seconds, 2)}
    if ok:
        if state.get("down_since"):
            record["outage_minutes"] = round((now - state["down_since"]) / 60, 1)
            try:
                ha("persistent_notification/dismiss", {"notification_id": NID})
            except Exception:
                _LOGGER.exception("Recovery notification dismissal failed")
        state = {"fails": 0}
    else:
        state["fails"] = state.get("fails", 0) + 1
        state.setdefault("down_since", now)
        if state["fails"] >= FAILURES_BEFORE_RELOAD and not state.get("reloaded"):
            _reload_outage(entry, url, code, record)
            state["reloaded"] = True
    with (STATE_DIR / "probes.jsonl").open("a") as log:
        log.write(json.dumps(record) + "\n")
    state_file.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
