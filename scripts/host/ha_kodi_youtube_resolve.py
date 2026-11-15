"""
Prepare a private HD YouTube manifest using installed upstream runtimes.

The controller owns current-request.json and the process group. This helper never
changes that generation, starts Kodi, or downloads media. The installed,
hash-pinned SmartTube modules supply public metadata and DASH construction.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import signal
import stat
import sys
import tempfile
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from types import FrameType, ModuleType
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

MAX_AUDIO_CHANNELS = 2
MIN_VIDEO_HEIGHT = 720
MAX_VIDEO_HEIGHT = 1080
MAX_VIDEO_WIDTH = 1920
MAX_VIDEO_FPS = 60
DEADLINE_SECONDS = 75
MANIFEST_BASE_URL = "http://172.20.0.1:8787/kodi/youtube/manifest"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
PRIVATE_FILE_MODE = 0o600
LOG_ACTIVE_GRACE_SECONDS = 120
LOG_DIRECTORY = Path("/tmp")  # noqa: S108 - private owned log directory
MAX_DESCRIPTOR_BYTES = 64 * 1024
RETAIN_REQUESTS = 32
RETENTION_SECONDS = 24 * 60 * 60
VIDEO_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")
SMARTTUBE_VERSION = "0.5.3"
SMARTTUBE_COMMIT = "97520cdb1c37ad78a3c3346b867bfed68c12de2a"
# GPL-2.0-only upstream code is imported unchanged from the installed addon.
SMARTTUBE_FILES = {
    "innertube.py": "0ca6d70df0742b8df3ea8e9c1b16415413e4bcf35a9052eb9c3a5233d0107987",
    "dash.py": "8b9e8b73e4c2283ecd3d4f3d55def29a0224802ccee2caec5345abadf680f1ab",
}
MAX_UPSTREAM_SOURCE_BYTES = 128 * 1024
MAX_PLAYER_BYTES = 8 * 1024 * 1024
PLAYER_TIMEOUT_SECONDS = 8
OWNER_NAME = "ha-kodi-youtube-resolve"
LOG_PREFIX = OWNER_NAME + "-"
LOG_NAME = re.compile(re.escape(LOG_PREFIX) + r"[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9_-]+\.log")
OWNED_FILES = {
    "owner.json",
    "manifest.mpd",
    "descriptor.json",
    ".owner.tmp",
    ".manifest.tmp",
    ".descriptor.tmp",
}


class ResolveError(Exception):
    """A safe, fixed error code with no provider response or signed URL."""


class SafeLog:
    """Write safe stages to stderr and a private per-run log automatically."""

    def __init__(self) -> None:
        """Open a private per-run log before resolver work."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        fd, path = tempfile.mkstemp(
            prefix=f"ha-kodi-youtube-resolve-{stamp}-", suffix=".log", dir="/tmp"
        )
        os.fchmod(fd, 0o600)
        self.path = path
        self.stream = os.fdopen(fd, "w")
        self.started = time.monotonic()
        self.write(f"starting log={path}")
        try:
            prune_logs()
        except OSError as exc:
            self.write(f"log_cleanup_failed type={type(exc).__name__}")

    def write(self, message: str) -> None:
        """Write a sanitized stage to the terminal and private file."""
        line = f"{datetime.now(UTC).isoformat()} {message}"
        print(line, file=sys.stderr, flush=True)  # noqa: T201 - required CLI or CI evidence output
        print(line, file=self.stream, flush=True)

    def finish(self, code: int) -> None:
        """Record the final return code and close the log."""
        self.write(
            f"completed rc={code} elapsed={time.monotonic() - self.started:.1f}s "
            f"log={self.path}"
        )
        self.stream.close()


def owned_log_mtime(path: Path) -> float | None:
    """Recognize a private regular log by filename, user, and startup marker."""
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
    """Bound only this helper's own old logs, preserving active and unknown files."""
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


def canonical_request_id(value: Any) -> str:
    """Accept one canonical UUID, never a caller-supplied file path."""
    try:
        if str(UUID(value)) != value:
            raise ValueError  # noqa: TRY301 - normalize every malformed UUID
    except (ValueError, TypeError, AttributeError) as exc:
        msg = "invalid_request_id"
        raise ResolveError(msg) from exc
    return value


