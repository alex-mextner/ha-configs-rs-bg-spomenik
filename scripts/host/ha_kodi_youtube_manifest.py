"""Read only the current prepared MPD for the authenticated host bridge."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from uuid import UUID

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_DESCRIPTOR_BYTES = 64 * 1024


class ManifestError(Exception):
    """A fixed public error code without provider URLs or local paths."""


def _json(path, limit):
    if path.is_symlink() or not path.is_file():
        raise ManifestError("manifest_unavailable")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ManifestError("manifest_unavailable")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ManifestError("manifest_unavailable") from exc
    if not isinstance(value, dict):
        raise ManifestError("manifest_unavailable")
    return value


def read_manifest(state: Path, query: dict[str, list[str]]) -> bytes:
    """No caller-controlled filename, redirects, or upstream HTTP requests."""
    if set(query) != {"request_id"} or len(query["request_id"]) != 1:
        raise ManifestError("invalid_request")
    request_id = query["request_id"][0]
    try:
        if str(UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ManifestError("invalid_request") from exc
    generation = state / "current-request.json"
    if _json(generation, 4096).get("request_id") != request_id:
        raise ManifestError("request_cancelled")
    directory = state / "requests" / request_id
    if (
        state.is_symlink()
        or (state / "requests").is_symlink()
        or directory.is_symlink()
    ):
        raise ManifestError("manifest_unavailable")
    descriptor = _json(directory / "descriptor.json", MAX_DESCRIPTOR_BYTES)
    manifest = directory / "manifest.mpd"
    if (
        descriptor.get("version") != 1
        or descriptor.get("request_id") != request_id
        or descriptor.get("manifest_file", descriptor.get("file")) != str(manifest)
    ):
        raise ManifestError("manifest_unavailable")
    if manifest.is_symlink() or not manifest.is_file():
        raise ManifestError("manifest_unavailable")
    with manifest.open("rb") as stream:
        payload = stream.read(MAX_MANIFEST_BYTES + 1)
    expected = descriptor.get("manifest_sha256")
    if (
        not 0 < len(payload) <= MAX_MANIFEST_BYTES
        or not isinstance(expected, str)
        or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected)
    ):
        raise ManifestError("manifest_unavailable")
    if _json(generation, 4096).get("request_id") != request_id:
        raise ManifestError("request_cancelled")
    return payload
