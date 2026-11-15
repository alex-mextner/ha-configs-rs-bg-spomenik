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
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

REQUEST_ID = re.compile(r"[A-Za-z0-9_.:+-]{1,96}\Z")
SOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
VIRTUAL_FILE = "/__ha_verified_fireplace__"


def utc():
    return datetime.now(timezone.utc).isoformat()


def file_hash(value):
    return hashlib.sha256(value.split("|", 1)[0].encode()).hexdigest()


def seconds(value):
    parts = [
        value.get(key, 0) for key in ("hours", "minutes", "seconds", "milliseconds")
    ]
    if any(type(part) not in {int, float} or part < 0 for part in parts):
        raise ValueError("invalid_playback_time")
    return parts[0] * 3600 + parts[1] * 60 + parts[2] + parts[3] / 1000


def catalog_item(source):
    item = dict(source)
    if not SOURCE_ID.fullmatch(str(item.get("id", ""))):
        raise ValueError("invalid_source_id")
    if item.get("kind") == "local":
        name = item.get("filename", "")
        if Path(name).name != name or not HASH.fullmatch(str(item.get("sha256", ""))):
            raise ValueError("invalid_local_source")
        item["path"] = str(Path.home() / "KodiLibrary/Ambience" / name)
    elif item.get("kind") != "youtube" or not YOUTUBE_ID.fullmatch(
        str(item.get("youtube_id", ""))
    ):
        raise ValueError("invalid_youtube_source")
    return item


def catalog(media_path, expected_sha256):
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
        raise ValueError("invalid_catalog")
    result = [catalog_item(source) for source in data["sources"]]
    if len({item["id"] for item in result}) != len(result) or len(result) < 2:
        raise ValueError("invalid_catalog")
    return result


class SourceUnavailable(Exception):
    """Preparation failed without sending a playback command."""


def resolve_local(source, deadline, cancelled):
    digest = hashlib.sha256()
    try:
        with Path(source["path"]).open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                if time.monotonic() >= deadline or cancelled.is_set():
                    raise SourceUnavailable("asset_deadline")
                digest.update(chunk)
    except OSError as error:
        raise SourceUnavailable("asset_unavailable") from error
    if digest.hexdigest() != source["sha256"]:
        raise SourceUnavailable("asset_unverified")
    return {"file": str(source["path"]), "title": source.get("title")}


def resolver_output(process, deadline, cancelled):
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or cancelled.is_set():
            raise SourceUnavailable("youtube_deadline_or_cancelled")
        try:
            output, _ = process.communicate(timeout=min(0.2, remaining))
            if process.returncode:
                raise SourceUnavailable("youtube_unavailable")
            return output
        except subprocess.TimeoutExpired:
            continue


