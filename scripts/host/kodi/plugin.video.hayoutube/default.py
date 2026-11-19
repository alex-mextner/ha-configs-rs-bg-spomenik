"""Open only the current prepared request; never perform network extraction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs

MANIFEST_BASE_URL = "http://172.20.0.1:8787/kodi/youtube/manifest"
PRIVATE_FILE_MODE = 0o600
LOG_ACTIVE_GRACE_SECONDS = 120
# Log files are uniquely named, mode 0600 and owned; /tmp itself is shared.
LOG_DIRECTORY = Path("/tmp")  # noqa: S108
MAX_TOKEN_BYTES = 4096
MAX_DESCRIPTOR_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
LOG_PREFIX = "ha-kodi-youtube-adapter-"
LOG_NAME = re.compile(re.escape(LOG_PREFIX) + r"[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9_-]+\.log")
MANIFEST_ELEMENTS = {
    "MPD",
    "Period",
    "AdaptationSet",
    "Role",
    "Representation",
    "AudioChannelConfiguration",
    "BaseURL",
    "SegmentBase",
    "Initialization",
}


class RequestError(Exception):
    """A safe fixed error code."""


def owned_log_mtime(path: Path) -> float | None:
    """Recognize private adapter logs without trusting a filename alone."""
    if not LOG_NAME.fullmatch(path.name):
        return None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            return None
        if stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE:
            return None
        with path.open("r") as stream:
            first_line = stream.readline(512)
        if " starting log=" + str(path) not in first_line:
            return None
    except (OSError, UnicodeError):
        return None
    return metadata.st_mtime


def prune_logs(
    directory: Path = LOG_DIRECTORY,
    now: float | None = None,
    keep: int = 64,
) -> None:
    """Keep recent logs and prune only owned logs beyond count or age limits."""
    now = time.time() if now is None else now
    owned = []
    for path in directory.glob(LOG_PREFIX + "*.log"):
        modified = owned_log_mtime(path)
        if modified is not None:
            owned.append((modified, path))
    for index, (modified, path) in enumerate(sorted(owned, reverse=True)):
        if now - modified < LOG_ACTIVE_GRACE_SECONDS:
            continue
        if index >= keep or now - modified > 7 * 86400:
            path.unlink(missing_ok=True)


def read_json(path: Path, limit: int) -> dict[str, Any]:
    """Read a bounded task file without accepting symlinks."""
    if path.is_symlink() or not path.is_file():
        msg = "state_missing"
        raise RequestError(msg)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        msg = "state_oversize"
        raise RequestError(msg)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        msg = "state_invalid"
        raise RequestError(msg) from exc
    if not isinstance(value, dict):
        msg = "state_invalid"
        raise RequestError(msg)
    return value


def require_current(state: Path, request_id: str) -> None:
    """Check controller generation immediately before resolving to Kodi."""
    if read_json(state / "current-request.json", 4096).get("request_id") != request_id:
        msg = "request_cancelled"
        raise RequestError(msg)


def parse_request_id(query: str) -> str:
    """Accept exactly one canonical UUID from the plugin query."""
    values = parse_qs(query.lstrip("?"), strict_parsing=True)
    if set(values) != {"request_id"} or len(values["request_id"]) != 1:
        msg = "invalid_request"
        raise RequestError(msg)
    request_id = values["request_id"][0]
    try:
        if str(UUID(request_id)) != request_id:
            raise ValueError  # noqa: TRY301 - normalize every malformed UUID
    except (ValueError, AttributeError) as exc:
        msg = "invalid_request"
        raise RequestError(msg) from exc
    return request_id


def load_descriptor(directory: Path, request_id: str) -> dict[str, Any]:
    """Require a descriptor bound to this exact request and manifest endpoint."""
    if directory.is_symlink():
        msg = "state_path_invalid"
        raise RequestError(msg)
    descriptor = read_json(directory / "descriptor.json", MAX_DESCRIPTOR_BYTES)
    expected = {
        "version": 1,
        "request_id": request_id,
        "file": MANIFEST_BASE_URL + "?" + urlencode({"request_id": request_id}),
        "manifest_file": str(directory / "manifest.mpd"),
    }
    if any(descriptor.get(key) != value for key, value in expected.items()):
        msg = "descriptor_invalid"
        raise RequestError(msg)
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(descriptor.get("video_id", ""))):
        msg = "descriptor_invalid"
        raise RequestError(msg)
    return descriptor


def verified_payload(manifest: Path, expected_digest: str) -> bytes:
    """Check the private MPD's size and digest before adding authentication."""
    if manifest.is_symlink() or not manifest.is_file():
        msg = "manifest_missing"
        raise RequestError(msg)
    with manifest.open("rb") as stream:
        payload = stream.read(MAX_MANIFEST_BYTES + 1)
    if not 0 < len(payload) <= MAX_MANIFEST_BYTES:
        msg = "manifest_changed"
        raise RequestError(msg)
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        msg = "manifest_changed"
        raise RequestError(msg)
    return payload