def read_json(path: Path, limit: int) -> dict[str, Any]:
    """Read only a bounded, regular, private task file."""
    if path.is_symlink() or not path.is_file():
        msg = "state_missing"
        raise ResolveError(msg)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        msg = "state_oversize"
        raise ResolveError(msg)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        msg = "state_invalid"
        raise ResolveError(msg) from exc
    if not isinstance(value, dict):
        msg = "state_invalid"
        raise ResolveError(msg)
    return value


def require_current(state: Path, request_id: str) -> None:
    """Refuse publication when the controller has cancelled this attempt."""
    value = read_json(state / "current-request.json", 4096)
    if value.get("request_id") != request_id:
        msg = "request_cancelled"
        raise ResolveError(msg)


def private_directory(path: Path) -> None:
    """Create task-owned private directories without following a symlink."""
    if path.is_symlink():
        msg = "state_path_invalid"
        raise ResolveError(msg)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def atomic_private_write(path: Path, payload: bytes, temporary_name: str) -> None:
    """Publish a complete task file with private permissions."""
    temporary = path.parent / temporary_name
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def only_owned_files(directory: Path) -> bool:
    """Keep directories containing any unknown name, subdirectory, or symlink."""
    return all(
        item.name in OWNED_FILES and item.is_file() and not item.is_symlink()
        for item in directory.iterdir()
    )


def protected_request_ids(state: Path) -> set[str] | None:
    """Read durable playback references, or refuse cleanup when state is uncertain."""
    try:
        generation = read_json(state / "current-request.json", 4096)
        selection = read_json(
            state.parent / "fireplace-selection.json", MAX_DESCRIPTOR_BYTES
        )
        if type(selection.get("version")) is not int or selection["version"] != 1:
            return None
        protected = {canonical_request_id(generation["request_id"])}
        for role in ("active", "pending"):
            identity = selection[role]
            if identity is None:
                continue
            if not isinstance(identity, dict):
                return None
            protected.add(canonical_request_id(identity.get("attempt_id")))
    except (KeyError, OSError, ResolveError):
        return None
    return protected


def owned_request_mtime(directory: Path, protected: set[str]) -> float | None:
    """Recognize owned preparations that no durable playback state protects."""
    if directory.is_symlink() or not directory.is_dir() or directory.name in protected:
        return None
    try:
        canonical_request_id(directory.name)
        owner = read_json(directory / "owner.json", 4096)
        if owner.get("version") != 1 or owner.get("owner") != OWNER_NAME:
            return None
        if owner.get("request_id") != directory.name or not only_owned_files(directory):
            return None
        return directory.stat().st_mtime
    except (OSError, ResolveError):
        return None


def remove_owned_files(directory: Path) -> None:
    """Remove only the fixed task filenames from one already owned directory."""
    for name in OWNED_FILES:
        (directory / name).unlink(missing_ok=True)
    directory.rmdir()


def remove_obsolete_request(state: Path, directory: Path) -> None:
    """Recheck durable playback references and ownership immediately before deletion."""
    protected = protected_request_ids(state)
    if protected is not None and owned_request_mtime(directory, protected) is not None:
        remove_owned_files(directory)


def cleanup_obsolete(state: Path, now: float | None) -> None:
    """Bound obsolete storage while preserving durable playback and unknown files."""
    requests = state / "requests"
    if not requests.is_dir() or requests.is_symlink():
        return
    protected = protected_request_ids(state)
    if protected is None:
        return
    owned = []
    for directory in requests.iterdir():
        modified = owned_request_mtime(directory, protected)
        if modified is not None:
            owned.append((modified, directory))
    for index, (modified, directory) in enumerate(sorted(owned, reverse=True)):
        if index >= RETAIN_REQUESTS or now - modified >= RETENTION_SECONDS:
            remove_obsolete_request(state, directory)


def load_pinned_module(directory: Path, filename: str, digest: str) -> ModuleType:
    """Import only the exact reviewed SmartTube release module."""
    path = directory / filename
    if path.is_symlink() or not path.is_file():
        msg = "upstream_source_missing"
        raise ResolveError(msg)
    with path.open("rb") as stream:
        payload = stream.read(MAX_UPSTREAM_SOURCE_BYTES + 1)
    if hashlib.sha256(payload).hexdigest() != digest:
        msg = "upstream_source_mismatch"
        raise ResolveError(msg)
    name = "ha_youtube_smarttube_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        msg = "upstream_source_missing"
        raise ResolveError(msg)
    module = importlib.util.module_from_spec(spec)
    # Execute the verified buffer, never a reopened source or cached bytecode.
    code = compile(payload, str(path), "exec", dont_inherit=True)
    exec(code, module.__dict__)  # noqa: S102 - exact hash-pinned upstream source
    return module


