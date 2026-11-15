"""Filesystem and metadata helpers for the standalone host bridge."""  # noqa: INP001
# Installed beside the standalone executable; the copied bundle is not a package.

from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

_MIN_TITLE_PREFIX = 3
_MIN_SEASON_CANDIDATES = 3
_MIN_UNLABELED_SERIES_CANDIDATES = 6

EPISODE_RE = re.compile(
    r"(?:^|[ ._\-\[\(])(?:s(?P<season>\d{1,2})[ ._\-]*e(?P<episode>\d{1,3})|"
    r"(?P<season_alt>\d{1,2})x(?P<episode_alt>\d{1,3}))(?:[ ._\-\]\)]|$)",
    re.IGNORECASE,
)


SEASON_HINT_RE = re.compile(
    r"(?:^|[ ._\-/\\])(?:s(?:eason)?|сезон)[ ._\-]*(\d{1,2})(?:[ ._\-/\\]|$)",
    re.IGNORECASE,
)


SERIES_HINT_RE = re.compile(
    r"(?:season|series|tv[ ._\-]?show|сезон|сериал|серии|эпизод)", re.IGNORECASE
)


TITLE_CUT_RE = re.compile(
    r"\b(?:s\d{1,2}(?:e\d{1,3})?|season|сезон|1080p|720p|2160p|4320p|"
    r"bluray|bdrip|brrip|web[ ._\-]?dl|webrip|hdtv|hdrip|dvdrip|remux|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|hdr|dv|rus|eng|multi|teamhd|lostfilm)\b",
    re.IGNORECASE,
)


def episode_match(text: str) -> re.Match[str] | None:
    """Find a supported season and episode pattern in the supplied text."""
    return EPISODE_RE.search(text)


def season_number(path: Path) -> int | None:
    """Read the nearest season hint from a local path."""
    for part in reversed(path.parts):
        match = SEASON_HINT_RE.search(part)
        if match:
            return int(match.group(1))
        short = re.search(r"(?:^|[ ._\-])s(\d{1,2})(?:[ ._\-]|$)", part, re.IGNORECASE)
        if short:
            return int(short.group(1))
    return None


def normalized_path(path: Path) -> str:
    """Resolve a local path while retaining the existing absolute fallback."""
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


def path_contains(parent: Path, child: Path) -> bool:
    """Check directory containment using normalized path boundaries."""
    parent_s = normalized_path(parent).rstrip("/") + "/"
    child_s = normalized_path(child)
    return child_s == parent_s.rstrip("/") or child_s.startswith(parent_s)


def trailing_slash(path: Path) -> str:
    """Return a path string with exactly one required trailing separator."""
    return str(path) if str(path).endswith("/") else f"{path}/"