def reap_resolver(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired as error:
        raise SourceUnavailable("resolver_cleanup_unconfirmed") from error
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def resolve_source(source, deadline, attempt_id, cancelled=None):
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
        process = subprocess.Popen(
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
            raise SourceUnavailable("youtube_invalid_result")
        return target
    except (OSError, ValueError) as error:
        raise SourceUnavailable("youtube_unavailable") from error
    finally:
        if process is not None:
            try:
                reap_resolver(process)
            except OSError as error:
                raise SourceUnavailable("resolver_cleanup_unconfirmed") from error


class RotatingFireplaceController:
    def __init__(
        self,
        owner_factory,
        error_type,
        rpc,
        receipt_path,
        *,
        state_writer,
        rejected_error_type=None,
        sources=None,
        resolver=None,
        media_path=None,
        expected_sha256=None,
        auto_monitor=True,
        operation_timeout=50.0,
        poll_interval=0.1,
        progress_interval=0.25,
        chooser=None,
        **owner_options,
    ):
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

    def _load_generation(self):
        try:
            if not self._generation.exists():
                return
            value = json.loads(self._generation.read_text())
            cancelled, sequence = (
                value.get("cancelled", False),
                value.get("intent_seq", 0),
            )
            if type(cancelled) is not bool or type(sequence) is not int or sequence < 0:
                raise ValueError("invalid_generation")
            self._stop_requested, self._intent_seq = cancelled, sequence
            if cancelled:
                self._cancelled.set()
        except (OSError, ValueError, TypeError, AttributeError):
            self._load_error = "selection_unreadable"

    def _validate_identity(self, value, *, pending=False):
        if value is None:
            return None
        if not isinstance(value, dict) or value.get("source_id") not in self._ids:
            raise ValueError("invalid_selection")
        preparing = pending and value.get("phase") == "preparing"
        if not preparing and not HASH.fullmatch(str(value.get("file_hash", ""))):
            raise ValueError("invalid_selection")
        if value.get("open_hash") is not None and not HASH.fullmatch(
            str(value["open_hash"])
        ):
            raise ValueError("invalid_selection")
        return value

    def _read_selection(self):
        state = json.loads(self._state_path.read_text())
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("invalid_selection")
        last = state.get("last_successful_source_id")
        if last is not None and not SOURCE_ID.fullmatch(last):
            raise ValueError("invalid_history")
        active = self._validate_identity(state.get("active"))
        pending = self._validate_identity(state.get("pending"), pending=True)
        if pending is not None and pending.get("phase") not in {
            "preparing",
            "prepared",
            "opening",
        }:
            raise ValueError("invalid_pending")
        return last, active, pending

    def _load_selection(self, media_path):
        try:
            if self._state_path.exists():
                self._last_source, self._active, self._pending = self._read_selection()
            elif self._receipt.exists() and media_path is None:
                self._load_legacy()
        except (OSError, ValueError, TypeError, AttributeError):
            self._load_error = "selection_unreadable"

    def _load_legacy(self):
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
    def _empty():
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

    def _publish(self, value):
        with self._snapshot_lock:
            self._snapshot = dict(value)
        return dict(value)

    def status(self):
        with self._snapshot_lock:
            value = dict(self._snapshot)
        stamp = value.get("observed_at")
        if value["state"] in {"playing", "paused"} and (
            not stamp
            or (
                datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
            ).total_seconds()
            > 5
        ):
            value.update(state="unknown", progressing=False, error="stale_observation")
        return value

    def _store(self):
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

    def _asset_check(self):
        if (
            not self._target
            or not self._pending
            or file_hash(self._target["file"]) != self._pending["file_hash"]
        ):
            raise self.Error("selection_unverified")

    def _check_cancelled(self):
        if self._closing.is_set() or self._cancelled.is_set():
            raise self.Error("cancelled")

    def _write_generation(self, attempt_id):
        self._write_state(
            self._generation,
            {
                "request_id": attempt_id,
                "cancelled": self._stop_requested,
                "intent_seq": self._intent_seq,
            },
        )

    def _set_generation(self, attempt_id):
        with self._cancel_lock:
            self._check_cancelled()
            self._write_generation(attempt_id)

    def _cancel_attempt(self):
        with self._cancel_lock:
            self._write_generation(str(uuid4()))

    def _check_deadline(self, deadline):
        self._check_cancelled()
        if time.monotonic() >= deadline:
            raise self.Error("deadline")

    def _reset_prepared_open(self):
        self._opening = False
        self._pending["phase"] = "prepared"
        self._store()

    def _open_candidate(self, params, deadline):
        self._check_cancelled()
        self._asset_check()
        if params != {"item": {"file": VIRTUAL_FILE}}:
            raise self.Error("invalid_open")
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

    def _check_fallback_intent(self, deadline):
        if self._fallback_repeat is None:
            return
        if self._rpc("Player.GetActivePlayers", {}, deadline) != []:
            raise self.Error("playback_changed")
        mode = self._rpc(
            "Player.GetProperties", {"playerid": 1, "properties": ["repeat"]}, deadline
        )
        if not isinstance(mode, dict) or mode.get("repeat") != self._fallback_repeat:
            raise self.Error("playback_changed")

    def _identities(self):
        if self._launching and self._opening and self._pending is not None:
            return [self._pending]
        values = [self._active]
        if self._pending is not None and self._pending.get("phase") == "opening":
            values.append(self._pending)
        return values

    def _match_item(self, result):
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

    def _rpc_adapter(self, method, params, deadline):
        if self._closing.is_set():
            raise self.NotDispatched("stopped")
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

    def _progressing(self, proof):
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
        return changed is not None and now - changed <= 5

    def _proof(self, proof):
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

    def _candidates(self):
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

    def _restore_active_generation(self):
        if self._opening or self._load_error or self._active is None:
            return
        with self._cancel_lock:
            attempt_id = self._active.get("attempt_id")
            if attempt_id and not self._cancelled.is_set():
                self._write_generation(attempt_id)

    def _failure(self, request, code, uncertain=False):
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

    def _prepare(self, source, request, deadline):
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
            raise self.Error("invalid_target")
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

    def _open_owned(self, deadline):
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
                raise self.Error("sources_unavailable") from error
            raise self.Error("open_unconfirmed") from error

    def _promote(self):
        self._active = {
            key: value for key, value in self._pending.items() if key != "phase"
        }
        self._last_source = self._active["source_id"]
        self._pending = None
        self._store()

    def _release_superseded(self, proof):
        if proof.get("repeat_superseded") is not True:
            return False
        self._active = self._pending = None
        self._store()
        return True

    def _require_progress_ownership(self, proof):
        if self._release_superseded(proof):
            raise self.Error("playback_changed")
        if not proof["owned"]:
            self._cancel_attempt()
            if proof.get("active") is not True:
                # Absence is not proof that a delayed native Open was cancelled.
                raise self.Error("open_unconfirmed")
            self._active = self._pending = None
            self._store()
            raise self.Error("playback_changed")

    def _await_progress(self, proof, deadline):
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
                raise self.Error("cancelled")
            proof = self._owner.observe(deadline=progress_deadline)
        self._require_progress_ownership(proof)
        self._cancel_attempt()
        try:
            stopped = self._owner.stop(deadline=deadline + 8.0)
        except Exception as error:
            raise self.Error("stop_unconfirmed") from error
        if self._release_superseded(stopped):
            raise self.Error("playback_changed")
        if stopped["owned"] or stopped.get("stopped") is not True:
            raise self.Error("stop_unconfirmed")
        self._fallback_repeat = stopped["repeat"]
        self._active = self._pending = None
        self._store()
        return None

    def _run_candidates(self, candidates, request, starting, deadline):
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
        raise self.Error("sources_unavailable")

    def start(self, request_id=None):
        request_id = uuid4().hex if request_id is None else request_id
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise self.Error("invalid_request_id")
        deadline = time.monotonic() + self._timeout
        # Only another admitted launch is busy. A bounded monitor/stop operation
        # may finish first; it must not randomly reject the next voice command.
        with self._cancel_lock:
            if self._launch_admitted:
                raise self.Error("busy")
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

    def _start_request(self, request, deadline):
        try:
            if self._closing.is_set() or self._load_error:
                raise self.Error(self._load_error or "stopped")
            candidates = self._candidates()
            self._failed_request = False
            self._fallback_repeat = None
            self._launching, self._opening = True, False
            starting = {**self._empty(), **request, "state": "starting"}
            self._publish(starting)
            if not candidates:
                raise self.Error("no_distinct_source")
            self._pending = None
            self._store()
            return self._run_candidates(candidates, request, starting, deadline)
        except self.Error as error:
            self._failure(
                request,
                str(error),
                str(error)
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
            self._failure(request, "start_unconfirmed", True)
            raise self.Error("start_unconfirmed") from error
        finally:
            self._launching = self._opening = False

    def _recover_prepared(self):
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

    def _finish_observation(self, proof):
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

    def _cleanup_identity(self):
        if self._pending is not None and self._pending.get("phase") == "opening":
            return self._pending
        return self._active if self._stop_requested else None

    def _fence_identity(self, identity):
        with self._cancel_lock:
            current = json.loads(self._generation.read_text())
            if current.get("request_id") == identity.get("attempt_id"):
                self._write_generation(str(uuid4()))

    def _settle_unleased(self, proof):
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

    def _settlement_still_current(self, proof):
        return (
            self._cleanup_identity() is None
            or proof["owned"]
            or (proof["state"] != "unknown" and not proof["file_matches"])
        )

    def _unresolved_opening(self):
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

    def observe(self):
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
        except Exception:
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

    def _recovery_rpc(self, method, params, deadline):
        if self._closing.is_set():
            raise self.Error("stopped")
        return self._rpc(method, params, deadline)

    def _stop_unleased_candidate(self, identity):
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
            raise self.Error("stop_unconfirmed")

        return True

    def stop(self):
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
            try:
                proof = self._owner.stop()
                if not self._settle_unleased(proof):
                    raise self.Error("stop_unconfirmed")
                if self._cleanup_identity() is not None and not proof["owned"]:
                    proof = self._owner.observe()
                if not self._settlement_still_current(proof):
                    raise self.Error("stop_unconfirmed")
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
                self._failure(request, "stop_unconfirmed", True)
                raise self.Error("stop_unconfirmed") from error

    def _recovery_pending(self):
        return (
            self._owner.status()["owned"]
            or self._pending is not None
            or self._active is not None
        )

    def start_monitor(self):
        with self._lock:
            if (
                not self._auto_monitor
                or self._closing.is_set()
                or self._load_error
                or not self._recovery_pending()
                or self._thread is not None
                and self._thread.is_alive()
            ):
                return
            self._thread = threading.Thread(
                target=self._monitor, name="kodi-fireplace-rotation", daemon=True
            )
            self._thread.start()

    def _monitor(self):
        try:
            while not self._closing.is_set():
                self.observe()
                if not self._recovery_pending() or self._closing.wait(1):
                    return
        finally:
            with self._lock:
                self._thread = None
                self.start_monitor()

    def close(self):
        self._closing.set()
        self._cancelled.set()
        self._owner.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=4)
