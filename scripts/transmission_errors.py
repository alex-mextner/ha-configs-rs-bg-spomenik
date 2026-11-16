#!/usr/bin/env python3
"""Print Transmission torrent errors as compact JSON for Home Assistant."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http import HTTPStatus

RPC_URL = os.environ.get(
    "TRANSMISSION_RPC_URL", "http://172.20.0.1:9091/transmission/rpc"
)
FIELDS = [
    "id",
    "name",
    "error",
    "errorString",
    "status",
    "percentDone",
    "downloadDir",
    "rateDownload",
    "rateUpload",
]


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


def rpc(method: str, arguments: dict | None = None) -> dict:
    """Call Transmission and repeat once when its session header is required."""
    payload = json.dumps({"method": method, "arguments": arguments or {}}).encode(
        "utf-8"
    )
    headers = {"Content-Type": "application/json"}
    for _ in range(2):
        request = urllib.request.Request(  # noqa: S310 - Dispatched only by the HTTP(S)-only opener.
            RPC_URL, data=payload, headers=headers, method="POST"
        )
        try:
            with _http_opener().open(request, timeout=8) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            session_id = exc.headers.get("X-Transmission-Session-Id")
            if exc.code == HTTPStatus.CONFLICT and session_id:
                headers["X-Transmission-Session-Id"] = session_id
                continue
            raise
    error_message = "Transmission RPC session negotiation failed"
    raise RuntimeError(error_message)


def main() -> int:
    """Emit the sensor's torrent inventory or its existing probe error payload."""
    try:
        data = rpc("torrent-get", {"fields": FIELDS})
        torrents = data.get("arguments", {}).get("torrents", [])
        normalized = []
        errors = []
        for torrent in torrents:
            item = {
                "id": torrent.get("id"),
                "name": torrent.get("name") or "",
                "error": int(torrent.get("error") or 0),
                "error_string": torrent.get("errorString") or "",
                "status": torrent.get("status"),
                "percent_done": round(float(torrent.get("percentDone") or 0) * 100, 2),
                "download_dir": torrent.get("downloadDir") or "",
                "rate_download": torrent.get("rateDownload") or 0,
                "rate_upload": torrent.get("rateUpload") or 0,
            }
            normalized.append(item)
            if item["error"] or item["error_string"]:
                errors.append(item)
        print(
            json.dumps(
                {"count": len(errors), "errors": errors, "torrents": normalized},
                ensure_ascii=False,
            )
        )
    except Exception as exc:  # noqa: BLE001 - sensor should report probe failures as data.
        print(
            json.dumps(
                {"count": 0, "errors": [], "torrents": [], "probe_error": repr(exc)},
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
