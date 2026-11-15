"""Select fresh verified sources around durable Kodi repeat ownership."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Callable

REQUEST_ID = re.compile(r"[A-Za-z0-9_.:+-]{1,96}\Z")
SOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
VIRTUAL_FILE = "/__ha_verified_fireplace__"
MINIMUM_CATALOG_SOURCES = 2
OBSERVATION_MAX_AGE_SECONDS = 5.0
PROGRESS_MAX_AGE_SECONDS = 5.0


def utc() -> str:
    """Return the current UTC instant in the receipt timestamp format."""
    return datetime.now(UTC).isoformat()


def file_hash(value: str) -> str:
    """Hash a native file identity without Kodi transport-header suffixes."""
    return hashlib.sha256(value.split("|", 1)[0].encode()).hexdigest()


def seconds(value: dict[str, Any]) -> float:
    """Convert a validated Kodi playback clock to elapsed seconds."""
    parts = [
        value.get(key, 0) for key in ("hours", "minutes", "seconds", "milliseconds")
    ]
    if any(type(part) not in {int, float} or part < 0 for part in parts):
        error_message = "invalid_playback_time"
        raise ValueError(error_message)
    return parts[0] * 3600 + parts[1] * 60 + parts[2] + parts[3] / 1000


def catalog_item(source: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one fixed catalog entry."""
    item = dict(source)
    if not SOURCE_ID.fullmatch(str(item.get("id", ""))):
        error_message = "invalid_source_id"
        raise ValueError(error_message)
    if item.get("kind") == "local":
        name = item.get("filename", "")
        if Path(name).name != name or not HASH.fullmatch(str(item.get("sha256", ""))):
            error_message = "invalid_local_source"
            raise ValueError(error_message)
        item["path"] = str(Path.home() / "KodiLibrary/Ambience" / name)
    elif item.get("kind") != "youtube" or not YOUTUBE_ID.fullmatch(
        str(item.get("youtube_id", ""))
    ):
        error_message = "invalid_youtube_source"
        raise ValueError(error_message)
    return item


def catalog(
    media_path: Path | str | None, expected_sha256: str | None
) -> list[dict[str, Any]]:
    """Load the source catalog or the explicitly supplied legacy asset."""
    if media_path is not None:
        return [
            {
                "id": "local-single",
                "kind": "local",
                "path": str(media_path),
                "sha256": expected_sha256,
                "title": Path(media_path).name,
            }
        ]
    data = json.loads(Path(__file__).with_name("kodi_fireplaces.json").read_text())
    if data.get("version") != 1 or not isinstance(data.get("sources"), list):
        error_message = "invalid_catalog"
        raise ValueError(error_message)
    result = [catalog_item(source) for source in data["sources"]]
    if (
        len({item["id"] for item in result}) != len(result)
        or len(result) < MINIMUM_CATALOG_SOURCES
    ):
        error_message = "invalid_catalog"
        raise ValueError(error_message)
    return result


class SourceUnavailableError(Exception):
    """Preparation failed without sending a playback command."""


# Keep the public exception import used by existing callers.
SourceUnavailable = SourceUnavailableError


def resolve_local(
    source: dict[str, Any], deadline: float, cancelled: threading.Event
) -> dict[str, Any]:
    """Verify the local asset digest within its cancellation deadline."""
    digest = hashlib.sha256()
    try:
        with Path(source["path"]).open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                if time.monotonic() >= deadline or cancelled.is_set():
                    error_message = "asset_deadline"
                    raise SourceUnavailable(error_message)
                digest.update(chunk)
    except OSError as error:
        error_message = "asset_unavailable"
        raise SourceUnavailable(error_message) from error
    if digest.hexdigest() != source["sha256"]:
        error_message = "asset_unverified"
        raise SourceUnavailable(error_message)
    return {"file": str(source["path"]), "title": source.get("title")}


