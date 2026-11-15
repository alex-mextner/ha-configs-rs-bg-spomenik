"""Open only the current prepared request; never perform network extraction."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID
import xml.etree.ElementTree as ET

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs

MANIFEST_BASE_URL = "http://172.20.0.1:8787/kodi/youtube/manifest"
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


def owned_log_mtime(path):
    """Recognize private adapter logs without trusting a filename alone."""
    if not LOG_NAME.fullmatch(path.name):
        return None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            return None
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            return None
        with path.open("r") as stream:
            first_line = stream.readline(512)
        if " starting log=" + str(path) not in first_line:
            return None
        return metadata.st_mtime
    except (OSError, UnicodeError):
        return None


def prune_logs(directory=Path("/tmp"), now=None, keep=64):
    """Keep recent logs and prune only owned logs beyond count or age limits."""
    now = time.time() if now is None else now
    owned = []
    for path in directory.glob(LOG_PREFIX + "*.log"):
        modified = owned_log_mtime(path)
        if modified is not None:
            owned.append((modified, path))
    for index, (modified, path) in enumerate(sorted(owned, reverse=True)):
        if now - modified < 120:
            continue
        if index >= keep or now - modified > 7 * 86400:
            path.unlink(missing_ok=True)


def read_json(path, limit):
    """Read a bounded task file without accepting symlinks."""
    if path.is_symlink() or not path.is_file():
        raise RequestError("state_missing")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise RequestError("state_oversize")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RequestError("state_invalid") from exc
    if not isinstance(value, dict):
        raise RequestError("state_invalid")
    return value


def require_current(state, request_id):
    """Check controller generation immediately before resolving to Kodi."""
    if read_json(state / "current-request.json", 4096).get("request_id") != request_id:
        raise RequestError("request_cancelled")


def parse_request_id(query):
    """Accept exactly one canonical UUID from the plugin query."""
    values = parse_qs(query.lstrip("?"), strict_parsing=True)
    if set(values) != {"request_id"} or len(values["request_id"]) != 1:
        raise RequestError("invalid_request")
    request_id = values["request_id"][0]
    try:
        if str(UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise RequestError("invalid_request") from exc
    return request_id


def load_descriptor(directory, request_id):
    """Require a descriptor bound to this exact request and manifest endpoint."""
    if directory.is_symlink():
        raise RequestError("state_path_invalid")
    descriptor = read_json(directory / "descriptor.json", MAX_DESCRIPTOR_BYTES)
    expected = {
        "version": 1,
        "request_id": request_id,
        "file": MANIFEST_BASE_URL + "?" + urlencode({"request_id": request_id}),
        "manifest_file": str(directory / "manifest.mpd"),
    }
    if any(descriptor.get(key) != value for key, value in expected.items()):
        raise RequestError("descriptor_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(descriptor.get("video_id", ""))):
        raise RequestError("descriptor_invalid")
    return descriptor


def verified_payload(manifest, expected_digest):
    """Check the private MPD's size and digest before adding authentication."""
    if manifest.is_symlink() or not manifest.is_file():
        raise RequestError("manifest_missing")
    with manifest.open("rb") as stream:
        payload = stream.read(MAX_MANIFEST_BYTES + 1)
    if not 0 < len(payload) <= MAX_MANIFEST_BYTES:
        raise RequestError("manifest_changed")
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        raise RequestError("manifest_changed")
    return payload


def validate_media_origin(value):
    """Permit only ordinary public YouTube HTTPS media origins."""
    url = urlsplit(value or "")
    if url.scheme != "https" or not (url.hostname or "").endswith(".googlevideo.com"):
        raise RequestError("media_origin_invalid")
    if url.username is not None or url.password is not None:
        raise RequestError("media_origin_invalid")
    if url.port not in (None, 443):
        raise RequestError("media_origin_invalid")


def validate_manifest(payload):
    """Prevent generated manifests from redirecting the bridge authentication."""
    tree = ET.fromstring(payload)
    if tree.tag.split("}")[-1] != "MPD":
        raise RequestError("manifest_structure_invalid")
    for element in tree.iter():
        name = element.tag.split("}")[-1]
        if name not in MANIFEST_ELEMENTS:
            raise RequestError("manifest_structure_invalid")
        if any(
            key.startswith("{http://www.w3.org/1999/xlink}") for key in element.attrib
        ):
            raise RequestError("manifest_link_invalid")
        if name == "BaseURL":
            validate_media_origin(element.text)


def load_request(state, query):
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


def resolve_item(state, query, handle):
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
        raise RequestError("headers_invalid")
    encoded = urlencode(headers)
    if encoded:
        item.setProperty("inputstream.adaptive.stream_headers", encoded)
    token_path = state.parent / "token"
    if token_path.is_symlink() or not token_path.is_file():
        raise RequestError("manifest_auth_missing")
    with token_path.open("r") as stream:
        token = stream.read(4097).strip()
    if not token or len(token) > 4096 or "\r" in token or "\n" in token:
        raise RequestError("manifest_auth_invalid")
    item.setProperty(
        "inputstream.adaptive.manifest_headers", urlencode({"X-HA-Host-Token": token})
    )
    # No blocking I/O, network extraction, or dialogs after this generation check.
    require_current(state, request_id)
    xbmcplugin.setResolvedUrl(handle, True, item)


def main():
    """Write bounded private automatic logs without signed stream URLs."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fd, path = tempfile.mkstemp(
        prefix=f"{LOG_PREFIX}{stamp}-", suffix=".log", dir="/tmp"
    )
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as log:

        def record(stage):
            line = f"{datetime.now(timezone.utc).isoformat()} {stage} log={path}"
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
        except Exception as exc:
            code = str(exc) if isinstance(exc, RequestError) else type(exc).__name__
            record(f"completed rc=1 code={code}")
            if len(sys.argv) > 1 and sys.argv[1].lstrip("-").isdigit():
                xbmcplugin.setResolvedUrl(int(sys.argv[1]), False, xbmcgui.ListItem())


if __name__ == "__main__":
    main()
