#!/usr/bin/env python3
"""
End-to-end check of HA's external URL (Dataplicity tunnel), every 5 min from cron.

Logs every probe to ~/dataplicity-watchdog/probes.jsonl. After two failed probes
in a row it reloads the Dataplicity config entry once per outage and raises an HA
notification. On recovery it dismisses it and logs the outage length. Cron runs
this script every five minutes and appends stdout/stderr to
/tmp/dataplicity_watchdog.log.
"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from email.message import Message
    from http.client import HTTPResponse

HA = "http://127.0.0.1:8123"
CONFIG = Path("/home/ultra/homeassistant/.storage")
STATE_DIR = Path.home() / "dataplicity-watchdog"
NID = "dataplicity_watchdog"
_RELOAD_FAILURE_THRESHOLD = 2


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


class _HTTPRedirects(urllib.request.HTTPRedirectHandler):
    """Keep HTTP(S) redirects from forwarding bearer credentials to another origin."""

    def redirect_request(  # noqa: PLR0913 -- Required HTTPRedirectHandler override signature.
        self,
        req: urllib.request.Request,
        fp: HTTPResponse | None,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Validate a redirect before the standard handler builds its request."""
        origin = _http_origin(newurl)
        if req.has_header("Authorization") and origin != _http_origin(req.full_url):
            message = "Authenticated redirect changed origin"
            raise urllib.error.HTTPError(req.full_url, code, message, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_http(request: str | urllib.request.Request, *, timeout: int) -> HTTPResponse:
    """Validate the initial URL and every redirect before opening."""
    _http_origin(
        request.full_url if isinstance(request, urllib.request.Request) else request
    )
    return urllib.request.build_opener(_HTTPRedirects()).open(request, timeout=timeout)


def ha_token() -> str:
    """Read the existing owner-local HA credential without logging it."""
    for line in (Path.home() / ".env").read_text().splitlines():
        if line.startswith("HA_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    message = "HA_TOKEN missing in ~/.env"
    raise SystemExit(message)


def ha(service: str, payload: dict) -> None:
    """Call the configured local HA service with the existing timeout."""
    req = urllib.request.Request(  # noqa: S310 -- HA is the fixed loopback HTTP origin.
        f"{HA}/api/services/{service}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {ha_token()}",
            "Content-Type": "application/json",
        },
    )
    _open_http(req, timeout=30).read()


def probe(url: str) -> tuple[bool, str, float]:
    """Check the public manifest and report status plus elapsed time."""
    started = time.monotonic()
    try:
        with _open_http(url.rstrip("/") + "/manifest.json", timeout=20) as resp:
            ok = resp.status == HTTPStatus.OK and b"Home Assistant" in resp.read(4096)
            return ok, str(resp.status), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return False, str(exc.code), time.monotonic() - started
    except Exception as exc:  # noqa: BLE001 -- Every failed probe becomes a recorded status.
        return False, type(exc).__name__, time.monotonic() - started


def main() -> None:
    """Record one probe and perform the existing once-per-outage recovery."""
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
            # Dismissal remains best effort after a successful availability probe.
            with contextlib.suppress(Exception):
                ha("persistent_notification/dismiss", {"notification_id": NID})
        state = {"fails": 0}
    else:
        state["fails"] = state.get("fails", 0) + 1
        state.setdefault("down_since", now)
        if state["fails"] >= _RELOAD_FAILURE_THRESHOLD and not state.get("reloaded"):
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
            except Exception as exc:  # noqa: BLE001 -- Persist failed recovery details in the existing probe record.
                record["action"] = f"reload failed: {exc}"
            state["reloaded"] = True
    with (STATE_DIR / "probes.jsonl").open("a") as log:
        log.write(json.dumps(record) + "\n")
    state_file.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
