"""Read only the current prepared MPD for the authenticated host bridge."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from pathlib import Path

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_DESCRIPTOR_BYTES = 64 * 1024


class ManifestError(Exception):
    """A fixed public error code without provider URLs or local paths."""


def _json(path: Path, limit: int) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        error_message = "manifest_unavailable"
        raise ManifestError(error_message)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        error_message = "manifest_unavailable"
        raise ManifestError(error_message)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        error_message = "manifest_unavailable"
        raise ManifestError(error_message) from exc
    if not isinstance(value, dict):
        error_message = "manifest_unavailable"
        raise ManifestError(error_message)
    return value


def _require_canonical_id(request_id: str) -> None:
    if str(UUID(request_id)) != request_id:
        raise ValueError


def read_manifest(state: Path, query: dict[str, list[str]]) -> bytes:
    """No caller-controlled filename, redirects, or upstream HTTP requests."""
    if set(query) != {"request_id"} or len(query["request_id"]) != 1:
        error_message = "invalid_request"
        raise ManifestError(error_message)
    request_id = query["request_id"][0]
    try:
        _require_canonical_id(request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        error_message = "invalid_request"
        raise ManifestError(error_message) from exc
    generation = state / "current-request.json"
    if _json(generation, 4096).get("request_id") != request_id:
        error_message = "request_cancelled"
        raise ManifestError(error_message)
    directory = state / "requests" / request_id
    if (
        state.is_symlink()
        or (state / "requests").is_symlink()
        or directory.is_symlink()
    ):
        error_message = "manifest_unavailable"
        raise ManifestError(error_message)
    descriptor = _json(directory / "descriptor.json", MAX_DESCRIPTOR_BYTES)
    manifest = directory / "manifest.mpd"
    if (
        descriptor.get("version") != 1
        or descriptor.get("request_id") != request_id
        or descriptor.get("manifest_file", descriptor.get("file")) != str(manifest)
    ):
        error_message = "manifest_unavailable"
        raise ManifestError(error_message)
    if manifest.is_symlink() or not manifest.is_file():
        error_message = "manifest_unavailable"
        raise ManifestError(error_message)
    with manifest.open("rb") as stream:
        payload = stream.read(MAX_MANIFEST_BYTES + 1)
    expected = descriptor.get("manifest_sha256")
    if (
        not 0 < len(payload) <= MAX_MANIFEST_BYTES
        or not isinstance(expected, str)
        or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected)
    ):
        error_message = "manifest_unavailable"
        raise ManifestError(error_message)
    if _json(generation, 4096).get("request_id") != request_id:
        error_message = "request_cancelled"
        raise ManifestError(error_message)
    return payload