def smarttube_modules(home: Path) -> tuple[ModuleType, ...]:
    """Load maintained pure modules without Kodi, yt-dlp, or a JS subprocess."""
    directory = home / ".kodi/addons/plugin.video.smarttube/resources/lib"
    return tuple(
        load_pinned_module(directory, filename, digest)
        for filename, digest in SMARTTUBE_FILES.items()
    )


def bounded_player_response(
    video_id: str, url: str, data: bytes, headers: Any, timeout: float
) -> bytes:
    """Bound upstream HTTP and verify identity before its parser drops videoId."""
    request = Request(url, data=data, headers=headers, method="POST")  # noqa: S310 - pinned client owns HTTPS endpoints
    with urlopen(request, timeout=min(timeout, PLAYER_TIMEOUT_SECONDS)) as response:  # noqa: S310 - pinned client owns HTTPS endpoints
        raw = response.read(MAX_PLAYER_BYTES + 1)
    if len(raw) > MAX_PLAYER_BYTES:
        msg = "player_response_oversize"
        raise ResolveError(msg)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        msg = "player_response_invalid"
        raise ResolveError(msg) from exc
    if not isinstance(value, dict):
        msg = "player_response_invalid"
        raise ResolveError(msg)
    status = value.get("playabilityStatus")
    if isinstance(status, dict) and status.get("status") == "OK":
        details = value.get("videoDetails")
        if not isinstance(details, dict) or details.get("videoId") != video_id:
            msg = "unexpected_video"
            raise ResolveError(msg)
    return raw


def public_media_url(value: Any) -> bool:
    """Accept only the same HTTPS YouTube media origins as the Kodi adapter."""
    try:
        url = urlsplit(value)
        return (
            url.scheme == "https"
            and (url.hostname or "").endswith(".googlevideo.com")
            and url.username is None
            and url.password is None
            and url.port in (None, 443)
        )
    except (TypeError, ValueError):
        return False


def allowed_smarttube_format(item: dict[str, Any], builder: ModuleType) -> bool:
    """Apply projector limits after upstream validates all DASH fields."""
    if not builder._usable(item) or not public_media_url(item["url"]):  # noqa: SLF001 - reviewed pinned upstream API
        return False
    mime, codec = builder._mime(item)  # noqa: SLF001 - reviewed pinned upstream API
    if mime == "audio/mp4":
        return (
            codec.startswith("mp4a.")
            and 0 < (item.get("audioChannels") or 0) <= MAX_AUDIO_CHANNELS
        )
    return (
        mime == "video/mp4"
        and codec.startswith("avc1.")
        and MIN_VIDEO_HEIGHT <= (item.get("height") or 0) <= MAX_VIDEO_HEIGHT
        and 0 < (item.get("width") or 0) <= MAX_VIDEO_WIDTH
        and 0 < (item.get("fps") or 0) <= MAX_VIDEO_FPS
    )


def select_smarttube_formats(
    formats: list[dict[str, Any]], builder: ModuleType
) -> list[dict[str, Any]]:
    """Keep H264 HD and non-DRC AAC; upstream owns format parsing and ranges."""
    return [item for item in formats if allowed_smarttube_format(item, builder)]


def build_smarttube_response(
    info: dict[str, Any], client: str, inner: ModuleType, builder: ModuleType
) -> dict[str, Any] | None:
    """Build one upstream MPD containing both permitted video and AAC audio."""
    if not info.get("playable") or info.get("is_live"):
        return None
    formats = select_smarttube_formats(info.get("adaptive") or [], builder)
    videos = [item for item in formats if item.get("height")]
    audios = [item for item in formats if not item.get("height")]
    if not videos or not audios:
        return None
    video = max(videos, key=lambda item: (item["height"], item["width"], item["fps"]))
    manifest = builder.manifest([video, *audios], hdr=False)
    if not isinstance(manifest, str) or not manifest:
        return None
    payload = manifest.encode("utf-8")
    if len(payload) > MAX_MANIFEST_BYTES:
        msg = "manifest_invalid"
        raise ResolveError(msg)
    return {
        "payload": payload,
        "title": str(info.get("title") or "YouTube")[:500],
        "width": video["width"],
        "height": video["height"],
        "headers": safe_headers(inner.CLIENTS[client]["headers"]),
    }