def sanitize_path_part(value: str, fallback: str) -> str:
    """Normalize a library component using the existing bounded policy."""
    text = re.sub(r"[._]+", " ", value).strip()
    text = re.sub(r"[/:\\\0]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text or fallback)[:120].strip(" .") or fallback


def clean_media_title(value: str, fallback: str) -> str:
    """Remove known release hints from a local media title."""
    text = Path(value).stem if "/" in value else value
    text = re.sub(r"[._]+", " ", text)
    cut = TITLE_CUT_RE.search(text)
    if cut and cut.start() >= _MIN_TITLE_PREFIX:
        text = text[: cut.start()]
    text = re.sub(r"\[[^\]]*\]|\([^\)]*\)", " ", text)
    return sanitize_path_part(text, fallback)


def _media_probe(
    root: Path,
    candidates: list[Path],
    payload: dict[str, Any],
    torrent: dict[str, Any] | None,
) -> str:
    """Collect existing filename, path and tracker-label hints."""
    labels = []
    if torrent and isinstance(torrent.get("labels"), list):
        labels = [str(label) for label in torrent.get("labels") or []]

    texts = [
        str(payload.get("name") or ""),
        str(payload.get("title") or ""),
        str(root),
        str(torrent.get("name") or "") if torrent else "",
        " ".join(labels),
    ]
    texts.extend(
        str(path.relative_to(root)) if path_contains(root, path) else str(path)
        for path in candidates
    )
    return "\n".join(text for text in texts if text)


def detect_media_type(
    root: Path,
    candidates: list[Path],
    payload: dict[str, Any] | None = None,
    torrent: dict[str, Any] | None = None,
) -> str:
    """Classify existing files using explicit choice, names and tracker hints."""
    payload = payload or {}
    requested = (
        str(payload.get("media_type") or payload.get("type") or "auto").strip().lower()
    )
    if requested in {"series", "movies"}:
        return requested

    probe = _media_probe(root, candidates, payload, torrent)

    if episode_match(probe) or SERIES_HINT_RE.search(probe):
        return "series"

    if len(candidates) >= _MIN_SEASON_CANDIDATES and any(
        season_number(path) is not None for path in candidates
    ):
        return "series"
    if len(candidates) >= _MIN_UNLABELED_SERIES_CANDIDATES:
        return "series"

    return "movies"


def _file_stat(stats: Any, index: int) -> dict[str, Any]:
    """Retain the missing-stat fallback for malformed or short tracker results."""
    if (
        isinstance(stats, list)
        and index < len(stats)
        and isinstance(stats[index], dict)
    ):
        return stats[index]
    return {}


def _completed_file(
    download_dir: Path,
    file_info: Any,
    file_stat: dict[str, Any],
    *,
    minimum_size: int,
    video_suffixes: set[str],
) -> Path | None:
    """Select one complete, wanted video that still exists on disk."""
    if not isinstance(file_info, dict):
        return None
    relative_name = str(file_info.get("name") or "").strip()
    if not relative_name:
        return None
    suffix = Path(relative_name).suffix.lower()
    if suffix not in video_suffixes or relative_name.lower().endswith(".part"):
        return None

    length = int(file_info.get("length") or 0)
    if length < minimum_size or file_stat.get("wanted") is False:
        return None

    completed = int(
        file_stat.get("bytesCompleted") or file_info.get("bytesCompleted") or 0
    )
    if completed < length:
        return None

    path = download_dir / relative_name
    return path if path.exists() and path.is_file() else None


def completed_video_files(
    torrent: dict[str, Any], *, minimum_size: int, video_suffixes: set[str]
) -> list[Path]:
    """Select complete wanted local videos from tracker file metadata."""
    download_dir = Path(str(torrent.get("downloadDir") or ""))
    files = torrent.get("files") or []
    stats = torrent.get("fileStats") or []
    candidates: list[Path] = []

    if not isinstance(files, list):
        return candidates

    for index, file_info in enumerate(files):
        path = _completed_file(
            download_dir,
            file_info,
            _file_stat(stats, index),
            minimum_size=minimum_size,
            video_suffixes=video_suffixes,
        )
        if path is not None:
            candidates.append(path)

    return candidates


def _ensure_video_source(video: ET.Element, name: str, path: Path) -> bool:
    """Update only the named source and retain unrelated owner XML entries."""
    changed = False
    source = next(
        (
            candidate
            for candidate in video.findall("source")
            if (candidate.findtext("name") or "") == name
        ),
        None,
    )
    if source is None:
        source = ET.SubElement(video, "source")
        ET.SubElement(source, "name").text = name
        ET.SubElement(source, "path", {"pathversion": "1"}).text = trailing_slash(path)
        ET.SubElement(source, "allowsharing").text = "true"
        changed = True
        return True

    path_node = source.find("path")
    wanted_path = trailing_slash(path)
    if path_node is None:
        path_node = ET.SubElement(source, "path", {"pathversion": "1"})
        changed = True
    if path_node.text != wanted_path:
        path_node.text = wanted_path
        changed = True
    return changed


def _write_sources(tree: ET.ElementTree, sources_path: Path) -> None:
    """Write updated XML and retain the first preexisting source backup."""
    sources_path.parent.mkdir(parents=True, exist_ok=True)
    if sources_path.exists():
        backup = sources_path.with_suffix(".xml.ha-host-actions.bak")
        if not backup.exists():
            shutil.copy2(sources_path, backup)
    ET.indent(tree, space="  ")
    tree.write(sources_path, encoding="utf-8", xml_declaration=True)


def ensure_sources_xml(
    userdata_dir: Path, series_dir: Path, movies_dir: Path
) -> dict[str, Any]:
    """Update only managed Kodi XML sources and retain the first backup."""
    series_dir.mkdir(parents=True, exist_ok=True)
    movies_dir.mkdir(parents=True, exist_ok=True)
    sources_path = userdata_dir / "sources.xml"
    changed = False

    if sources_path.exists():
        # Only the owner-managed local Kodi XML file is parsed here.
        tree = ET.parse(sources_path)  # noqa: S314
        root = tree.getroot()
    else:
        root = ET.Element("mediasources")
        tree = ET.ElementTree(root)
        changed = True

    video = root.find("video")
    if video is None:
        video = ET.SubElement(root, "video")
        changed = True

    for name, path in (("HA Series", series_dir), ("HA Movies", movies_dir)):
        changed = _ensure_video_source(video, name, path) or changed

    if changed:
        _write_sources(tree, sources_path)

    return {"path": str(sources_path), "changed": changed}


def season_episode_key(path: Path) -> tuple[int, int, str]:
    """Order a media path by observed season, episode and filename."""
    probe = " ".join([path.name, str(path)])
    match = episode_match(probe)
    if match:
        season = match.group("season") or match.group("season_alt") or "999"
        episode = match.group("episode") or match.group("episode_alt") or "999"
        return (int(season), int(episode), str(path).lower())

    season = season_number(path)
    return (season if season is not None else 999, 999, str(path).lower())


def video_candidates(
    root: Path, *, minimum_size: int, video_suffixes: set[str]
) -> list[Path]:
    """Select complete local videos above the configured minimum size."""
    files = [root] if root.is_file() else list(root.rglob("*"))
    candidates: list[Path] = []
    for path in files:
        if not path.is_file():
            continue
        name = path.name.lower()
        if name.endswith(".part") or path.suffix.lower() not in video_suffixes:
            continue
        try:
            if path.stat().st_size < minimum_size:
                continue
        except OSError:
            continue
        candidates.append(path)
    return candidates


def choose_video_file_from_candidates(candidates: list[Path], media_type: str) -> Path:
    """Choose the first episode or the largest movie candidate."""
    if not candidates:
        error_message = "no completed video files are available"
        raise FileNotFoundError(error_message)
    if media_type == "series":
        return sorted(candidates, key=season_episode_key)[0]
    return sorted(
        candidates,
        key=lambda path: (path.stat().st_size, str(path).lower()),
        reverse=True,
    )[0]


def torrent_root_path(torrent: dict[str, Any]) -> Path:
    """Derive the local download root from existing tracker metadata."""
    download_dir = Path(str(torrent.get("downloadDir") or ""))
    name = str(torrent.get("name") or "").strip()
    if name:
        return download_dir / name

    files = torrent.get("files") or []
    if files and isinstance(files[0], dict):
        first = str(files[0].get("name") or "").strip()
        if first:
            return download_dir / first.split("/", 1)[0]
    return download_dir


def torrent_matches_payload(
    torrent: dict[str, Any], payload: dict[str, Any], root: Path | None = None
) -> bool:
    """Match a requested download by identifier, name or local root."""
    requested_id = str(payload.get("id") or "").strip()
    if requested_id and str(torrent.get("id") or "") == requested_id:
        return True

    requested_name = str(payload.get("name") or "").strip()
    torrent_name = str(torrent.get("name") or "").strip()
    if requested_name and torrent_name and requested_name == torrent_name:
        return True

    if root is not None:
        candidate_root = torrent_root_path(torrent)
        if path_contains(candidate_root, root) or path_contains(root, candidate_root):
            return True

    return False


def ensure_symlink(src: Path, dest: Path) -> str:
    """Retain an equivalent local link or create the requested library link."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink():
        try:
            if dest.resolve() == src.resolve():
                return "exists"
        except OSError:
            pass
        dest.unlink()
    elif dest.exists():
        if dest.is_file() and dest.stat().st_size == src.stat().st_size:
            return "exists_file"
        error_message = f"destination already exists and is not this video: {dest}"
        raise FileExistsError(error_message)

    dest.symlink_to(src)
    return "created"
