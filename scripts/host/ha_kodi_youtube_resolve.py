#!/usr/bin/env python3
"""Prepare a private HD YouTube manifest using installed upstream runtimes.

The controller owns current-request.json and the process group. This helper never
changes that generation, starts Kodi, or downloads media. SendToKodi supplies
DASH construction; its installed yt-dlp and Deno supply public extraction.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import tempfile
import time
from urllib.parse import urlencode
from uuid import UUID

DEADLINE_SECONDS = 75
MANIFEST_BASE_URL = "http://172.20.0.1:8787/kodi/youtube/manifest"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_DESCRIPTOR_BYTES = 64 * 1024
RETAIN_REQUESTS = 32
RETENTION_SECONDS = 24 * 60 * 60
VIDEO_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")
VERSION_PATTERN = re.compile(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}")
DENO_VERSION_PATTERN = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
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

    def __init__(self):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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

    def write(self, message):
        line = f"{datetime.now(timezone.utc).isoformat()} {message}"
        print(line, file=sys.stderr, flush=True)
        print(line, file=self.stream, flush=True)

    def finish(self, code):
        self.write(
            f"completed rc={code} elapsed={time.monotonic() - self.started:.1f}s log={self.path}"
        )
        self.stream.close()


def owned_log_mtime(path):
    """Recognize a private regular log by filename, user, and startup marker."""
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
    """Bound only this helper's own old logs, preserving active and unknown files."""
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


class QuietExtractorLog:
    """Keep upstream messages containing signed URLs out of diagnostic logs."""

    def debug(self, _message):
        pass

    def warning(self, _message):
        pass

    def error(self, _message):
        pass


def canonical_request_id(value):
    """Accept one canonical UUID, never a caller-supplied file path."""
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ResolveError("invalid_request_id") from exc
    return value


def read_json(path, limit):
    """Read only a bounded, regular, private task file."""
    if path.is_symlink() or not path.is_file():
        raise ResolveError("state_missing")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ResolveError("state_oversize")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ResolveError("state_invalid") from exc
    if not isinstance(value, dict):
        raise ResolveError("state_invalid")
    return value


def require_current(state, request_id):
    """Refuse publication when the controller has cancelled this attempt."""
    value = read_json(state / "current-request.json", 4096)
    if value.get("request_id") != request_id:
        raise ResolveError("request_cancelled")


def private_directory(path):
    """Create task-owned private directories without following a symlink."""
    if path.is_symlink():
        raise ResolveError("state_path_invalid")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def atomic_private_write(path, payload, temporary_name):
    """Publish a complete task file with private permissions."""
    temporary = path.parent / temporary_name
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def only_owned_files(directory):
    """Keep directories containing any unknown name, subdirectory, or symlink."""
    return all(
        item.name in OWNED_FILES and item.is_file() and not item.is_symlink()
        for item in directory.iterdir()
    )


def protected_request_ids(state):
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
        return protected
    except (KeyError, OSError, ResolveError):
        return None


def owned_request_mtime(directory, protected):
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


def remove_owned_files(directory):
    """Remove only the fixed task filenames from one already owned directory."""
    for name in OWNED_FILES:
        (directory / name).unlink(missing_ok=True)
    directory.rmdir()


def remove_obsolete_request(state, directory):
    """Recheck durable playback references and ownership immediately before deletion."""
    protected = protected_request_ids(state)
    if protected is not None and owned_request_mtime(directory, protected) is not None:
        remove_owned_files(directory)


def cleanup_obsolete(state, now):
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


def runtime_modules(home):
    """Load supported runtimes and pure builder modules from installed addons."""
    addons = home / ".kodi" / "addons"
    runtime = home / ".kodi" / "userdata" / "addon_data" / "plugin.video.sendtokodi"
    ytdlp_version = (runtime / "ytdlp" / "ytdlp_version.txt").read_text().strip()
    deno_version = (runtime / "deno" / "deno_version.txt").read_text().strip()
    if not VERSION_PATTERN.fullmatch(
        ytdlp_version
    ) or not DENO_VERSION_PATTERN.fullmatch(deno_version):
        raise ResolveError("runtime_version_invalid")
    ytdlp_root = runtime / "ytdlp" / "versions" / ytdlp_version
    deno = runtime / "deno" / "versions" / deno_version / "deno"
    addon = addons / "plugin.video.sendtokodi"
    if not (ytdlp_root / "yt_dlp" / "__init__.py").is_file() or not deno.is_file():
        raise ResolveError("runtime_missing")
    if not (addon / "core" / "dash_builder.py").is_file():
        raise ResolveError("builder_missing")
    # Normal Python addon dependency directories, not a system-library override.
    for module in ("requests", "urllib3", "certifi", "chardet", "idna"):
        sys.path.insert(0, str(addons / f"script.module.{module}" / "lib"))
    sys.path.insert(0, str(addon))
    sys.path.insert(0, str(ytdlp_root))
    return (
        importlib.import_module("yt_dlp"),
        importlib.import_module("core.dash_builder"),
        importlib.import_module("core.playback_selection"),
        deno,
        ytdlp_version,
        deno_version,
    )