def prepare_smarttube(video_id: str, home: Path, log: SafeLog) -> dict[str, Any]:
    """Use the maintained public client and MPD builder within caller cancellation."""
    inner, builder = smarttube_modules(home)
    log.write(f"runtime smarttube={SMARTTUBE_VERSION}")
    opener = partial(bounded_player_response, video_id)
    for info, client in inner.player_responses(video_id, opener=opener):
        prepared = build_smarttube_response(info, client, inner, builder)
        if prepared is not None:
            log.write(
                "upstream_dash_ready "
                f"width={prepared['width']} height={prepared['height']}"
            )
            return prepared
    msg = "hd_audio_video_unavailable"
    raise ResolveError(msg)


def safe_headers(headers: Any) -> dict[str, str]:
    """Persist only ordinary playback headers; never cookies or authorization."""
    allowed = {"user-agent", "accept", "accept-language", "referer", "origin"}
    return {
        str(key): str(value)
        for key, value in (headers or {}).items()
        if str(key).lower() in allowed
        and "\r" not in str(value)
        and "\n" not in str(value)
    }


def resolve(video_id: str, request_id: str, home: Path, log: SafeLog) -> dict[str, str]:
    """Publish one private HD result only for the controller's current attempt."""
    if not VIDEO_ID_PATTERN.fullmatch(video_id):
        msg = "invalid_video_id"
        raise ResolveError(msg)
    canonical_request_id(request_id)
    state = home / ".config" / "ha-host-actions" / "youtube"
    require_current(state, request_id)
    private_directory(state)
    private_directory(state / "requests")
    cleanup_obsolete(state, time.time())
    request_dir = state / "requests" / request_id
    request_dir.mkdir(mode=0o700, exist_ok=False)
    manifest_path = request_dir / "manifest.mpd"
    manifest_url = MANIFEST_BASE_URL + "?" + urlencode({"request_id": request_id})
    try:
        # Durable ownership precedes metadata, builder work, and signed data.
        owner = {
            "version": 1,
            "owner": OWNER_NAME,
            "request_id": request_id,
            "created_at": time.time(),
        }
        atomic_private_write(
            request_dir / "owner.json", json.dumps(owner).encode(), ".owner.tmp"
        )
        prepared = prepare_smarttube(video_id, home, log)
        require_current(state, request_id)
        payload = prepared["payload"]
        descriptor = {
            "version": 1,
            "request_id": request_id,
            "video_id": video_id,
            "file": manifest_url,
            "manifest_file": str(manifest_path),
            "title": prepared["title"],
            "created_at": time.time(),
            "manifest_sha256": hashlib.sha256(payload).hexdigest(),
            "headers": prepared["headers"],
            "width": prepared["width"],
            "height": prepared["height"],
        }
        atomic_private_write(manifest_path, payload, ".manifest.tmp")
        require_current(state, request_id)
        atomic_private_write(
            request_dir / "descriptor.json",
            json.dumps(descriptor, ensure_ascii=False).encode(),
            ".descriptor.tmp",
        )
        require_current(state, request_id)
        log.write("manifest_ready audio=true video=true")
        return {
            "file": manifest_url,
            "open_file": "plugin://plugin.video.hayoutube/?"
            + urlencode({"request_id": request_id}),
            "title": prepared["title"],
        }
    except BaseException:
        remove_owned_files(request_dir)
        raise


def cancel(_signum: int, _frame: FrameType | None) -> NoReturn:
    """Interrupt the helper while its caller reaps the entire process group."""
    msg = "cancelled_or_deadline"
    raise ResolveError(msg)


def main() -> int:
    """Run one bounded resolver with machine-readable stdout and safe logs."""
    log = SafeLog()
    code = 1
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--video-id", required=True)
        parser.add_argument("--request-id", required=True)
        args = parser.parse_args()
        signal.signal(signal.SIGTERM, cancel)
        signal.signal(signal.SIGALRM, cancel)
        signal.alarm(DEADLINE_SECONDS)
        result = resolve(args.video_id, args.request_id, Path.home(), log)
        print(json.dumps(result, ensure_ascii=False), flush=True)  # noqa: T201 - required CLI or CI evidence output
        code = 0
    except ResolveError as exc:
        log.write(f"failed code={exc}")
    except Exception as exc:  # noqa: BLE001 - sanitize failures at the entry point
        # Upstream exception text may embed a signed stream URL.
        log.write(f"failed code=upstream_or_io_error type={type(exc).__name__}")
    finally:
        signal.alarm(0)
        log.finish(code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
