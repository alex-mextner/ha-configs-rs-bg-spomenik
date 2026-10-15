#!/usr/bin/env python3
"""Small local HTTP bridge for Home Assistant host actions."""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


CONFIG_DIR = Path.home() / ".config" / "ha-host-actions"
TOKEN_FILE = CONFIG_DIR / "token"
PASSWORD_FILE = CONFIG_DIR / "desktop-password"
KODI_APP_ID = "tv.kodi.Kodi"
AGENT_BROWSER = Path(os.environ.get("AGENT_BROWSER", "/home/linuxbrew/.linuxbrew/bin/agent-browser"))
RUTRACKER_LOGIN_URL = os.environ.get(
    "RUTRACKER_LOGIN_URL", "https://rutracker.org/forum/login.php"
)
RUTRACKER_BROWSER_SESSION = os.environ.get(
    "RUTRACKER_BROWSER_SESSION", "ha-rutracker-login"
)
KODI_JSONRPC_URL = os.environ.get("KODI_JSONRPC_URL", "http://127.0.0.1:8080/jsonrpc")
KODI_USERNAME = os.environ.get("KODI_USERNAME", "kodi")
KODI_PASSWORD = os.environ.get("KODI_PASSWORD", "kodi")
KODI_USERDATA_DIR = Path(os.environ.get("KODI_USERDATA_DIR", str(Path.home() / ".var/app/tv.kodi.Kodi/data/userdata")))
KODI_LIBRARY_DIR = Path(os.environ.get("KODI_LIBRARY_DIR", str(Path.home() / "KodiLibrary")))
KODI_SERIES_DIR = KODI_LIBRARY_DIR / "Series"
KODI_MOVIES_DIR = KODI_LIBRARY_DIR / "Movies"
TRANSMISSION_RPC_URL = os.environ.get("TRANSMISSION_RPC_URL", "http://127.0.0.1:9091/transmission/rpc")
TRANSMISSION_RPC_USERNAME = os.environ.get("TRANSMISSION_RPC_USERNAME", "")
TRANSMISSION_RPC_PASSWORD = os.environ.get("TRANSMISSION_RPC_PASSWORD", "")
VIDEO_SUFFIXES = {".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm"}
MIN_VIDEO_SIZE = 50 * 1024 * 1024
EPISODE_RE = re.compile(
    r"(?:^|[ ._\-\[\(])(?:s(?P<season>\d{1,2})[ ._\-]*e(?P<episode>\d{1,3})|"
    r"(?P<season_alt>\d{1,2})x(?P<episode_alt>\d{1,3}))(?:[ ._\-\]\)]|$)",
    re.IGNORECASE,
)
SEASON_HINT_RE = re.compile(r"(?:^|[ ._\-/\\])(?:s(?:eason)?|сезон)[ ._\-]*(\d{1,2})(?:[ ._\-/\\]|$)", re.IGNORECASE)
SERIES_HINT_RE = re.compile(r"(?:season|series|tv[ ._\-]?show|сезон|сериал|серии|эпизод)", re.IGNORECASE)
TITLE_CUT_RE = re.compile(
    r"\b(?:s\d{1,2}(?:e\d{1,3})?|season|сезон|1080p|720p|2160p|4320p|"
    r"bluray|bdrip|brrip|web[ ._\-]?dl|webrip|hdtv|hdrip|dvdrip|remux|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|hdr|dv|rus|eng|multi|teamhd|lostfilm)\b",
    re.IGNORECASE,
)


def read_secret(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def session_env() -> dict[str, str]:
    env = os.environ.copy()
    runtime_dir = f"/run/user/{os.getuid()}"
    env.update(
        {
            "HOME": str(Path.home()),
            "USER": os.environ.get("USER", "ultra"),
            "LOGNAME": os.environ.get("LOGNAME", "ultra"),
            "XDG_RUNTIME_DIR": runtime_dir,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus",
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
            "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", "wayland-0"),
        }
    )
    xauthority = os.environ.get("XAUTHORITY")
    if not xauthority:
        auth_files = sorted(Path(runtime_dir).glob(".mutter-Xwaylandauth.*"))
        if auth_files:
            xauthority = str(auth_files[-1])
    if xauthority:
        env["XAUTHORITY"] = xauthority
    return env


