"""Verified fireplace playback with durable, narrowly owned repeat cleanup.

No caller-controlled paths or RPC methods cross the HTTP boundary. The observer
only exists while a receipt remains. It never touches an active audio player.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from uuid import uuid4

MEDIA_PATH = Path("/home/ultra/KodiLibrary/Ambience/fireplace-fastily-1080p.webm")
MEDIA_SHA256 = "248cc597abdf1e53e3d55ab998e1aadfff3c96f7c48ca2ce30362b74d5959267"
MODES = {"off", "one", "all"}


class FireplaceError(Exception):
    """A safe error code; never include Kodi response bodies or other filenames."""


class OwnedPlayback:
    def __init__(
        self,
        rpc,
        receipt_path: Path,
        *,
        media_path=MEDIA_PATH,
        expected_sha256=MEDIA_SHA256,
        auto_monitor=True,
        operation_timeout=8.0,
        poll_interval=0.05,
        asset_check=None,
    ):
        self._rpc_callback = rpc
        self._receipt_path = Path(receipt_path)
        self._media = Path(media_path)
        self._sha = expected_sha256
        self._asset_check = asset_check or self._verify_asset
        self._auto_monitor = auto_monitor
        self._operation_timeout = operation_timeout
        self._poll_interval = poll_interval
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._thread = None
        self._owner = None
        self._load_error = False
        self._snapshot = self._empty()
        try:
            if self._receipt_path.exists():
                receipt = json.loads(self._receipt_path.read_text())
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("version") != 1
                    or receipt.get("prior_repeat") not in MODES
                    or receipt.get("phase") not in {"armed", "looping", "restoring"}
                    or not isinstance(receipt.get("token"), str)
                ):
                    raise ValueError("invalid receipt")
                self._owner = receipt
                self._snapshot["owned"] = True
                self._snapshot["state"] = "recovering"
        except (OSError, ValueError, TypeError):
            self._load_error = True
            self._snapshot["state"] = "error"
            self._snapshot["error"] = "owner_unreadable"

    @staticmethod
    def _empty():
        return {
            "state": "idle",
            "owned": False,
            "file_matches": False,
            "repeat": None,
            "observed_at": None,
            "error": None,
        }

    def _rpc(self, method, params, deadline):
        if self._closing.is_set():
            raise FireplaceError("stopped")
        if time.monotonic() >= deadline:
            raise FireplaceError("deadline")
        return self._rpc_callback(method, params, deadline)

    def _persist(self, receipt):
        """Fsync both data and rename before the first repeat mutation."""
        parent = self._receipt_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".fireplace-", dir=parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(receipt, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._receipt_path)
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _forget(self):
        self._receipt_path.unlink(missing_ok=True)
        if self._receipt_path.parent.exists():
            directory = os.open(self._receipt_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        self._owner = None

    def _read_video_item(self, active, deadline):
        if not active:
            return None
        response = self._rpc(
            "Player.GetItem", {"playerid": 1, "properties": ["file", "title"]}, deadline
        )
        item = response.get("item") if isinstance(response, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            raise FireplaceError("invalid_item_state")
        return item

    def _read(self, deadline):
        active = self._rpc("Player.GetActivePlayers", {}, deadline)
        if not isinstance(active, list):
            raise FireplaceError("invalid_player_state")
        if any(not isinstance(item, dict) for item in active):
            raise FireplaceError("invalid_player_state")
        # Never query/set playlist 1 while another player is active. Kodi may
        # otherwise resolve properties against its preferred audio player.
        if any(
            item.get("playerid") != 1 or item.get("type") != "video" for item in active
        ):
            return {
                "foreign_player": True,
                "file_matches": False,
                "repeat": None,
                "speed": None,
                "active": False,
            }
        item = self._read_video_item(active, deadline)
        properties = ["repeat", "speed"] if active else ["repeat"]
        try:
            values = self._rpc(
                "Player.GetProperties",
                {"playerid": 1, "properties": properties},
                deadline,
            )
        except Exception as exc:
            raise FireplaceError("video_properties_unavailable") from exc
        if not isinstance(values, dict) or values.get("repeat") not in MODES:
            raise FireplaceError("invalid_repeat_state")
        speed = values.get("speed", 0)
        if type(speed) not in {int, float}:
            raise FireplaceError("invalid_speed_state")
        return {
            "foreign_player": False,
            "active": bool(active),
            "file_matches": item is not None and item["file"] == str(self._media),
            "repeat": values["repeat"],
            "speed": speed,
        }

    def _publish(self, observed, *, error=None):
        owned = self._owner is not None
        valid = not observed.get("foreign_player")
        if error:
            state = "unknown"
        elif (
            owned and valid and observed["file_matches"] and observed["repeat"] == "one"
        ):
            state = "playing" if observed["speed"] > 0 else "paused"
        elif owned:
            state = "cleanup_pending"
        else:
            state = "idle"
        self._snapshot = {
            "state": state,
            "owned": owned,
            "file_matches": bool(observed.get("file_matches")),
            "repeat": observed.get("repeat"),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "error": error,
        }
        return dict(self._snapshot)

    def _failed(self, code):
        self._snapshot = {
            **self._snapshot,
            "state": "unknown",
            "owned": self._owner is not None,
            "file_matches": False,
            "repeat": None,
            "observed_at": None,
            "error": code,
        }
        return dict(self._snapshot)

    def _observe(self, deadline):
        current = self._read(deadline)
        if self._owner is None:
            return self._publish(current)
        if current["foreign_player"]:
            return self._publish(current, error="foreign_player_active")
        if current["repeat"] != "one":
            # Covers external off/all, a crash before SetRepeat, and a crash
            # after successful restoration. Never replay an old repeat intent.
            self._forget()
            return self._publish(current)
        if current["file_matches"]:
            return self._publish(current)
        # Recheck the value immediately before cleanup. This detects an external
        # mode edit made while the first observation was in flight.
        latest = self._read(deadline)
        if latest["foreign_player"]:
            return self._publish(latest, error="foreign_player_active")
        if latest["repeat"] != "one":
            self._forget()
            return self._publish(latest)
        if latest["file_matches"]:
            return self._publish(latest)
        prior = self._owner["prior_repeat"]
        receipt = {**self._owner, "phase": "restoring"}
        self._persist(receipt)
        self._owner = receipt
        # A durable write may block. An external mode/content change during it
        # must win over this older cleanup intent.
        latest = self._read(deadline)
        if latest["foreign_player"]:
            return self._publish(latest, error="foreign_player_active")
        if latest["repeat"] != "one":
            self._forget()
            return self._publish(latest)
        if latest["file_matches"]:
            return self._publish(latest)
        if prior != "one":
            self._rpc("Player.SetRepeat", {"playerid": 1, "repeat": prior}, deadline)
            latest = self._read(deadline)
            if latest["foreign_player"] or latest["repeat"] == "one":
                return self._publish(latest, error="restore_unconfirmed")
        self._forget()
        return self._publish(latest)

    def observe(self):
        with self._lock:
            if self._closing.is_set():
                return self._failed("stopped")
            if self._load_error:
                return dict(self._snapshot)
            try:
                return self._observe(
                    time.monotonic() + min(3.0, self._operation_timeout)
                )
            except Exception:
                return self._failed("observation_failed")

    def status(self):
        with self._lock:
            snapshot = dict(self._snapshot)
            stamp = snapshot.get("observed_at")
            if snapshot["state"] in {"playing", "paused"} and (
                not stamp
                or (
                    datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
                ).total_seconds()
                > 5
            ):
                snapshot.update(state="unknown", error="stale_observation")
            return snapshot

    def _open_confirmed(self, deadline):
        # Exactly one mutation. Missing/ambiguous ACK never triggers another Open.
        self._rpc("Player.Open", {"item": {"file": str(self._media)}}, deadline)
        while True:
            try:
                current = self._read(deadline)
                if current["file_matches"] and current["speed"] > 0:
                    return current
            except FireplaceError as exc:
                # The old preferred playlist can persist briefly after Open.
                # Retry only this read; all transport and operation budgets hold.
                if str(exc) != "video_properties_unavailable":
                    raise
            if self._closing.wait(self._poll_interval):
                raise FireplaceError("stopped")

    def _verify_asset(self):
        digest = hashlib.sha256()
        with self._media.open("rb") as source:
            for chunk in iter(lambda: source.read(65536), b""):
                digest.update(chunk)
        if digest.hexdigest() != self._sha:
            raise FireplaceError("asset_unverified")

    def _existing_observation(self, deadline):
        try:
            return None, self._observe(deadline)
        except FireplaceError as exc:
            if str(exc) != "video_properties_unavailable":
                raise
            if self._rpc("Player.GetActivePlayers", {}, deadline) != []:
                raise FireplaceError("previous_owner_pending") from exc
            opened = self._open_confirmed(deadline)
            return opened, self._observe(deadline)

    def _only_audio_active(self, deadline):
        players = self._rpc("Player.GetActivePlayers", {}, deadline)
        return (
            isinstance(players, list)
            and len(players) == 1
            and isinstance(players[0], dict)
            and players[0].get("playerid") == 0
            and players[0].get("type") == "audio"
        )

    def _resume_owned(self, deadline, force_new):
        if not self._owner:
            return None, None
        opened, proof = self._existing_observation(deadline)
        if opened is None and proof.get("error") == "foreign_player_active":
            if self._only_audio_active(deadline):
                opened = self._open_confirmed(deadline)
                proof = self._observe(deadline)
        if force_new and opened is None and proof["state"] in {"playing", "paused"}:
            # Explicit replacement retains the existing prior-repeat lease.
            # The callback atomically records the pending source before Open.
            opened = self._open_confirmed(deadline)
            proof = self._observe(deadline)
        if proof["state"] == "paused":
            self._rpc("Player.PlayPause", {"playerid": 1, "play": True}, deadline)
            proof = self._observe(deadline)
        if proof["state"] == "playing":
            return opened, proof
        if self._owner:
            raise FireplaceError("previous_owner_pending")
        return opened, None

    def _prior_mode(self, deadline):
        try:
            previous = self._read(deadline)
            return previous["repeat"] if previous["repeat"] in MODES else "off"
        except Exception:
            return "off"

    def _arm_loop(self, current, prior, deadline):
        prior = current["repeat"] if current["repeat"] in MODES else prior
        receipt = {
            "version": 1,
            "token": uuid4().hex,
            "prior_repeat": prior,
            "phase": "armed",
        }
        self._persist(receipt)
        self._owner = receipt
        latest = self._read(deadline)
        if (
            latest["foreign_player"]
            or not latest["file_matches"]
            or latest["speed"] <= 0
            or latest["repeat"] != current["repeat"]
        ):
            self._forget()
            raise FireplaceError("content_changed")
        self._rpc("Player.SetRepeat", {"playerid": 1, "repeat": "one"}, deadline)
        proof = self._observe(deadline)
        self.start_monitor()
        if proof["state"] != "playing":
            raise FireplaceError("loop_unconfirmed")
        return proof

    def start(self, *, force_new=False):
        with self._lock:
            if self._closing.is_set():
                raise FireplaceError("stopped")
            if self._load_error:
                raise FireplaceError("owner_unreadable")
            deadline = time.monotonic() + self._operation_timeout
            try:
                self._asset_check()
                opened, proof = self._resume_owned(deadline, force_new)
                if proof is not None:
                    self.start_monitor()
                    return proof
                prior = self._prior_mode(deadline)
                current = (
                    opened if opened is not None else self._open_confirmed(deadline)
                )
                return self._arm_loop(current, prior, deadline)
            except FireplaceError:
                self._failed("start_unconfirmed")
                self.start_monitor()
                raise
            except Exception as exc:
                self._failed("start_unconfirmed")
                self.start_monitor()
                raise FireplaceError("start_unconfirmed") from exc

    def stop(self):
        with self._lock:
            if self._closing.is_set():
                raise FireplaceError("stopped")
            if not self._owner:
                return self.status()
            deadline = time.monotonic() + self._operation_timeout
            try:
                current = self._read(deadline)
                if current["foreign_player"]:
                    return self._publish(current, error="foreign_player_active")
                if current["file_matches"]:
                    self._rpc("Player.Stop", {"playerid": 1}, deadline)
                return self._observe(deadline)
            except FireplaceError:
                raise
            except Exception as exc:
                self._failed("stop_unconfirmed")
                raise FireplaceError("stop_unconfirmed") from exc

    def start_monitor(self):
        with self._lock:
            if (
                not self._auto_monitor
                or self._closing.is_set()
                or self._owner is None
                or self._thread is not None
                and self._thread.is_alive()
            ):
                return
            self._thread = threading.Thread(
                target=self._monitor, name="kodi-fireplace", daemon=True
            )
            self._thread.start()

    def _monitor(self):
        try:
            while not self._closing.is_set():
                proof = self.observe()
                with self._lock:
                    if self._owner is None:
                        return
                delay = 3.0 if proof["state"] == "unknown" else 1.0
                if self._closing.wait(delay):
                    return
        finally:
            with self._lock:
                self._thread = None
                # A new start can establish ownership while the former worker
                # is between its final check and thread exit. Do not strand it.
                self.start_monitor()

    def close(self):
        # Keep the receipt: restarting the bridge must not change user playback.
        # Each RPC has a bounded deadline; no new RPC starts after this flag.
        self._closing.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=4.0)


# Source selection composes the existing repeat-ownership state machine.
_rotation_spec = importlib.util.spec_from_file_location(
    "ha_kodi_fireplace_rotation",
    Path(__file__).with_name("ha_kodi_fireplace_rotation.py"),
)
_rotation = importlib.util.module_from_spec(_rotation_spec)
_rotation_spec.loader.exec_module(_rotation)
SourceUnavailable = _rotation.SourceUnavailable


class FireplaceController(_rotation.RotatingFireplaceController):
    def __init__(self, rpc, receipt_path, **options):
        options.setdefault("expected_sha256", MEDIA_SHA256)
        super().__init__(OwnedPlayback, FireplaceError, rpc, receipt_path, **options)