def resolver_output(
    process: subprocess.Popen[str], deadline: float, cancelled: threading.Event
) -> str:
    """Collect one resolver result while observing the shared deadline."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or cancelled.is_set():
            error_message = "youtube_deadline_or_cancelled"
            raise SourceUnavailable(error_message)
        try:
            output, _ = process.communicate(timeout=min(0.2, remaining))
        except subprocess.TimeoutExpired:
            continue
        else:
            if process.returncode:
                error_message = "youtube_unavailable"
                raise SourceUnavailable(error_message)
            return output


def reap_resolver(process: subprocess.Popen[str]) -> None:
    """Kill and reap the resolver process group and close its pipes."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired as error:
        error_message = "resolver_cleanup_unconfirmed"
        raise SourceUnavailable(error_message) from error
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def resolve_source(
    source: dict[str, Any],
    deadline: float,
    attempt_id: str,
    cancelled: threading.Event | None = None,
) -> dict[str, Any]:
    """Prepare a catalog source with a bounded, isolated resolver process."""
    cancelled = cancelled or threading.Event()
    if source["kind"] == "local":
        return resolve_local(source, deadline, cancelled)
    script = Path(__file__).with_name("ha_kodi_youtube_resolve.py")
    process = None
    try:
        command = [
            sys.executable,
            str(script),
            "--video-id",
            source["youtube_id"],
            "--request-id",
            attempt_id,
        ]
        # Fixed helper, validated catalog ID and generated attempt UUID; no shell.
        process = subprocess.Popen(  # noqa: S603
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        target = json.loads(
            resolver_output(process, min(deadline, time.monotonic() + 20), cancelled)
        )
        if not isinstance(target, dict) or not isinstance(target.get("file"), str):
            error_message = "youtube_invalid_result"
            raise SourceUnavailable(error_message)
    except (OSError, ValueError) as error:
        error_message = "youtube_unavailable"
        raise SourceUnavailable(error_message) from error
    else:
        return target
    finally:
        if process is not None:
            try:
                reap_resolver(process)
            except OSError as error:
                error_message = "resolver_cleanup_unconfirmed"
                raise SourceUnavailable(error_message) from error


class RotatingFireplaceController:
    """Coordinate source selection, bounded opening and durable ownership proof."""

    # Preserve the established callback and configuration injection API.
    def __init__(  # noqa: PLR0913
        self,
        owner_factory: Callable[..., Any],
        error_type: type[Exception],
        rpc: Callable[[str, dict[str, Any], float], Any],
        receipt_path: Path,
        *,
        state_writer: Callable[[Path, dict[str, Any]], None],
        rejected_error_type: type[Exception] | None = None,
        sources: list[dict[str, Any]] | None = None,
        resolver: Callable[[dict[str, Any], float], dict[str, Any]] | None = None,
        media_path: Path | str | None = None,
        expected_sha256: str | None = None,
        auto_monitor: bool = True,
        operation_timeout: float = 50.0,
        poll_interval: float = 0.1,
        progress_interval: float = 0.25,
        chooser: Callable[[list[dict[str, Any]]], None] | None = None,
        **owner_options: Any,
    ) -> None:
        """Bind the transport, persistence and repeat-owner dependencies."""
        self.Error = error_type
        self.NotDispatched = rejected_error_type or error_type
        self._rpc = rpc
        self._write_state = state_writer
        self._receipt = Path(receipt_path)
        self._state_path = self._receipt.with_name("fireplace-selection.json")
        self._generation = self._receipt.parent / "youtube/current-request.json"
        self._sources = (
            list(sources)
            if sources is not None
            else catalog(media_path, expected_sha256)
        )
        self._ids = {source["id"] for source in self._sources}
        self._resolver = resolver
        self._shuffle = chooser or random.SystemRandom().shuffle
        self._timeout, self._poll = operation_timeout, poll_interval
        self._progress_interval, self._auto_monitor = progress_interval, auto_monitor
        self._lock, self._snapshot_lock = threading.RLock(), threading.Lock()
        self._cancel_lock = threading.Lock()
        self._closing, self._cancelled = threading.Event(), threading.Event()
        self._thread = None
        self._active = self._pending = self._matched = self._target = None
        self._last_source = self._load_error = None
        self._last_time = self._last_title = self._progress = None
        self._failed_request = self._launching = self._opening = False
        self._launch_admitted = False
        self._fallback_repeat = None
        self._stop_requested = False
        self._intent_seq = 0
        self._snapshot = self._empty()
        self._load_generation()
        self._load_selection(media_path)
        self._owner = owner_factory(
            self._rpc_adapter,
            self._receipt,
            media_path=VIRTUAL_FILE,
            expected_sha256="",
            auto_monitor=False,
            operation_timeout=min(8.0, operation_timeout),
            poll_interval=poll_interval,
            asset_check=self._asset_check,
            **owner_options,
        )
        if self._owner.status().get("error") == "owner_unreadable":
            self._load_error = "selection_unreadable"
        if self._load_error:
            self._snapshot.update(state="error", error=self._load_error)
        elif self._owner.status()["owned"]:
            self._snapshot.update(state="recovering", owned=True)

    @staticmethod
    def _require_valid_generation(cancelled: object, sequence: object) -> None:
        if type(cancelled) is not bool or type(sequence) is not int or sequence < 0:
            error_message = "invalid_generation"
            raise ValueError(error_message)

    def _load_generation(self) -> None:
        try:
            if not self._generation.exists():
                return
            value = json.loads(self._generation.read_text())
            cancelled, sequence = (
                value.get("cancelled", False),
                value.get("intent_seq", 0),
            )
            self._require_valid_generation(cancelled, sequence)
            self._stop_requested, self._intent_seq = cancelled, sequence
            if cancelled:
                self._cancelled.set()
        except (OSError, ValueError, TypeError, AttributeError):
            self._load_error = "selection_unreadable"

    def _validate_identity(
        self, value: object, *, pending: bool = False
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict) or value.get("source_id") not in self._ids:
            error_message = "invalid_selection"
            raise ValueError(error_message)
        preparing = pending and value.get("phase") == "preparing"
        if not preparing and not HASH.fullmatch(str(value.get("file_hash", ""))):
            error_message = "invalid_selection"
            raise ValueError(error_message)
        if value.get("open_hash") is not None and not HASH.fullmatch(
            str(value["open_hash"])
        ):
            error_message = "invalid_selection"
            raise ValueError(error_message)
        return value

    def _read_selection(
        self,
    ) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
        state = json.loads(self._state_path.read_text())
        if not isinstance(state, dict) or state.get("version") != 1:
            error_message = "invalid_selection"
            raise ValueError(error_message)
        last = state.get("last_successful_source_id")
        if last is not None and not SOURCE_ID.fullmatch(last):
            error_message = "invalid_history"
            raise ValueError(error_message)
        active = self._validate_identity(state.get("active"))
        pending = self._validate_identity(state.get("pending"), pending=True)
        if pending is not None and pending.get("phase") not in {
            "preparing",
            "prepared",
            "opening",
        }:
            error_message = "invalid_pending"
            raise ValueError(error_message)
        return last, active, pending

    def _load_selection(self, media_path: Path | str | None) -> None:
        try:
            if self._state_path.exists():
                self._last_source, self._active, self._pending = self._read_selection()
            elif self._receipt.exists() and media_path is None:
                self._load_legacy()
        except (OSError, ValueError, TypeError, AttributeError):
            self._load_error = "selection_unreadable"

    def _load_legacy(self) -> None:
        legacy = next(
            (item for item in self._sources if item["id"] == "local-fastily"), None
        )
        if legacy is not None:
            self._active = {
                "source_id": legacy["id"],
                "source_kind": "local",
                "file_hash": file_hash(legacy["path"]),
                "request_id": None,
                "launch_started_at": None,
                "title": legacy["title"],
            }
            self._last_source = legacy["id"]

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "state": "idle",
            "owned": False,
            "file_matches": False,
            "repeat": None,
            "observed_at": None,
            "error": None,
            "source_id": None,
            "source_kind": None,
            "request_id": None,
            "launch_started_at": None,
            "title": None,
            "progressing": False,
        }

    def _publish(self, value: dict[str, Any]) -> dict[str, Any]:
        with self._snapshot_lock:
            self._snapshot = dict(value)
        return dict(value)

    def status(self) -> dict[str, Any]:
        """Return the published receipt while rejecting stale playback evidence."""
        with self._snapshot_lock:
            value = dict(self._snapshot)
        stamp = value.get("observed_at")
        if value["state"] in {"playing", "paused"} and (
            not stamp
            or (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
            > OBSERVATION_MAX_AGE_SECONDS
        ):
            value.update(state="unknown", progressing=False, error="stale_observation")
        return value

    def _store(self) -> None:
        if self._load_error:
            raise self.Error(self._load_error)
        self._write_state(
            self._state_path,
            {
                "version": 1,
                "last_successful_source_id": self._last_source,
                "active": self._active,
                "pending": self._pending,
            },
        )

    def _asset_check(self) -> None:
        if (
            not self._target
            or not self._pending
            or file_hash(self._target["file"]) != self._pending["file_hash"]
        ):
            error_message = "selection_unverified"
            raise self.Error(error_message)

    def _check_cancelled(self) -> None:
        if self._closing.is_set() or self._cancelled.is_set():
            error_message = "cancelled"
            raise self.Error(error_message)

    def _write_generation(self, attempt_id: str) -> None:
        self._write_state(
            self._generation,
            {
                "request_id": attempt_id,
                "cancelled": self._stop_requested,
                "intent_seq": self._intent_seq,
            },
        )

    def _set_generation(self, attempt_id: str) -> None:
        with self._cancel_lock:
            self._check_cancelled()
            self._write_generation(attempt_id)

    def _cancel_attempt(self) -> None:
        with self._cancel_lock:
            self._write_generation(str(uuid4()))

    def _check_deadline(self, deadline: float) -> None:
        self._check_cancelled()
        if time.monotonic() >= deadline:
            error_message = "deadline"
            raise self.Error(error_message)

    def _reset_prepared_open(self) -> None:
        self._opening = False
        self._pending["phase"] = "prepared"
        self._store()

    def _open_candidate(self, params: dict[str, Any], deadline: float) -> Any:
        self._check_cancelled()
        self._asset_check()
        if params != {"item": {"file": VIRTUAL_FILE}}:
            error_message = "invalid_open"
            raise self.Error(error_message)
        self._pending["phase"] = "opening"
        self._store()
        try:
            self._check_deadline(deadline)
            self._check_fallback_intent(deadline)
            self._check_deadline(deadline)
        except Exception:
            self._reset_prepared_open()
            raise
        self._opening = True
        try:
            return self._rpc(
                "Player.Open",
                {"item": {"file": self._target.get("open_file", self._target["file"])}},
                deadline,
            )
        except self.NotDispatched:
            self._reset_prepared_open()
            raise

    def _check_fallback_intent(self, deadline: float) -> None:
        if self._fallback_repeat is None:
            return
        if self._rpc("Player.GetActivePlayers", {}, deadline) != []:
            error_message = "playback_changed"
            raise self.Error(error_message)
        mode = self._rpc(
            "Player.GetProperties", {"playerid": 1, "properties": ["repeat"]}, deadline
        )
        if not isinstance(mode, dict) or mode.get("repeat") != self._fallback_repeat:
            error_message = "playback_changed"
            raise self.Error(error_message)

    def _identities(self) -> list[dict[str, Any] | None]:
        if self._launching and self._opening and self._pending is not None:
            return [self._pending]
        values = [self._active]
        if self._pending is not None and self._pending.get("phase") == "opening":
            values.append(self._pending)
        return values

    def _match_item(self, result: Any) -> Any:
        if not isinstance(result, dict) or not isinstance(result.get("item"), dict):
            return result
        item = dict(result["item"])
        raw_file = item.get("file")
        if not isinstance(raw_file, str) or not raw_file:
            self._matched = None
            return result
        digest = file_hash(raw_file)
        self._matched = next(
            (
                value
                for value in self._identities()
                if value is not None
                and digest is not None
                and digest in {value.get("file_hash"), value.get("open_hash")}
            ),
            None,
        )
        if self._matched is not None:
            title = item.get("title") or item.get("label")
            self._last_title = title[:512] if isinstance(title, str) else None
        item["file"] = (
            VIRTUAL_FILE if self._matched is not None else "/__unrelated_media__"
        )
        return {**result, "item": item}

    def _rpc_adapter(self, method: str, params: dict[str, Any], deadline: float) -> Any:
        if self._closing.is_set():
            error_message = "stopped"
            raise self.NotDispatched(error_message)
        params = dict(params)
        if method == "Player.Open":
            return self._open_candidate(params, deadline)
        if method == "Player.GetProperties" and "speed" in params.get("properties", []):
            params["properties"] = list(dict.fromkeys([*params["properties"], "time"]))
        result = self._rpc(method, params, deadline)
        if method == "Player.GetItem":
            return self._match_item(result)
        if (
            method == "Player.GetProperties"
            and isinstance(result, dict)
            and "time" in result
        ):
            self._last_time = seconds(result["time"])
        return result

    def _progressing(self, proof: dict[str, Any]) -> bool:
        if (
            proof["state"] != "playing"
            or not proof["file_matches"]
            or self._last_time is None
        ):
            self._progress = None
            return False
        now = time.monotonic()
        identity = tuple(
            (self._matched or {}).get(key)
            for key in ("source_id", "attempt_id", "file_hash")
        )
        previous = self._progress
        if previous is None or previous[0] != identity:
            self._progress = (identity, self._last_time, None)
            return False
        _, position, changed = previous
        if abs(self._last_time - position) >= self._progress_interval:
            self._progress = (identity, self._last_time, now)
            return True
        return changed is not None and now - changed <= PROGRESS_MAX_AGE_SECONDS

    def _proof(self, proof: dict[str, Any]) -> dict[str, Any]:
        value = {**self._empty(), **proof}
        identity = self._matched if proof["file_matches"] else self._active
        if identity is not None:
            value.update(
                {
                    key: identity.get(key)
                    for key in (
                        "source_id",
                        "source_kind",
                        "request_id",
                        "launch_started_at",
                    )
                }
            )
            if proof["file_matches"]:
                value["title"] = self._last_title or identity.get("title")
        value["progressing"] = self._progressing(proof)
        if value["state"] == "playing" and not value["progressing"]:
            value.update(state="recovering", error="progress_unconfirmed")
        return value

    def _candidates(self) -> list[dict[str, Any]]:
        excluded = {self._last_source}
        excluded.update(
            value["source_id"]
            for value in (self._active, self._pending)
            if value is not None
        )
        groups = [
            [
                item
                for item in self._sources
                if item["kind"] == kind
                and item["id"] not in excluded
                and item.get("selectable", True) is True
            ]
            for kind in ("youtube", "local")
        ]
        for group in groups:
            self._shuffle(group)
        return groups[0][:2] + groups[1]

    def _restore_active_generation(self) -> None:
        if self._opening or self._load_error or self._active is None:
            return
        with self._cancel_lock:
            attempt_id = self._active.get("attempt_id")
            if attempt_id and not self._cancelled.is_set():
                self._write_generation(attempt_id)

    def _failure(
        self, request: dict[str, Any], code: str, *, uncertain: bool = False
    ) -> None:
        if not uncertain:
            self._restore_active_generation()
            if self._pending is not None and self._pending.get("phase") in {
                "preparing",
                "prepared",
            }:
                self._pending = None
                self._store()
        self._failed_request = True
        value = self._empty()
        value.update(
            **request,
            state="unknown" if uncertain else "error",
            error=code,
            owned=self._owner.status()["owned"],
        )
        self._publish(value)
        self.start_monitor()

    def _prepare(
        self, source: dict[str, Any], request: dict[str, Any], deadline: float
    ) -> None:
        self._check_deadline(deadline)
        attempt_id = str(uuid4())
        # Recovery must know this is pre-Open before the generation changes.
        self._pending = {
            **request,
            "source_id": source["id"],
            "source_kind": source["kind"],
            "attempt_id": attempt_id,
            "phase": "preparing",
        }
        self._store()
        self._set_generation(attempt_id)
        resolve_deadline = min(
            deadline - min(3.0, self._timeout / 3), time.monotonic() + 20
        )
        target = (
            self._resolver(source, resolve_deadline)
            if self._resolver
            else resolve_source(source, resolve_deadline, attempt_id, self._cancelled)
        )
        self._check_deadline(deadline)
        if not isinstance(target, dict) or not isinstance(target.get("file"), str):
            error_message = "invalid_target"
            raise self.Error(error_message)
        self._target = target
        self._pending.update(
            file_hash=file_hash(target["file"]),
            open_hash=file_hash(target.get("open_file", target["file"])),
            title=target.get("title") or source.get("title"),
            phase="prepared",
        )
        self._store()
        self._progress = self._last_time = self._last_title = None
        self._opening = False

    def _open_owned(self, deadline: float) -> dict[str, Any]:
        self._check_deadline(deadline)
        try:
            return self._owner.start(force_new=True, deadline=deadline)
        except Exception as error:
            if str(error) == "playback_changed":
                self._active = self._pending = None
                self._store()
                raise
            self._cancel_attempt()
            self._check_cancelled()
            if not self._opening:
                error_message = "sources_unavailable"
                raise self.Error(error_message) from error
            error_message = "open_unconfirmed"
            raise self.Error(error_message) from error

    def _promote(self) -> None:
        self._active = {
            key: value for key, value in self._pending.items() if key != "phase"
        }
        self._last_source = self._active["source_id"]
        self._pending = None
        self._store()

    def _release_superseded(self, proof: dict[str, Any]) -> bool:
        if proof.get("repeat_superseded") is not True:
            return False
        self._active = self._pending = None
        self._store()
        return True

    def _require_progress_ownership(self, proof: dict[str, Any]) -> None:
        if self._release_superseded(proof):
            error_message = "playback_changed"
            raise self.Error(error_message)
        if not proof["owned"]:
            self._cancel_attempt()
            if proof.get("active") is not True:
                # Absence is not proof that a delayed native Open was cancelled.
                error_message = "open_unconfirmed"
                raise self.Error(error_message)
            self._active = self._pending = None
            self._store()
            error_message = "playback_changed"
            raise self.Error(error_message)

    def _await_progress(
        self, proof: dict[str, Any], deadline: float
    ) -> dict[str, Any] | None:
        progress_deadline = min(deadline, time.monotonic() + 3)
        while time.monotonic() < progress_deadline:
            self._require_progress_ownership(proof)
            self._check_cancelled()
            value = self._proof(proof)
            if (
                value["state"] == "playing"
                and value["progressing"]
                and self._matched is self._pending
            ):
                self._promote()
                with self._cancel_lock:
                    self._check_cancelled()
                    self._publish(value)
                self.start_monitor()
                return value
            if self._closing.wait(self._poll):
                error_message = "cancelled"
                raise self.Error(error_message)
            proof = self._owner.observe(deadline=progress_deadline)
        self._require_progress_ownership(proof)
        self._cancel_attempt()
        try:
            stopped = self._owner.stop(deadline=deadline + 8.0)
        except Exception as error:
            error_message = "stop_unconfirmed"
            raise self.Error(error_message) from error
        if self._release_superseded(stopped):
            error_message = "playback_changed"
            raise self.Error(error_message)
        if stopped["owned"] or stopped.get("stopped") is not True:
            error_message = "stop_unconfirmed"
            raise self.Error(error_message)
        self._fallback_repeat = stopped["repeat"]
        self._active = self._pending = None
        self._store()
        return None

    def _run_candidates(
        self,
        candidates: list[dict[str, Any]],
        request: dict[str, Any],
        starting: dict[str, Any],
        deadline: float,
    ) -> dict[str, Any]:
        for source in candidates:
            self._check_cancelled()
            if time.monotonic() >= deadline:
                break
            try:
                self._prepare(source, request, deadline)
                opened = self._open_owned(deadline)
            except SourceUnavailable:
                self._cancel_attempt()
                continue
            value = self._await_progress(opened, deadline)
            if value is not None:
                return value
            self._publish(starting)
        self._check_cancelled()
        error_message = "sources_unavailable"
        raise self.Error(error_message)

    def start(self, request_id: str | None = None) -> dict[str, Any]:
        """Admit one explicit command and select a distinct progressing source."""
        request_id = uuid4().hex if request_id is None else request_id
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            error_message = "invalid_request_id"
            raise self.Error(error_message)
        deadline = time.monotonic() + self._timeout
        # Only another admitted launch is busy. A bounded monitor/stop operation
        # may finish first; it must not randomly reject the next voice command.
        with self._cancel_lock:
            if self._launch_admitted:
                error_message = "busy"
                raise self.Error(error_message)
            self._launch_admitted = True
            self._intent_seq += 1
            self._stop_requested = False
            self._cancelled.clear()
        try:
            with self._lock:
                return self._start_request(
                    {"request_id": request_id, "launch_started_at": utc()}, deadline
                )
        finally:
            with self._cancel_lock:
                self._launch_admitted = False

    def _start_request(
        self, request: dict[str, Any], deadline: float
    ) -> dict[str, Any]:
        # Keep admitted errors inside correlated failure publication.
        try:
            if self._closing.is_set() or self._load_error:
                raise self.Error(self._load_error or "stopped")  # noqa: TRY301
            candidates = self._candidates()
            self._failed_request = False
            self._fallback_repeat = None
            self._launching, self._opening = True, False
            starting = {**self._empty(), **request, "state": "starting"}
            self._publish(starting)
            if not candidates:
                error_message = "no_distinct_source"
                raise self.Error(error_message)  # noqa: TRY301
            self._pending = None
            self._store()
            return self._run_candidates(candidates, request, starting, deadline)
        except self.Error as error:
            self._failure(
                request,
                str(error),
                uncertain=str(error)
                in {
                    "open_unconfirmed",
                    "stop_unconfirmed",
                    "deadline",
                    "cancelled",
                    "playback_changed",
                },
            )
            raise
        except Exception as error:
            self._failure(request, "start_unconfirmed", uncertain=True)
            error_message = "start_unconfirmed"
            raise self.Error(error_message) from error
        finally:
            self._launching = self._opening = False

    def _recover_prepared(self) -> None:
        if self._pending is None or self._pending.get("phase") not in {
            "preparing",
            "prepared",
        }:
            return
        with self._cancel_lock:
            if not self._cancelled.is_set():
                previous_attempt = (
                    self._active.get("attempt_id") if self._active else None
                )
                self._write_generation(previous_attempt or str(uuid4()))
        self._pending = None
        self._store()

    def _finish_observation(self, proof: dict[str, Any]) -> dict[str, Any]:
        if not self._settlement_still_current(proof):
            return self._unresolved_opening()
        value = self._proof(proof)
        if (
            not self._cancelled.is_set()
            and self._pending is not None
            and self._matched is self._pending
            and value["state"] == "playing"
            and value["progressing"]
        ):
            self._promote()
        if (
            not proof["owned"]
            and proof["state"] != "unknown"
            and (self._active is not None or self._pending is not None)
        ):
            self._active = self._pending = None
            self._store()
        with self._cancel_lock:
            if (self._failed_request or self._cancelled.is_set()) and value["owned"]:
                previous = self.status()
                previous.update(owned=True, progressing=False)
                return self._publish(previous)
            return self._publish(value)

    def _cleanup_identity(self) -> dict[str, Any] | None:
        if self._pending is not None and self._pending.get("phase") == "opening":
            return self._pending
        return self._active if self._stop_requested else None

    def _fence_identity(self, identity: dict[str, Any]) -> None:
        with self._cancel_lock:
            current = json.loads(self._generation.read_text())
            if current.get("request_id") == identity.get("attempt_id"):
                self._write_generation(str(uuid4()))

    def _settle_unleased(self, proof: dict[str, Any]) -> bool:
        if self._release_superseded(proof):
            return True
        identity = self._cleanup_identity()
        if identity is None or proof["owned"]:
            return True
        self._fence_identity(identity)
        if proof.get("stopped") is True and self._matched is identity:
            return True
        if (
            identity is self._active
            and proof["state"] != "unknown"
            and not proof["file_matches"]
        ):
            return True
        return self._stop_unleased_candidate(identity)

    def _settlement_still_current(self, proof: dict[str, Any]) -> bool:
        return (
            self._cleanup_identity() is None
            or proof["owned"]
            or (proof["state"] != "unknown" and not proof["file_matches"])
        )

    def _unresolved_opening(self) -> dict[str, Any]:
        value = self.status()
        identity = self._pending or self._active or {}
        value.update(
            {
                key: identity.get(key)
                for key in (
                    "request_id",
                    "launch_started_at",
                    "source_id",
                    "source_kind",
                )
            }
        )
        value.update(
            state="unknown",
            owned=False,
            file_matches=False,
            progressing=False,
            observed_at=None,
            error="stop_unconfirmed",
        )
        return self._publish(value)

    def observe(self) -> dict[str, Any]:
        """Publish current ownership without blocking an admitted launch."""
        if self._load_error or not self._lock.acquire(blocking=False):
            return self.status()
        try:
            self._recover_prepared()
            proof = (
                self._owner.stop()
                if self._stop_requested and self._owner.status()["owned"]
                else self._owner.observe()
            )
            if not self._settle_unleased(proof):
                return self._unresolved_opening()
            if self._cleanup_identity() is not None and not proof["owned"]:
                proof = self._owner.observe()
            return self._finish_observation(proof)
        # Contain every callback failure as bounded, non-secret evidence.
        except Exception:  # noqa: BLE001
            value = self.status()
            value.update(
                state="unknown",
                progressing=False,
                error="observation_failed",
                observed_at=None,
            )
            return self._publish(value)
        finally:
            self._lock.release()

    def _recovery_rpc(
        self, method: str, params: dict[str, Any], deadline: float
    ) -> Any:
        if self._closing.is_set():
            error_message = "stopped"
            raise self.Error(error_message)
        return self._rpc(method, params, deadline)

    def _stop_unleased_candidate(self, identity: dict[str, Any]) -> bool:
        if self._owner.status()["owned"]:
            return False
        deadline = time.monotonic() + 3
        players = self._recovery_rpc("Player.GetActivePlayers", {}, deadline)
        if (
            not isinstance(players, list)
            or len(players) != 1
            or not isinstance(players[0], dict)
            or players[0].get("playerid") != 1
            or players[0].get("type") != "video"
        ):
            return False
        item = self._recovery_rpc(
            "Player.GetItem", {"playerid": 1, "properties": ["file"]}, deadline
        )
        raw = item.get("item", {}).get("file")
        expected = {identity.get("file_hash"), identity.get("open_hash")}
        if not isinstance(raw, str) or file_hash(raw) not in expected:
            return False
        self._recovery_rpc("Player.Stop", {"playerid": 1}, deadline)
        remaining = self._recovery_rpc("Player.GetActivePlayers", {}, deadline)
        if not isinstance(remaining, list) or remaining:
            error_message = "stop_unconfirmed"
            raise self.Error(error_message)

        return True

    def stop(self) -> dict[str, Any]:
        """Cancel preparation and clean up only the current owned attempt."""
        if self._load_error:
            raise self.Error(self._load_error)
        with self._cancel_lock:
            self._intent_seq += 1
            sequence = self._intent_seq
            self._stop_requested = True
            self._cancelled.set()
            self._write_generation(str(uuid4()))
            value = self.status()
            value.update(
                state="unknown",
                progressing=False,
                observed_at=None,
                error="stop_pending",
            )
            self._publish(value)
        with self._lock:
            with self._cancel_lock:
                if sequence != self._intent_seq:
                    return self.status()
            # Keep native cleanup errors inside the durable failure receipt path.
            try:
                proof = self._owner.stop()
                if not self._settle_unleased(proof):
                    error_message = "stop_unconfirmed"
                    raise self.Error(error_message)  # noqa: TRY301
                if self._cleanup_identity() is not None and not proof["owned"]:
                    proof = self._owner.observe()
                if not self._settlement_still_current(proof):
                    error_message = "stop_unconfirmed"
                    raise self.Error(error_message)  # noqa: TRY301
                self._failed_request = False
                if not proof["owned"]:
                    self._active = self._pending = None
                    self._store()
                self._progress = None
                return self._publish(self._proof(proof))
            except Exception as error:
                request = {
                    key: value.get(key) for key in ("request_id", "launch_started_at")
                }
                self._failure(request, "stop_unconfirmed", uncertain=True)
                error_message = "stop_unconfirmed"
                raise self.Error(error_message) from error

    def _recovery_pending(self) -> bool:
        return (
            self._owner.status()["owned"]
            or self._pending is not None
            or self._active is not None
        )

    def start_monitor(self) -> None:
        """Start one observer while any durable recovery work remains."""
        with self._lock:
            if (
                not self._auto_monitor
                or self._closing.is_set()
                or self._load_error
                or not self._recovery_pending()
                or (self._thread is not None and self._thread.is_alive())
            ):
                return
            self._thread = threading.Thread(
                target=self._monitor, name="kodi-fireplace-rotation", daemon=True
            )
            self._thread.start()

    def _monitor(self) -> None:
        try:
            while not self._closing.is_set():
                self.observe()
                if not self._recovery_pending() or self._closing.wait(1):
                    return
        finally:
            with self._lock:
                self._thread = None
                self.start_monitor()

    def close(self) -> None:
        """Cancel local work while retaining durable recovery state."""
        self._closing.set()
        self._cancelled.set()
        self._owner.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=4)