def select_hd_formats(result):
    """Restrict upstream format selection to projector-compatible H264 and AAC."""
    selected = []
    for item in result.get("formats", []):
        vcodec = str(item.get("vcodec", "none"))
        acodec = str(item.get("acodec", "none"))
        video = (
            vcodec.startswith("avc1")
            and acodec == "none"
            and item.get("container") == "mp4_dash"
            and 720 <= (item.get("height") or 0) <= 1080
            and 0 < (item.get("width") or 0) <= 1920
            and 0 < (item.get("fps") or 0) <= 60
        )
        audio = (
            vcodec == "none"
            and acodec.startswith("mp4a")
            and item.get("container") == "m4a_dash"
        )
        if (video or audio) and str(item.get("url", "")).startswith("https://"):
            selected.append(item)
    return selected


def safe_headers(headers):
    """Persist only ordinary playback headers; never cookies or authorization."""
    allowed = {"user-agent", "accept", "accept-language", "referer", "origin"}
    return {
        str(key): str(value)
        for key, value in (headers or {}).items()
        if str(key).lower() in allowed
        and "\r" not in str(value)
        and "\n" not in str(value)
    }


def resolve(video_id, request_id, home, log):
    """Build one private HD result, publishing only for the current generation."""
    if not VIDEO_ID_PATTERN.fullmatch(video_id):
        raise ResolveError("invalid_video_id")
    canonical_request_id(request_id)
    state = home / ".config" / "ha-host-actions" / "youtube"
    require_current(state, request_id)
    private_directory(state)
    private_directory(state / "requests")
    cleanup_obsolete(state, time.time())
    ytdlp, builder, selection, deno, ytdlp_version, deno_version = runtime_modules(home)
    log.write(f"runtime yt_dlp={ytdlp_version} deno={deno_version}")
    options = {
        "quiet": True,
        "no_warnings": True,
        "logger": QuietExtractorLog(),
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 10,
        "retries": 1,
        "extractor_retries": 1,
        "js_runtimes": {"deno": {"path": str(deno)}},
        "remote_components": {"ejs:github"},
        "format": "bestvideo+bestaudio/best",
    }
    log.write("extracting_public_video")
    with ytdlp.YoutubeDL(options) as extractor:
        result = extractor.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False
        )
    require_current(state, request_id)
    if (
        not isinstance(result, dict)
        or result.get("id") != video_id
        or result.get("is_live")
    ):
        raise ResolveError("unexpected_video")
    formats = select_hd_formats(result)
    have_video, have_audio, videos, audios = selection.analyze_formats(formats)
    if not have_video or not have_audio or not videos or not audios:
        raise ResolveError("hd_audio_video_unavailable")
    video = selection.pick_best_dash_video_format(videos, 1920)
    request_dir = state / "requests" / request_id
    request_dir.mkdir(mode=0o700, exist_ok=False)
    manifest_path = request_dir / "manifest.mpd"
    manifest_url = MANIFEST_BASE_URL + "?" + urlencode({"request_id": request_id})
    manifest_payload = None

    def capture_manifest(payload, **_ignored):
        nonlocal manifest_payload
        if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_MANIFEST_BYTES:
            raise ResolveError("manifest_invalid")
        manifest_payload = payload
        return str(manifest_path)

    try:
        # Persist ownership before any builder work or signed manifest data.
        owner = {
            "version": 1,
            "owner": OWNER_NAME,
            "request_id": request_id,
            "created_at": time.time(),
        }
        atomic_private_write(
            request_dir / "owner.json", json.dumps(owner).encode(), ".owner.tmp"
        )
        log.write(
            f"building_upstream_dash width={video['width']} height={video['height']}"
        )
        candidate = selection.build_dash_manifest_candidate(
            result.get("duration"),
            [video],
            audios,
            True,
            True,
            builder.Manifest,
            capture_manifest,
            preferred_video_format=video,
        )
        if candidate.get("url") != str(manifest_path) or manifest_payload is None:
            raise ResolveError("dash_audio_video_unavailable")
        require_current(state, request_id)
        title = str(result.get("title") or "YouTube")[:500]
        descriptor = {
            "version": 1,
            "request_id": request_id,
            "video_id": video_id,
            "file": manifest_url,
            "manifest_file": str(manifest_path),
            "title": title,
            "created_at": time.time(),
            "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
            "headers": safe_headers(
                video.get("http_headers") or result.get("http_headers")
            ),
            "width": video["width"],
            "height": video["height"],
        }
        atomic_private_write(manifest_path, manifest_payload, ".manifest.tmp")
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
            "title": title,
        }
    except BaseException:
        remove_owned_files(request_dir)
        raise


def cancel(_signum, _frame):
    """Interrupt the helper while its caller reaps the entire process group."""
    raise ResolveError("cancelled_or_deadline")


def main():
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
        print(json.dumps(result, ensure_ascii=False), flush=True)
        code = 0
    except ResolveError as exc:
        log.write(f"failed code={exc}")
    except Exception as exc:
        # Upstream exception text may embed a signed stream URL.
        log.write(f"failed code=upstream_or_io_error type={type(exc).__name__}")
    finally:
        signal.alarm(0)
        log.finish(code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