def validate_media_origin(value: Any) -> None:
    """Permit only ordinary public YouTube HTTPS media origins."""
    url = urlsplit(value or "")
    if url.scheme != "https" or not (url.hostname or "").endswith(".googlevideo.com"):
        msg = "media_origin_invalid"
        raise RequestError(msg)
    if url.username is not None or url.password is not None:
        msg = "media_origin_invalid"
        raise RequestError(msg)
    if url.port not in (None, 443):
        msg = "media_origin_invalid"
        raise RequestError(msg)


def validate_manifest(payload: bytes) -> None:
    """Prevent generated manifests from redirecting the bridge authentication."""
    tree = ET.fromstring(payload)  # noqa: S314 - bounded hash-verified upstream XML
    if tree.tag.split("}")[-1] != "MPD":
        msg = "manifest_structure_invalid"
        raise RequestError(msg)
    for element in tree.iter():
        name = element.tag.split("}")[-1]
        if name not in MANIFEST_ELEMENTS:
            msg = "manifest_structure_invalid"
            raise RequestError(msg)
        if any(
            key.startswith("{http://www.w3.org/1999/xlink}") for key in element.attrib
        ):
            msg = "manifest_link_invalid"
            raise RequestError(msg)
        if name == "BaseURL":
            validate_media_origin(element.text)


def load_request(state: Path, query: str) -> tuple[str, dict[str, Any]]:
    """Read one verified, current request without network extraction."""
    request_id = parse_request_id(query)
    require_current(state, request_id)
    directory = state / "requests" / request_id
    descriptor = load_descriptor(directory, request_id)
    payload = verified_payload(
        directory / "manifest.mpd", descriptor.get("manifest_sha256")
    )
    validate_manifest(payload)
    return request_id, descriptor


def resolve_item(state: Path, query: str, handle: int) -> None:
    """Set maintained InputStream Adaptive properties for one prepared MPD."""
    request_id, descriptor = load_request(state, query)
    item = xbmcgui.ListItem(
        label=str(descriptor.get("title") or "YouTube"), path=descriptor["file"]
    )
    item.getVideoInfoTag().setTitle(str(descriptor.get("title") or "YouTube"))
    item.setProperty("IsPlayable", "true")
    item.setProperty("inputstream", "inputstream.adaptive")
    item.setProperty("inputstream.adaptive.manifest_type", "mpd")
    item.setProperty("ha.youtube.request_id", request_id)
    item.setProperty("ha.youtube.video_id", descriptor["video_id"])
    item.setMimeType("application/dash+xml")
    item.setContentLookup(False)
    headers = descriptor.get("headers", {})
    allowed = {"user-agent", "accept", "accept-language", "referer", "origin"}
    if not isinstance(headers, dict) or any(
        str(key).lower() not in allowed
        or not isinstance(value, str)
        or "\r" in value
        or "\n" in value
        for key, value in headers.items()
    ):
        msg = "headers_invalid"
        raise RequestError(msg)
    encoded = urlencode(headers)
    if encoded:
        item.setProperty("inputstream.adaptive.stream_headers", encoded)
    token_path = state.parent / "token"
    if token_path.is_symlink() or not token_path.is_file():
        msg = "manifest_auth_missing"
        raise RequestError(msg)
    with token_path.open("r") as stream:
        token = stream.read(MAX_TOKEN_BYTES + 1).strip()
    if not token or len(token) > MAX_TOKEN_BYTES or "\r" in token or "\n" in token:
        msg = "manifest_auth_invalid"
        raise RequestError(msg)
    item.setProperty(
        "inputstream.adaptive.manifest_headers", urlencode({"X-HA-Host-Token": token})
    )
    # No blocking I/O, network extraction, or dialogs after this generation check.
    require_current(state, request_id)
    xbmcplugin.setResolvedUrl(handle, True, item)  # noqa: FBT003 - Kodi positional API contract


def main() -> None:
    """Write bounded private automatic logs without signed stream URLs."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    fd, path = tempfile.mkstemp(
        prefix=f"{LOG_PREFIX}{stamp}-", suffix=".log", dir="/tmp"
    )
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as log:

        def record(stage: Any) -> None:
            line = f"{datetime.now(UTC).isoformat()} {stage} log={path}"
            print(line, file=log, flush=True)
            xbmc.log(line, xbmc.LOGINFO)

        record("starting")
        try:
            prune_logs()
        except OSError as exc:
            record(f"log_cleanup_failed type={type(exc).__name__}")
        try:
            handle = int(sys.argv[1])
            home = Path(xbmcvfs.translatePath("special://home/")).parent
            state = home / ".config" / "ha-host-actions" / "youtube"
            resolve_item(state, sys.argv[2], handle)
            record("completed rc=0")
        except Exception as exc:  # noqa: BLE001 - sanitize failures at the entry point
            code = str(exc) if isinstance(exc, RequestError) else type(exc).__name__
            record(f"completed rc=1 code={code}")
            if len(sys.argv) > 1 and sys.argv[1].lstrip("-").isdigit():
                xbmcplugin.setResolvedUrl(int(sys.argv[1]), False, xbmcgui.ListItem())  # noqa: FBT003 - Kodi positional API contract


if __name__ == "__main__":
    main()