def run(
    cmd: list[str],
    timeout: float = 10.0,
    check: bool = False,
    input_data: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        env=session_env(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=check,
        input=input_data,
    )


def dbus_call(args: list[str], timeout: float = 5.0) -> dict[str, Any]:
    proc = run(["gdbus", "call", "--session", *args], timeout=timeout)
    return {
        "cmd": " ".join(args),
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def wake_and_unlock_screen() -> dict[str, Any]:
    results = []
    results.append(
        dbus_call(
            [
                "--dest",
                "org.gnome.Mutter.DisplayConfig",
                "--object-path",
                "/org/gnome/Mutter/DisplayConfig",
                "--method",
                "org.freedesktop.DBus.Properties.Set",
                "org.gnome.Mutter.DisplayConfig",
                "PowerSaveMode",
                "<int32 0>",
            ]
        )
    )
    results.append(
        dbus_call(
            [
                "--dest",
                "org.gnome.ScreenSaver",
                "--object-path",
                "/org/gnome/ScreenSaver",
                "--method",
                "org.gnome.ScreenSaver.SetActive",
                "false",
            ]
        )
    )
    return {"screen": "unlocked", "results": results}


def lock_screen() -> dict[str, Any]:
    result = dbus_call(
        [
            "--dest",
            "org.gnome.ScreenSaver",
            "--object-path",
            "/org/gnome/ScreenSaver",
            "--method",
            "org.gnome.ScreenSaver.Lock",
        ]
    )
    return {"screen": "locked", "result": result}


def kodi_running() -> bool:
    return bool(kodi_pids())


def kodi_pids() -> list[int]:
    proc = run(["ps", "-eo", "pid=,uid=,comm=,args="], timeout=3)
    if proc.returncode != 0:
        return []

    pids: list[int] = []
    current_uid = str(os.getuid())
    own_pid = os.getpid()
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, uid_s, comm, args = parts
        if uid_s != current_uid:
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid == own_pid:
            continue
        if "ha-host-actions.py" in args:
            continue
        if (
            "flatpak run tv.kodi.Kodi" in args
            or "/app/bin/kodi" in args
            or comm in {"kodi", "kodi.bin"}
        ):
            pids.append(pid)
    return pids


def start_kodi() -> dict[str, Any]:
    if kodi_running():
        return {"kodi": "already_running"}

    unit = f"ha-kodi-projector-{int(time.time())}"
    proc = subprocess.Popen(
        [
            "systemd-run",
            "--user",
            "--scope",
            "--collect",
            "--unit",
            unit,
            "flatpak",
            "run",
            KODI_APP_ID,
        ],
        env=session_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"kodi": "starting", "pid": proc.pid, "unit": unit}


def kodi_jsonrpc(method: str, params: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": "ha-host-actions", "method": method}
    if params is not None:
        payload["params"] = params
    request = urllib.request.Request(
        KODI_JSONRPC_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    credentials = f"{KODI_USERNAME}:{KODI_PASSWORD}".encode("utf-8")
    request.add_header(
        "Authorization",
        "Basic " + __import__("base64").b64encode(credentials).decode("ascii"),
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(65536).decode("utf-8", "replace")
    try:
        decoded: Any = json.loads(body)
    except json.JSONDecodeError:
        decoded = body
    return {"ok": True, "status": response.status, "body": decoded}


def transmission_rpc(method: str, arguments: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    payload = {"method": method, "arguments": arguments or {}}
    body = json.dumps(payload).encode("utf-8")
    session_id = ""
    for _ in range(2):
        request = urllib.request.Request(
            TRANSMISSION_RPC_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if session_id:
            request.add_header("X-Transmission-Session-Id", session_id)
        if TRANSMISSION_RPC_USERNAME or TRANSMISSION_RPC_PASSWORD:
            credentials = f"{TRANSMISSION_RPC_USERNAME}:{TRANSMISSION_RPC_PASSWORD}".encode("utf-8")
            request.add_header(
                "Authorization",
                "Basic " + __import__("base64").b64encode(credentials).decode("ascii"),
            )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
            if decoded.get("result") != "success":
                raise RuntimeError(decoded.get("result") or decoded)
            return decoded
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                session_id = exc.headers.get("X-Transmission-Session-Id", "")
                if session_id:
                    continue
            raise
    raise RuntimeError("Transmission RPC did not return a session id")


def transmission_torrents() -> list[dict[str, Any]]:
    response = transmission_rpc(
        "torrent-get",
        {
            "fields": [
                "id",
                "name",
                "downloadDir",
                "files",
                "fileStats",
                "labels",
                "status",
                "percentDone",
                "error",
                "errorString",
            ]
        },
    )
    torrents = response.get("arguments", {}).get("torrents", [])
    return torrents if isinstance(torrents, list) else []


def kodi_jsonrpc_quit() -> dict[str, Any]:
    try:
        return kodi_jsonrpc("Application.Quit", timeout=2)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def stop_kodi() -> dict[str, Any]:
    if not kodi_running():
        return {"kodi": "not_running"}

    rpc_result = kodi_jsonrpc_quit()
    time.sleep(3)
    if not kodi_running():
        return {"kodi": "stopped", "method": "jsonrpc", "jsonrpc": rpc_result}

    flatpak = run(["flatpak", "kill", KODI_APP_ID], timeout=5)
    time.sleep(1)
    if not kodi_running():
        return {
            "kodi": "stopped",
            "method": "flatpak_kill",
            "jsonrpc": rpc_result,
            "flatpak_returncode": flatpak.returncode,
        }

    killed_pids = kodi_pids()
    for pid in killed_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return {
        "kodi": "stop_requested",
        "method": "pid_term",
        "jsonrpc": rpc_result,
        "flatpak_returncode": flatpak.returncode,
        "pids": killed_pids,
    }


def projector_on() -> dict[str, Any]:
    return {"unlock": wake_and_unlock_screen(), "kodi": start_kodi()}


def projector_off() -> dict[str, Any]:
    return {"kodi": stop_kodi(), "lock": lock_screen()}


def episode_match(text: str) -> re.Match[str] | None:
    return EPISODE_RE.search(text)


def season_episode_key(path: Path) -> tuple[int, int, str]:
    probe = " ".join([path.name, str(path)])
    match = episode_match(probe)
    if match:
        season = match.group("season") or match.group("season_alt") or "999"
        episode = match.group("episode") or match.group("episode_alt") or "999"
        return (int(season), int(episode), str(path).lower())

    season = season_number(path)
    return (season if season is not None else 999, 999, str(path).lower())


def season_number(path: Path) -> int | None:
    for part in reversed(path.parts):
        match = SEASON_HINT_RE.search(part)
        if match:
            return int(match.group(1))
        short = re.search(r"(?:^|[ ._\-])s(\d{1,2})(?:[ ._\-]|$)", part, re.IGNORECASE)
        if short:
            return int(short.group(1))
    return None


def video_candidates(root: Path) -> list[Path]:
    files = [root] if root.is_file() else list(root.rglob("*"))
    candidates: list[Path] = []
    for path in files:
        if not path.is_file():
            continue
        name = path.name.lower()
        if name.endswith(".part") or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        try:
            if path.stat().st_size < MIN_VIDEO_SIZE:
                continue
        except OSError:
            continue
        candidates.append(path)
    return candidates


def choose_video_file_from_candidates(candidates: list[Path], media_type: str) -> Path:
    if not candidates:
        raise FileNotFoundError("no completed video files are available")
    if media_type == "series":
        return sorted(candidates, key=season_episode_key)[0]
    return sorted(candidates, key=lambda path: (path.stat().st_size, str(path).lower()), reverse=True)[0]


def choose_video_file(root: Path, media_type: str) -> Path:
    candidates = video_candidates(root)
    if not candidates:
        raise FileNotFoundError(f"no completed video files under {root}")
    return choose_video_file_from_candidates(candidates, media_type)


def normalized_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


def path_contains(parent: Path, child: Path) -> bool:
    parent_s = normalized_path(parent).rstrip("/") + "/"
    child_s = normalized_path(child)
    return child_s == parent_s.rstrip("/") or child_s.startswith(parent_s)


def torrent_root_path(torrent: dict[str, Any]) -> Path:
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


def torrent_matches_payload(torrent: dict[str, Any], payload: dict[str, Any], root: Path | None = None) -> bool:
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


def completed_video_files_for_torrent(torrent: dict[str, Any]) -> list[Path]:
    download_dir = Path(str(torrent.get("downloadDir") or ""))
    files = torrent.get("files") or []
    stats = torrent.get("fileStats") or []
    candidates: list[Path] = []

    if not isinstance(files, list):
        return candidates

    for index, file_info in enumerate(files):
        if not isinstance(file_info, dict):
            continue
        relative_name = str(file_info.get("name") or "").strip()
        if not relative_name:
            continue
        suffix = Path(relative_name).suffix.lower()
        if suffix not in VIDEO_SUFFIXES or relative_name.lower().endswith(".part"):
            continue

        length = int(file_info.get("length") or 0)
        if length < MIN_VIDEO_SIZE:
            continue

        file_stat = stats[index] if isinstance(stats, list) and index < len(stats) and isinstance(stats[index], dict) else {}
        if file_stat.get("wanted") is False:
            continue

        completed = int(file_stat.get("bytesCompleted") or file_info.get("bytesCompleted") or 0)
        if completed < length:
            continue

        path = download_dir / relative_name
        if path.exists() and path.is_file():
            candidates.append(path)

    return candidates


def completed_video_files_for_payload(payload: dict[str, Any], root: Path) -> tuple[list[Path], dict[str, Any] | None, list[dict[str, Any]]]:
    torrents = transmission_torrents()
    for torrent in torrents:
        if torrent_matches_payload(torrent, payload, root):
            completed = [path for path in completed_video_files_for_torrent(torrent) if path_contains(root, path) or path_contains(torrent_root_path(torrent), path)]
            return completed, torrent, torrents
    return [], None, torrents


def detect_media_type(
    root: Path,
    candidates: list[Path],
    payload: dict[str, Any] | None = None,
    torrent: dict[str, Any] | None = None,
) -> str:
    payload = payload or {}
    requested = str(payload.get("media_type") or payload.get("type") or "auto").strip().lower()
    if requested in {"series", "movies"}:
        return requested

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
    texts.extend(str(path.relative_to(root)) if path_contains(root, path) else str(path) for path in candidates)
    probe = "\n".join(text for text in texts if text)

    if episode_match(probe) or SERIES_HINT_RE.search(probe):
        return "series"

    if len(candidates) >= 3 and any(season_number(path) is not None for path in candidates):
        return "series"
    if len(candidates) >= 6:
        return "series"

    return "movies"


def trailing_slash(path: Path) -> str:
    return str(path) if str(path).endswith("/") else f"{path}/"


def sanitize_path_part(value: str, fallback: str) -> str:
    text = re.sub(r"[._]+", " ", value).strip()
    text = re.sub(r"[/:\\\0]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text or fallback)[:120].strip(" .") or fallback


def clean_media_title(value: str, fallback: str) -> str:
    text = Path(value).stem if "/" in value else value
    text = re.sub(r"[._]+", " ", text)
    cut = TITLE_CUT_RE.search(text)
    if cut and cut.start() >= 3:
        text = text[: cut.start()]
    text = re.sub(r"\[[^\]]*\]|\([^\)]*\)", " ", text)
    return sanitize_path_part(text, fallback)


def library_destination(src: Path, torrent: dict[str, Any], media_type: str) -> Path:
    torrent_name = str(torrent.get("name") or src.parent.name)
    if media_type == "series":
        show_title = clean_media_title(torrent_name, src.parent.name)
        season = season_number(src) or 1
        return KODI_SERIES_DIR / show_title / f"Season {season:02d}" / src.name

    movie_title = clean_media_title(torrent_name, src.stem)
    return KODI_MOVIES_DIR / movie_title / src.name


def ensure_symlink(src: Path, dest: Path) -> str:
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
        raise FileExistsError(f"destination already exists and is not this video: {dest}")

    os.symlink(src, dest)
    return "created"


def latest_kodi_video_db() -> Path | None:
    database_dir = KODI_USERDATA_DIR / "Database"
    databases = sorted(
        database_dir.glob("MyVideos*.db"),
        key=lambda path: int(re.search(r"(\d+)", path.stem).group(1)) if re.search(r"(\d+)", path.stem) else 0,
    )
    return databases[-1] if databases else None


def ensure_kodi_sources_xml() -> dict[str, Any]:
    KODI_SERIES_DIR.mkdir(parents=True, exist_ok=True)
    KODI_MOVIES_DIR.mkdir(parents=True, exist_ok=True)
    sources_path = KODI_USERDATA_DIR / "sources.xml"
    changed = False

    if sources_path.exists():
        tree = ET.parse(sources_path)
        root = tree.getroot()
    else:
        root = ET.Element("mediasources")
        tree = ET.ElementTree(root)
        changed = True

    video = root.find("video")
    if video is None:
        video = ET.SubElement(root, "video")
        changed = True

    for name, path in (("HA Series", KODI_SERIES_DIR), ("HA Movies", KODI_MOVIES_DIR)):
        source = None
        for candidate in video.findall("source"):
            if (candidate.findtext("name") or "") == name:
                source = candidate
                break
        if source is None:
            source = ET.SubElement(video, "source")
            ET.SubElement(source, "name").text = name
            ET.SubElement(source, "path", {"pathversion": "1"}).text = trailing_slash(path)
            ET.SubElement(source, "allowsharing").text = "true"
            changed = True
            continue

        path_node = source.find("path")
        wanted_path = trailing_slash(path)
        if path_node is None:
            path_node = ET.SubElement(source, "path", {"pathversion": "1"})
            changed = True
        if path_node.text != wanted_path:
            path_node.text = wanted_path
            changed = True

    if changed:
        sources_path.parent.mkdir(parents=True, exist_ok=True)
        if sources_path.exists():
            backup = sources_path.with_suffix(".xml.ha-host-actions.bak")
            if not backup.exists():
                shutil.copy2(sources_path, backup)
        ET.indent(tree, space="  ")
        tree.write(sources_path, encoding="utf-8", xml_declaration=True)

    return {"path": str(sources_path), "changed": changed}


def ensure_kodi_video_db_sources() -> dict[str, Any]:
    db_path = latest_kodi_video_db()
    if db_path is None:
        return {"changed": False, "error": "Kodi video database was not found"}

    backup = db_path.with_suffix(db_path.suffix + ".ha-host-actions.bak")
    if not backup.exists():
        shutil.copy2(db_path, backup)

    changed = False
    rows: list[dict[str, Any]] = []
    conn = sqlite3.connect(db_path, timeout=5)
    try:
      with conn:
        conn.execute("PRAGMA busy_timeout=5000")
        for path, content, scraper, use_folder_names in (
            (KODI_SERIES_DIR, "tvshows", "metadata.tvshows.themoviedb.org.python", 0),
            (KODI_MOVIES_DIR, "movies", "metadata.themoviedb.org.python", 1),
        ):
            path_text = trailing_slash(path)
            row = conn.execute(
                "select idPath, strContent, strScraper, scanRecursive, useFolderNames, noUpdate, exclude from path where strPath = ?",
                (path_text,),
            ).fetchone()
            if row:
                desired = (content, scraper, 2147483647, use_folder_names, 0, 0)
                current = (row[1], row[2], row[3], row[4], row[5], row[6])
                updated = current != desired
                if updated:
                    conn.execute(
                        """
                        update path
                           set strContent = ?, strScraper = ?, scanRecursive = ?,
                               useFolderNames = ?, noUpdate = ?, exclude = ?
                         where strPath = ?
                        """,
                        (*desired, path_text),
                    )
                    changed = True
                rows.append({"path": path_text, "idPath": row[0], "updated": updated})
            else:
                conn.execute(
                    """
                    insert into path
                      (strPath, strContent, strScraper, scanRecursive, useFolderNames, noUpdate, exclude, dateAdded)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (path_text, content, scraper, 2147483647, use_folder_names, 0, 0, datetime.now().isoformat(timespec="seconds")),
                )
                changed = True
                rows.append({"path": path_text, "idPath": conn.execute("select last_insert_rowid()").fetchone()[0], "updated": False})
        conn.commit()
    finally:
        conn.close()

    return {"path": str(db_path), "changed": changed, "rows": rows}


def ensure_kodi_library_setup() -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        override = run(["flatpak", "override", "--user", f"--filesystem={KODI_LIBRARY_DIR}:ro", KODI_APP_ID], timeout=10)
        result["flatpak"] = {"returncode": override.returncode, "stderr": override.stderr.strip()[-500:]}
    except Exception as exc:  # noqa: BLE001 - setup should still continue.
        result["flatpak"] = {"error": repr(exc)}

    result["sources_xml"] = ensure_kodi_sources_xml()
    try:
        result["video_db"] = ensure_kodi_video_db_sources()
    except Exception as exc:  # noqa: BLE001 - report DB lock instead of hiding useful symlink sync.
        result["video_db"] = {"changed": False, "error": repr(exc)}
    return result


def sync_completed_files_to_kodi(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    setup = ensure_kodi_library_setup()
    torrents = transmission_torrents()
    linked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    touched_roots: set[Path] = set()

    for torrent in torrents:
        candidates = completed_video_files_for_torrent(torrent)
        if not candidates:
            continue
        root = torrent_root_path(torrent)
        media_type = detect_media_type(root, candidates, {"name": torrent.get("name")}, torrent)
        for src in candidates:
            if path_contains(KODI_LIBRARY_DIR, src):
                continue
            try:
                dest = library_destination(src, torrent, media_type)
                status = ensure_symlink(src, dest)
                if status == "created":
                    touched_roots.add(KODI_SERIES_DIR if media_type == "series" else KODI_MOVIES_DIR)
                linked.append(
                    {
                        "source": str(src),
                        "library_path": str(dest),
                        "media_type": media_type,
                        "status": status,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - continue syncing other completed files.
                skipped.append({"source": str(src), "error": repr(exc)})

    scan_result: list[dict[str, Any]] = []
    should_scan = bool(payload.get("scan", True))
    if should_scan and payload.get("start_kodi"):
        start_kodi()
    if should_scan and kodi_running() and (touched_roots or setup.get("sources_xml", {}).get("changed") or setup.get("video_db", {}).get("changed")):
        try:
            wait_for_kodi_jsonrpc(timeout=20)
            for root in sorted(touched_roots or {KODI_SERIES_DIR, KODI_MOVIES_DIR}, key=str):
                scan_result.append({"directory": str(root), "result": kodi_jsonrpc("VideoLibrary.Scan", {"directory": trailing_slash(root)}, timeout=3)})
        except Exception as exc:  # noqa: BLE001 - return scan diagnostics to HA.
            scan_result.append({"error": repr(exc)})

    return {
        "library_dir": str(KODI_LIBRARY_DIR),
        "setup": setup,
        "linked_count": len([item for item in linked if item["status"] == "created"]),
        "known_count": len(linked),
        "linked": linked[:50],
        "skipped": skipped[:20],
        "scan": scan_result,
    }


def wait_for_kodi_jsonrpc(timeout: float = 25.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            return kodi_jsonrpc("Application.GetProperties", {"properties": ["name", "version"]}, timeout=2)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
            time.sleep(1)
    raise RuntimeError(f"Kodi JSON-RPC is not ready: {last_error}")


def kodi_play_path(payload: dict[str, Any]) -> dict[str, Any]:
    raw_path = str(payload.get("path") or "").strip()
    if not raw_path:
        raise ValueError("path is required")

    root = Path(raw_path).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"path does not exist: {root}")

    rpc_error = ""
    matched_torrent: dict[str, Any] | None = None
    try:
        completed_candidates, matched_torrent, _ = completed_video_files_for_payload(payload, root)
    except Exception as exc:  # noqa: BLE001 - allow manual local playback without Transmission RPC.
        completed_candidates = []
        rpc_error = repr(exc)

    if matched_torrent is not None:
        candidates = completed_candidates
        if not candidates:
            raise FileNotFoundError(f"no fully downloaded video files are available under {root}")
    else:
        candidates = video_candidates(root)

    media_type = detect_media_type(root, candidates, payload, matched_torrent)
    selected = choose_video_file_from_candidates(candidates, media_type)
    sync_result = sync_completed_files_to_kodi({"scan": False})
    start_result = start_kodi()
    ready = wait_for_kodi_jsonrpc()
    scan_dir = str(selected.parent if selected.is_file() else selected)
    scan_result = kodi_jsonrpc("VideoLibrary.Scan", {"directory": scan_dir}, timeout=3)
    open_result = kodi_jsonrpc("Player.Open", {"item": {"file": str(selected)}}, timeout=8)
    time.sleep(2)
    players = kodi_jsonrpc("Player.GetActivePlayers", timeout=3)
    active_players = []
    body = players.get("body")
    if isinstance(body, dict):
        active_players = body.get("result") or []
    if not active_players:
        raise RuntimeError(f"Kodi did not start playback for {selected}: {open_result}")
    return {
        "kodi": "playing",
        "requested_path": str(root),
        "selected_file": str(selected),
        "media_type": media_type,
        "transmission_rpc_error": rpc_error,
        "library_sync": {
            "linked_count": sync_result.get("linked_count"),
            "known_count": sync_result.get("known_count"),
            "skipped": sync_result.get("skipped", [])[:3],
        },
        "start": start_result,
        "ready": ready,
        "scan": scan_result,
        "open": open_result,
        "players": players,
    }


def agent_browser(args: list[str], timeout: float = 30.0, input_data: str | None = None) -> subprocess.CompletedProcess[str]:
    if not AGENT_BROWSER.exists():
        return subprocess.CompletedProcess(
            [str(AGENT_BROWSER), *args],
            127,
            "",
            f"{AGENT_BROWSER} is not installed",
        )
    return run(
        [str(AGENT_BROWSER), "--session", RUTRACKER_BROWSER_SESSION, *args],
        timeout=timeout,
        input_data=input_data,
    )


def browser_cookie_header(stdout: str) -> str:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return ""

    cookies = payload
    if isinstance(payload, dict):
        cookies = payload.get("data", {}).get("cookies", [])
    if not isinstance(cookies, list):
        return ""

    parts: list[str] = []
    seen: set[str] = set()
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "")
        if "rutracker.org" not in domain:
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def rutracker_browser_login(payload: dict[str, Any]) -> dict[str, Any]:
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        return {"ok": False, "error": "username_and_password_required"}

    opened = agent_browser(["open", RUTRACKER_LOGIN_URL], timeout=45)
    if opened.returncode != 0:
        return {
            "ok": False,
            "error": "agent_browser_open_failed",
            "stderr": opened.stderr.strip()[-500:],
        }

    script = f"""
(() => {{
  const username = {json.dumps(username)};
  const password = {json.dumps(password)};
  const pick = (selectors) => selectors.map((selector) => document.querySelector(selector)).find(Boolean);
  const setValue = (element, value) => {{
    element.focus();
    const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    if (descriptor && descriptor.set) {{
      descriptor.set.call(element, value);
    }} else {{
      element.value = value;
    }}
    element.dispatchEvent(new Event("input", {{ bubbles: true }}));
    element.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }};

  const user = pick([
    'input[name="login_username"]',
    'input[name="username"]',
    'input[name="user"]',
    'input[type="email"]',
    'input[type="text"]'
  ]);
  const pass = pick([
    'input[name="login_password"]',
    'input[name="password"]',
    'input[type="password"]'
  ]);
  if (!user || !pass) {{
    return {{
      ok: false,
      reason: "login_fields_not_found",
      url: location.href,
      title: document.title,
      text: (document.body && document.body.innerText || "").slice(0, 300)
    }};
  }}

  setValue(user, username);
  setValue(pass, password);
  const form = pass.form || user.form || document.querySelector("form");
  const submit = form && form.querySelector('input[type="submit"], button[type="submit"], input[name="login"], button[name="login"]');
  if (submit) {{
    submit.click();
  }} else if (form && typeof form.submit === "function") {{
    form.submit();
  }} else {{
    pass.dispatchEvent(new KeyboardEvent("keydown", {{ key: "Enter", code: "Enter", bubbles: true }}));
  }}
  return {{ ok: true, url: location.href, title: document.title }};
}})()
"""
    submitted = agent_browser(["--json", "eval", "--stdin"], timeout=15, input_data=script)
    if submitted.returncode != 0:
        return {
            "ok": False,
            "error": "agent_browser_submit_failed",
            "stderr": submitted.stderr.strip()[-500:],
        }

    agent_browser(["wait", "6000"], timeout=10)
    cookies = agent_browser(["--json", "cookies", "get"], timeout=15)
    cookie_header = browser_cookie_header(cookies.stdout)
    current_url = agent_browser(["get", "url"], timeout=10)
    title = agent_browser(["get", "title"], timeout=10)

    if not cookie_header:
        return {
            "ok": False,
            "error": "no_rutracker_cookies_returned",
            "url": current_url.stdout.strip(),
            "title": title.stdout.strip(),
        }

    return {
        "ok": True,
        "method": "agent-browser",
        "cookie_header": cookie_header,
        "cookies_count": cookie_header.count("="),
        "url": current_url.stdout.strip(),
        "title": title.stdout.strip(),
        "message": "agent-browser login produced RuTracker.org cookies",
    }


def health() -> dict[str, Any]:
    active = dbus_call(
        [
            "--dest",
            "org.gnome.ScreenSaver",
            "--object-path",
            "/org/gnome/ScreenSaver",
            "--method",
            "org.gnome.ScreenSaver.GetActive",
        ],
        # HA's health probe gives up after 3 s; a stuck gnome-shell must not stall it.
        timeout=1.5,
    )
    return {
        "ok": True,
        "kodi_running": kodi_running(),
        "screen_active_raw": active.get("stdout"),
        "password_file_present": PASSWORD_FILE.exists(),
    }


# Voice stack controls. The HA container has no docker CLI, so HA's
# shell_command.voice_* call these endpoints instead (fixed allow-list, no args).
COMPOSE = ["/home/ultra/homeassistant/scripts/ha_compose.sh"]
SATELLITE = "homeassistant-wyoming-satellite-1"
APLAY = "aplay -D plughw:CARD=ArrayUAC10,DEV=0"
VOICE_COMMANDS: dict[str, list[str]] = {
    "restart-services": [*COMPOSE, "up", "-d", "--force-recreate", "wyoming-openwakeword", "wyoming-satellite"],
    "restart-satellite": [*COMPOSE, "up", "-d", "--force-recreate", "wyoming-satellite"],
    "stop-recording": [*COMPOSE, "stop", "wyoming-satellite"],
    "start-recording": [*COMPOSE, "start", "wyoming-satellite"],
    "play-last-wake": ["docker", "exec", SATELLITE, "sh", "-c", f"{APLAY} $(ls -1t /tmp/debug/*-wake.wav | head -n1)"],
    "play-last-stt": ["docker", "exec", SATELLITE, "sh", "-c", f"{APLAY} $(ls -1t /tmp/debug/*-stt.wav | head -n1)"],
    "play-awake": ["docker", "exec", SATELLITE, "sh", "-c", f"{APLAY} /app/sounds/awake.wav"],
    "play-done": ["docker", "exec", SATELLITE, "sh", "-c", f"{APLAY} /app/sounds/done.wav"],
}


def voice_action(name: str):
    def action() -> dict[str, Any]:
        proc = subprocess.run(VOICE_COMMANDS[name], text=True, capture_output=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"{name} failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")
        return {"voice": name, "returncode": proc.returncode}

    return action


# Network devices (router-cli). HA's network-devices card reads the inventory and
# pins/unpins DHCP leases through these endpoints. Every argument is validated and
# passed as an argv list — never through a shell. `dry_run: true` in a body makes
# pin/unpin pass --dry-run so the flow can be tested without touching the router.
ROUTER_BIN = os.environ.get("ROUTER_BIN") or shutil.which("router") or str(Path.home() / ".local/bin/router")
MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
SINCE_RE = re.compile(r"^\d{1,4}[smhd]$")
MDI_RE = re.compile(r"^mdi:[a-z0-9-]{1,64}$")
INVENTORY_FILTERS = {"recent", "active", "all", "reserved", "new"}
SCAN_WAIT = 20.0
_scan_lock = threading.Lock()
_scan_state: dict[str, Any] = {"running": None, "started_at": None, "last": None}


class InvalidInput(ValueError):
    """Bad request data (reported to HA as HTTP 400)."""


def router_cmd(args: list[str], timeout: float = 25.0) -> subprocess.CompletedProcess[str]:
    if not Path(ROUTER_BIN).exists():
        raise RuntimeError(f"router CLI not found at {ROUTER_BIN}")
    return subprocess.run(
        [ROUTER_BIN, *args], text=True, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL
    )


def router_result(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    if proc.returncode != 0:
        raise RuntimeError(f"router failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()[-500:]}")
    out = proc.stdout.strip()
    try:
        parsed: Any = json.loads(out) if out else None
    except json.JSONDecodeError:
        parsed = None
    result: dict[str, Any] = {"returncode": proc.returncode, "stderr": proc.stderr.strip()[-800:]}
    if isinstance(parsed, (dict, list)):
        result["result"] = parsed
    else:
        result["stdout"] = out[-2000:]
    return result


def first_value(value: Any) -> Any:
    # GET query params arrive as lists (parse_qs); JSON bodies as scalars.
    return value[0] if isinstance(value, list) and value else value


def valid_mac(value: Any) -> str:
    mac = str(first_value(value) or "").strip().lower().replace("-", ":")
    if not MAC_RE.match(mac):
        raise InvalidInput("invalid_mac")
    return mac


def valid_ip(value: Any) -> str:
    try:
        ip = ipaddress.ip_address(str(first_value(value) or "").strip())
    except ValueError as exc:
        raise InvalidInput("invalid_ip") from exc
    if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_multicast or ip.is_unspecified:
        raise InvalidInput("invalid_ip")
    return str(ip)


def valid_name(value: Any) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(first_value(value) or "")).strip()[:64]
    if name.startswith("-"):
        raise InvalidInput("invalid_name")
    return name


def is_dry_run(payload: dict[str, Any]) -> bool:
    return str(first_value(payload.get("dry_run", False))).lower() in {"1", "true", "yes", "on"}


def network_inventory(payload: dict[str, Any]) -> dict[str, Any]:
    flt = str(first_value(payload.get("filter")) or "all")
    if flt not in INVENTORY_FILTERS:
        raise InvalidInput("invalid_filter")
    args = ["inventory", "list", "--json", "--filter", flt]
    since = str(first_value(payload.get("since")) or "")
    if since:
        if not SINCE_RE.match(since):
            raise InvalidInput("invalid_since")
        args += ["--since", since]
    no_favicons = str(first_value(payload.get("favicons", "1"))).lower() in {"0", "false", "no"}
    if no_favicons:
        # Fast path: the CLI never even reads the favicon blobs (the full answer is ~100 KB).
        args.append("--no-favicons")
    proc = router_cmd(args, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(f"router inventory failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("router inventory returned invalid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        raise RuntimeError("router inventory returned unexpected JSON")
    if no_favicons:
        for device in data["devices"]:
            for service in device.get("services") or []:
                service["favicon_data_url"] = None
    data["scan"] = {"running": _scan_state["running"], "started_at": _scan_state["started_at"], "last": _scan_state["last"]}
    return data


# History / stats (router-cli `inventory history|stats`, local DB only — never the router).
# Responses are capped: at most HISTORY_MAX_BUCKETS buckets (the bucket grows to fit the
# requested days) and at most STATS_MAX_HOURS entries in per_hour (averaged down to fit).
BUCKET_RE = re.compile(r"^\d{1,3}[mhd]$")
HISTORY_MAX_BUCKETS = 400
STATS_MAX_HOURS = 336
_BUCKET_STEPS = [(300, "5m"), (900, "15m"), (1800, "30m"), (3600, "1h"), (10800, "3h"), (21600, "6h"), (43200, "12h"), (86400, "1d")]


def valid_days(value: Any, default: float = 7.0) -> float:
    raw = first_value(value)
    if raw in (None, ""):
        return default
    try:
        days = float(str(raw))
    except ValueError as exc:
        raise InvalidInput("invalid_days") from exc
    if not 0 < days <= 90:
        raise InvalidInput("invalid_days")
    return days


def valid_device(value: Any) -> str:
    raw = str(first_value(value) or "").strip()
    if not raw:
        raise InvalidInput("device_required")
    try:
        return valid_mac(raw)
    except InvalidInput:
        pass
    try:
        return valid_ip(raw)
    except InvalidInput:
        pass
    name = valid_name(raw)
    if not name:
        raise InvalidInput("invalid_device")
    return name


def _bucket_seconds(text: str) -> int:
    return int(text[:-1]) * {"m": 60, "h": 3600, "d": 86400}[text[-1]]


def network_history(payload: dict[str, Any]) -> dict[str, Any]:
    device = valid_device(payload.get("device") or payload.get("mac"))
    days = valid_days(payload.get("days"))
    bucket = str(first_value(payload.get("bucket")) or "1h")
    if not BUCKET_RE.match(bucket) or not 300 <= _bucket_seconds(bucket) <= 86400:
        raise InvalidInput("invalid_bucket")
    for seconds, label in _BUCKET_STEPS:
        if seconds >= _bucket_seconds(bucket) and days * 86400 / seconds <= HISTORY_MAX_BUCKETS:
            bucket = label
            break
    args = ["inventory", "history", device, "--json", "--days", f"{days:g}", "--bucket", bucket]
    result = router_result(router_cmd(args, timeout=20))
    data = result.get("result")
    if not isinstance(data, dict) or not isinstance(data.get("buckets"), list):
        raise RuntimeError(f"router inventory history failed: {result.get('stderr') or result.get('stdout')}")
    return data


def _downsample_hours(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(rows) <= limit:
        return rows
    step = -(-len(rows) // limit)
    out = []
    for i in range(0, len(rows), step):
        group = rows[i : i + step]
        values = [r["online"] for r in group if r.get("online") is not None]
        out.append(
            {
                "t": group[0]["t"],
                "online": round(sum(values) / len(values), 1) if values else None,
                "samples": sum(int(r.get("samples") or 0) for r in group),
                "hours": len(group),
            }
        )
    return out


def network_stats(payload: dict[str, Any]) -> dict[str, Any]:
    days = valid_days(payload.get("days"))
    result = router_result(router_cmd(["inventory", "stats", "--json", "--days", f"{days:g}"], timeout=30))
    data = result.get("result")
    if not isinstance(data, dict) or not isinstance(data.get("per_hour"), list):
        raise RuntimeError(f"router inventory stats failed: {result.get('stderr') or result.get('stdout')}")
    data["per_hour"] = _downsample_hours(data["per_hour"], STATS_MAX_HOURS)
    return data


def _scan_worker(args: list[str], label: str) -> None:
    try:
        proc = subprocess.run(
            [ROUTER_BIN, *args], text=True, capture_output=True, timeout=1800, stdin=subprocess.DEVNULL
        )
        _scan_state["last"] = {
            "target": label,
            "returncode": proc.returncode,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "stderr": proc.stderr.strip()[-300:],
        }
    except Exception as exc:  # noqa: BLE001 - background thread, record and move on.
        _scan_state["last"] = {"target": label, "error": repr(exc)}
    finally:
        _scan_state["running"] = None
        _scan_state["started_at"] = None
        _scan_lock.release()


def network_scan(payload: dict[str, Any]) -> dict[str, Any]:
    ip = first_value(payload.get("ip"))
    if ip:
        ip = valid_ip(ip)
        args, label = ["scan", "--ip", ip, "--json"], ip
    else:
        args, label = ["scan", "--all-online", "--json"], "all-online"
    if not Path(ROUTER_BIN).exists():
        raise RuntimeError(f"router CLI not found at {ROUTER_BIN}")
    if not _scan_lock.acquire(blocking=False):
        return {"started": False, "busy": True, "running": _scan_state["running"]}
    _scan_state["running"] = label
    _scan_state["started_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    worker = threading.Thread(target=_scan_worker, args=(args, label), daemon=True)
    worker.start()
    # A single-host scan usually finishes quickly: wait briefly so the card can refresh
    # right away. Full scans (and slow hosts) keep running in the background.
    worker.join(SCAN_WAIT if label != "all-online" else 0)
    done = not worker.is_alive()
    return {"started": True, "target": label, "done": done, "last": _scan_state["last"] if done else None}


# Static-lease writes are serialized. router-cli's reserve/unreserve change the router but
# not the local inventory DB (its reserved_ip is refreshed only by `inventory update`), so a
# successful write is followed by exactly one `router inventory update` — otherwise the card
# would keep showing the old state until the hourly poll.
_write_lock = threading.Lock()
SLOTS_FULL_RE = re.compile(r"all (\d+) static lease slots are in use")


def _refresh_after_write() -> dict[str, Any]:
    if not _update_lock.acquire(timeout=60):
        return {"refreshed": False, "reason": "busy"}
    try:
        result = router_result(router_cmd(["inventory", "update", "--json", "--wait", "40"], timeout=60))
        summary = result.get("result") if isinstance(result.get("result"), dict) else {}
        print(f"network write: inventory refreshed, {summary.get('reservations')} reservations", flush=True)
        return {"refreshed": True, "last_poll": summary.get("at"), "reservations": summary.get("reservations")}
    except Exception as exc:  # noqa: BLE001 - the write itself succeeded; report, don't fail it.
        print(f"network write: inventory refresh failed: {exc!r}", flush=True)
        return {"refreshed": False, "error": str(exc)[-300:]}
    finally:
        _update_lock.release()


def _reserve(mac: str, ip: str, name: str, dry_run: bool) -> dict[str, Any]:
    args = ["reserve", mac, ip, "--yes", "--json"]
    if name:
        args += ["--name", name]
    if dry_run:
        args.append("--dry-run")
    proc = router_cmd(args, timeout=60)
    if proc.returncode != 0:
        m = SLOTS_FULL_RE.search(f"{proc.stderr}\n{proc.stdout}")
        if m:
            # Structured for the card: it offers to replace one of the existing reservations.
            raise RuntimeError(f"slots_full:{m.group(1)}")
    return router_result(proc)


def _unreserve(mac: str, dry_run: bool) -> dict[str, Any]:
    args = ["unreserve", mac, "--yes", "--json"]
    if dry_run:
        args.append("--dry-run")
    return router_result(router_cmd(args, timeout=60))


def network_pin(payload: dict[str, Any]) -> dict[str, Any]:
    mac, ip = valid_mac(payload.get("mac")), valid_ip(payload.get("ip"))
    name = valid_name(payload.get("name"))
    dry_run = is_dry_run(payload)
    with _write_lock:
        out = {"action": "pin", "mac": mac, "ip": ip, "dry_run": dry_run, **_reserve(mac, ip, name, dry_run)}
        if not dry_run:
            out["inventory"] = _refresh_after_write()
    return out


def network_unpin(payload: dict[str, Any]) -> dict[str, Any]:
    mac = valid_mac(payload.get("mac"))
    dry_run = is_dry_run(payload)
    with _write_lock:
        out = {"action": "unpin", "mac": mac, "dry_run": dry_run, **_unreserve(mac, dry_run)}
        if not dry_run:
            out["inventory"] = _refresh_after_write()
    return out


def _reserved_devices() -> dict[str, dict[str, Any]]:
    """Current reservations from the local inventory DB (no router traffic)."""
    proc = router_cmd(["inventory", "list", "--json", "--filter", "reserved", "--no-favicons"], timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(f"router inventory failed ({proc.returncode}): {proc.stderr.strip()[-300:]}")
    data = json.loads(proc.stdout)
    return {str(d.get("mac")): d for d in data.get("devices") or [] if d.get("reserved_ip")}


def network_pin_replace(payload: dict[str, Any]) -> dict[str, Any]:
    """All static-lease slots are taken: free `replace_mac`'s slot, then pin `mac` to `ip`.

    If the new reservation fails, the old one is restored (same MAC and IP)."""
    old_mac = valid_mac(payload.get("replace_mac"))
    mac, ip = valid_mac(payload.get("mac")), valid_ip(payload.get("ip"))
    name = valid_name(payload.get("name"))
    dry_run = is_dry_run(payload)
    if old_mac == mac:
        raise InvalidInput("same_device")
    with _write_lock:
        old = _reserved_devices().get(old_mac)
        if not old:
            raise InvalidInput("not_reserved")
        if old.get("is_self"):
            # This server's LAN IP is hardcoded in HA's config (internal_url, go2rtc).
            raise InvalidInput("protected_reservation")
        old_ip = valid_ip(old.get("reserved_ip"))
        out: dict[str, Any] = {
            "action": "pin-replace", "replace_mac": old_mac, "replace_ip": old_ip,
            "mac": mac, "ip": ip, "dry_run": dry_run,
        }
        print(f"network pin-replace: {old_mac} {old_ip} -> {mac} {ip} (dry_run={dry_run})", flush=True)
        out["unreserve"] = _unreserve(old_mac, dry_run)
        try:
            out["reserve"] = _reserve(mac, ip, name, dry_run)
        except Exception as exc:
            if dry_run:
                raise
            print(f"network pin-replace: reserve failed ({exc!r}); restoring {old_mac} {old_ip}", flush=True)
            try:
                _reserve(old_mac, old_ip, "", False)
                rollback = "restored"
            except Exception as exc2:  # noqa: BLE001 - report both failures.
                rollback = f"FAILED: {exc2}"
            _refresh_after_write()
            raise RuntimeError(f"reserve failed: {exc}; old reservation {rollback}") from exc
        if not dry_run:
            out["inventory"] = _refresh_after_write()
    return out


def network_alias(payload: dict[str, Any]) -> dict[str, Any]:
    mac = valid_mac(payload.get("mac"))
    name = valid_name(payload.get("name"))
    icon = str(first_value(payload.get("icon")) or "").strip().lower()
    if icon and not MDI_RE.match(icon):
        raise InvalidInput("invalid_icon")
    if not name and not icon:
        raise InvalidInput("name_or_icon_required")
    args = ["alias", mac, "--json"]
    if name:
        args += ["--name", name]
    if icon:
        args += ["--icon", icon]
    return {"action": "alias", "mac": mac, **router_result(router_cmd(args, timeout=15))}


# The router (Ubee) hangs when polled often. It is polled ONLY by the hourly
# router-inventory-update.timer and by this endpoint (the card's "Обновить" button / the
# button.network_devices_update entity). Requests are serialized, and a poll younger than
# NETWORK_UPDATE_MIN_AGE seconds is reused instead of hitting the router again.
NETWORK_UPDATE_MIN_AGE = float(os.environ.get("NETWORK_UPDATE_MIN_AGE", "60"))
ROUTER_DB = Path(os.environ.get("ROUTER_CLI_DB") or Path.home() / ".local/share/router-cli/inventory.sqlite3")
_update_lock = threading.Lock()


def last_poll() -> tuple[str | None, float | None]:
    """(ISO time, age in seconds) of the last inventory poll, read from the local DB."""
    try:
        con = sqlite3.connect(f"file:{ROUTER_DB}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'last_poll'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None, None
    if not row or not row[0]:
        return None, None
    try:
        age = time.time() - datetime.fromisoformat(str(row[0])).timestamp()
    except ValueError:
        return str(row[0]), None
    return str(row[0]), age


def network_update(payload: dict[str, Any]) -> dict[str, Any]:
    at, age = last_poll()
    if age is not None and age < NETWORK_UPDATE_MIN_AGE:
        print(f"network update: skipped, last poll {age:.0f}s ago", flush=True)
        return {"polled": False, "reason": "fresh", "last_poll": at, "age_s": round(age)}
    if not _update_lock.acquire(timeout=45):
        return {"polled": False, "reason": "busy", "last_poll": at}
    try:
        # Another request may have polled while this one waited for the lock.
        at, age = last_poll()
        if age is not None and age < NETWORK_UPDATE_MIN_AGE:
            print(f"network update: joined a poll {age:.0f}s old", flush=True)
            return {"polled": False, "reason": "fresh", "last_poll": at, "age_s": round(age)}
        print("network update: polling the router (router inventory update)", flush=True)
        started = time.monotonic()
        result = router_result(router_cmd(["inventory", "update", "--json", "--wait", "40"], timeout=60))
        took = time.monotonic() - started
        summary = result.get("result") if isinstance(result.get("result"), dict) else {}
        print(f"network update: done in {took:.1f}s, seen {summary.get('seen')}", flush=True)
        return {
            "polled": not summary.get("skipped", False),
            "last_poll": summary.get("at") or last_poll()[0],
            "seen": summary.get("seen"),
            "new": summary.get("new"),
            "went_offline": summary.get("went_offline"),
            "took_s": round(took, 1),
        }
    finally:
        _update_lock.release()


def network_summary(query: dict[str, Any]) -> dict[str, Any]:
    """Compact, local-DB-only view for Home Assistant sensors (never touches the router)."""
    proc = router_cmd(["inventory", "list", "--json", "--filter", "all"], timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(f"router inventory failed ({proc.returncode}): {proc.stderr.strip()[-300:]}")
    data = json.loads(proc.stdout)
    devices = data.get("devices") or []
    online = [d for d in devices if d.get("online")]
    day_ago = time.time() - 86400

    def ts(value: Any) -> float:
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except ValueError:
            return 0.0

    def label(d: dict[str, Any]) -> str:
        return str(d.get("hostname") or d.get("vendor") or d.get("mac"))

    return {
        "last_poll": data.get("last_poll"),
        "online_count": len(online),
        "known_count": len(devices),
        "reserved_count": sum(1 for d in devices if d.get("reserved_ip")),
        "new_24h": [
            {"name": label(d), "mac": d.get("mac"), "ip": d.get("ip")}
            for d in devices
            if d.get("first_seen") and ts(d["first_seen"]) >= day_ago
        ],
        "online": [{"name": label(d), "ip": d.get("ip"), "mac": d.get("mac")} for d in online],
        "online_macs": [d.get("mac") for d in online],
        "online_names": sorted({label(d) for d in online}, key=str.casefold),
    }


ROUTES = {
    ("POST", "/projector/on"): projector_on,
    ("POST", "/projector/off"): projector_off,
    ("POST", "/kodi/start"): start_kodi,
    ("POST", "/kodi/stop"): stop_kodi,
    ("POST", "/kodi/play-path"): kodi_play_path,
    ("POST", "/kodi/sync-library"): sync_completed_files_to_kodi,
    ("POST", "/screen/unlock"): wake_and_unlock_screen,
    ("POST", "/screen/lock"): lock_screen,
    ("POST", "/rutracker/login"): rutracker_browser_login,
    ("GET", "/health"): health,
    **{("POST", f"/voice/{name}"): voice_action(name) for name in VOICE_COMMANDS},
    ("GET", "/network/inventory"): network_inventory,
    ("POST", "/network/inventory"): network_inventory,
    ("POST", "/network/scan"): network_scan,
    ("POST", "/network/pin"): network_pin,
    ("POST", "/network/unpin"): network_unpin,
    ("POST", "/network/pin-replace"): network_pin_replace,
    ("POST", "/network/alias"): network_alias,
    ("POST", "/network/update"): network_update,
    ("GET", "/network/summary"): network_summary,
    ("GET", "/network/history"): network_history,
    ("POST", "/network/history"): network_history,
    ("GET", "/network/stats"): network_stats,
    ("POST", "/network/stats"): network_stats,
}

BODY_ROUTES = {
    ("POST", "/network/update"),
    ("POST", "/kodi/play-path"),
    ("POST", "/kodi/sync-library"),
    ("POST", "/rutracker/login"),
    ("POST", "/network/inventory"),
    ("POST", "/network/scan"),
    ("POST", "/network/pin"),
    ("POST", "/network/unpin"),
    ("POST", "/network/pin-replace"),
    ("POST", "/network/alias"),
    ("POST", "/network/history"),
    ("POST", "/network/stats"),
}
QUERY_ROUTES = {
    ("GET", "/network/inventory"),
    ("GET", "/network/summary"),
    ("GET", "/network/history"),
    ("GET", "/network/stats"),
}


class Handler(BaseHTTPRequestHandler):
    token = read_secret(TOKEN_FILE)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def authorized(self) -> bool:
        supplied = self.headers.get("X-HA-Host-Token", "")
        if not self.token:
            return False
        return hmac.compare_digest(supplied, self.token)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def handle_route(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        route_key = (self.command, url.path)
        route = ROUTES.get(route_key)
        if route is None:
            self.send_json(404, {"ok": False, "error": "not_found"})
            return
        if not self.authorized():
            self.send_json(403, {"ok": False, "error": "forbidden"})
            return
        try:
            if route_key in BODY_ROUTES:
                result = route(self.read_json_body())
            elif route_key in QUERY_ROUTES:
                result = route(urllib.parse.parse_qs(url.query))
            else:
                result = route()
            self.send_json(200, {"ok": True, **result})
        except InvalidInput as exc:
            # Validation errors (bad MAC/IP/filter/...) are the caller's fault.
            self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - keep bridge alive and report error to HA.
            self.send_json(500, {"ok": False, "error": repr(exc)})

    def do_GET(self) -> None:
        self.handle_route()

    def do_POST(self) -> None:
        self.handle_route()


def main() -> None:
    bind = os.environ.get("HA_HOST_ACTIONS_BIND", "172.20.0.1")
    port = int(os.environ.get("HA_HOST_ACTIONS_PORT", "8787"))
    server = ThreadingHTTPServer((bind, port), Handler)
    print(f"ha-host-actions listening on {bind}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
