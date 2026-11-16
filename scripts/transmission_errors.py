#!/usr/bin/env python3
# noqa: EXE001 - Home Assistant launches this non-executable file via python.
"""Print Transmission torrent errors as compact JSON for Home Assistant."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http import HTTPStatus
from urllib.parse import urlsplit

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


def _rpc_url() -> str:
    parsed = urlsplit(RPC_URL)
    hostname = parsed.hostname
    # Reading port also rejects malformed or out-of-range authority ports.
    _ = parsed.port
    if parsed.scheme not in {"http", "https"} or not hostname:
        message = "Transmission RPC URL must have an HTTP(S) authority"
        raise ValueError(message)
    if any(character.isspace() for character in hostname):
        message = "Transmission RPC authority must not contain whitespace"
        raise ValueError(message)
    return RPC_URL


def rpc(method: str, arguments: dict | None = None) -> dict:
    """Call the configured HTTP(S) RPC endpoint with session-ID negotiation."""
    url = _rpc_url()
    payload = json.dumps({"method": method, "arguments": arguments or {}}).encode(
        "utf-8"
    )
    headers = {"Content-Type": "application/json"}
    for _ in range(2):
        request = urllib.request.Request(  # noqa: S310 - _rpc_url validates HTTP(S) and authority.
            url, data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 - Validated RPC URL; preserve urllib HTTP redirect behavior.
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            session_id = exc.headers.get("X-Transmission-Session-Id")
            if exc.code == HTTPStatus.CONFLICT and session_id:
                headers["X-Transmission-Session-Id"] = session_id
                continue
            raise
    message = "Transmission RPC session negotiation failed"
    raise RuntimeError(message)


def main() -> int:
    """Emit the existing JSON sensor record, including probe failures as data."""
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
        print(  # noqa: T201 - stdout is the Home Assistant JSON sensor contract.
            json.dumps(
                {"count": len(errors), "errors": errors, "torrents": normalized},
                ensure_ascii=False,
            )
        )
        return 0  # noqa: TRY300 - Keep stdout failures inside the sensor boundary.
    except Exception as exc:  # noqa: BLE001 - sensor should report probe failures as data.
        print(  # noqa: T201 - stdout is the Home Assistant JSON sensor contract.
            json.dumps(
                {"count": 0, "errors": [], "torrents": [], "probe_error": repr(exc)},
                ensure_ascii=False,
            )
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
