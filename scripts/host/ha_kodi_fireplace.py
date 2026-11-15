"""
Verified fireplace playback with durable, narrowly owned repeat cleanup.

No caller-controlled paths or RPC methods cross the HTTP boundary. The observer
only exists while a receipt remains. It never touches an active audio player.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Callable

MEDIA_PATH = Path("/home/ultra/KodiLibrary/Ambience/fireplace-fastily-1080p.webm")
MEDIA_SHA256 = "248cc597abdf1e53e3d55ab998e1aadfff3c96f7c48ca2ce30362b74d5959267"
MODES = {"off", "one", "all"}
ABSENCE_GRACE_SECONDS = 2.0
OBSERVATION_MAX_AGE_SECONDS = 5.0


class FireplaceError(Exception):
    """A safe error code; never include Kodi response bodies or other filenames."""


class RpcNotDispatchedError(FireplaceError):
    """The local deadline/closing guard rejected a call before dispatch."""


def atomic_json(path: Path, value: object) -> None:
    """Durably replace a private JSON record, including its directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".fireplace-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


class OwnedPlayback:
    """Maintain a durable repeat lease for one precisely identified video."""

    # Preserve the established dependency-injected constructor API.
    def __init__(  # noqa: PLR0913
        self,
        rpc: Callable[[str, dict[str, Any], float], Any],
        receipt_path: Path,
        *,
        media_path: Path | str = MEDIA_PATH,
        expected_sha256: str = MEDIA_SHA256,
        auto_monitor: bool = True,
        operation_timeout: float = 8.0,
        poll_interval: float = 0.05,
        asset_check: Callable[[], None] | None = None,
    ) -> None:
        """Configure callbacks, budgets and any durable prior repeat lease."""
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
        self._idle_since = None
        self._stop_requested = False
        self._load_error = False
        self._snapshot = self._empty()
        try:
            if self._receipt_path.exists():
                receipt = json.loads(self._receipt_path.read_text())
                self._validate_receipt(receipt)
                self._owner = receipt
                self._snapshot["owned"] = True
                self._snapshot["state"] = "recovering"
        except (OSError, ValueError, TypeError):
            self._load_error = True
            self._snapshot["state"] = "error"
            self._snapshot["error"] = "owner_unreadable"

    @staticmethod
    def _validate_receipt(receipt: object) -> None:
        if (
            not isinstance(receipt, dict)
            or receipt.get("version") != 1
            or receipt.get("prior_repeat") not in MODES
            or receipt.get("phase") not in {"armed", "looping", "restoring"}
            or not isinstance(receipt.get("token"), str)
        ):
            error_message = "invalid receipt"
            raise ValueError(error_message)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "state": "idle",
            "owned": False,
            "file_matches": False,
            "repeat": None,
            "observed_at": None,
            "error": None,
        }

    def _rpc(self, method: str, params: dict[str, Any], deadline: float) -> Any:
        if self._closing.is_set():
            error_message = "stopped"
            raise RpcNotDispatchedError(error_message)
        if time.monotonic() >= deadline:
            error_message = "deadline"
            raise RpcNotDispatchedError(error_message)
        return self._rpc_callback(method, params, deadline)

    def _persist(self, receipt: dict[str, Any]) -> None:
        """Fsync both data and rename before the first repeat mutation."""
        atomic_json(self._receipt_path, receipt)

    def _forget(self) -> None:
        self._receipt_path.unlink(missing_ok=True)
        if self._receipt_path.parent.exists():
            directory = os.open(self._receipt_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        self._owner = None

    def _read_video_item(
        self, active: list[dict[str, Any]], deadline: float
    ) -> dict[str, Any] | None:
        if not active:
            return None
        response = self._rpc(
            "Player.GetItem", {"playerid": 1, "properties": ["file", "title"]}, deadline
        )
        item = response.get("item") if isinstance(response, dict) else None
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("file"), str)
            or not item["file"]
        ):
            error_message = "invalid_item_state"
            raise FireplaceError(error_message)
        return item

    def _read(self, deadline: float) -> dict[str, Any]:
        active = self._rpc("Player.GetActivePlayers", {}, deadline)
        if not isinstance(active, list):
            error_message = "invalid_player_state"
            raise FireplaceError(error_message)
        if any(not isinstance(item, dict) for item in active):
            error_message = "invalid_player_state"
            raise FireplaceError(error_message)
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
            error_message = "video_properties_unavailable"
            raise FireplaceError(error_message) from exc
        if not isinstance(values, dict) or values.get("repeat") not in MODES:
            error_message = "invalid_repeat_state"
            raise FireplaceError(error_message)
        speed = values.get("speed", 0)
        if type(speed) not in {int, float}:
            error_message = "invalid_speed_state"
            raise FireplaceError(error_message)
        return {
            "foreign_player": False,
            "active": bool(active),
            "file_matches": item is not None and item["file"] == str(self._media),
            "repeat": values["repeat"],
            "speed": speed,
        }

    def _publish(
        self, observed: dict[str, Any], *, error: str | None = None
    ) -> dict[str, Any]:
        self._confirm_playback(observed)
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
            "active": observed.get("active"),
            "repeat": observed.get("repeat"),
            "observed_at": datetime.now(UTC).isoformat(),
            "error": error,
        }
        return dict(self._snapshot)

    def _failed(self, code: str) -> dict[str, Any]:
        self._snapshot = {
            **self._snapshot,
            "state": "unknown",
            "owned": self._owner is not None,
            "file_matches": False,
            "active": None,
            "repeat": None,
            "observed_at": None,
            "error": code,
        }
        return dict(self._snapshot)

    def _defer_absence(self, current: dict[str, Any], *, grace: bool) -> bool:
        if current["active"] or self._stop_requested or not grace:
            self._idle_since = None
            return False
        now = time.monotonic()
        if self._idle_since is None:
            self._idle_since = now
        return now - self._idle_since < ABSENCE_GRACE_SECONDS

    def _repeat_superseded(self, current: dict[str, Any]) -> dict[str, Any]:
        """Release durable ownership and return a positive transient intent proof."""
        self._forget()
        return {**self._publish(current), "repeat_superseded": True}

    def _release_observed_repeat(self, current: dict[str, Any]) -> dict[str, Any]:
        """Unchanged unconfirmed/restored modes do not prove a newer intent."""
        if (
            self._owner["phase"] in {"armed", "restoring"}
            and current["repeat"] == self._owner["prior_repeat"]
        ):
            self._forget()
            return self._publish(current)
        return self._repeat_superseded(current)

    def _confirm_looping(self) -> bool:
        if self._owner["phase"] in {"armed", "restoring"}:
            receipt = {**self._owner, "phase": "looping"}
            self._persist(receipt)
            self._owner = receipt
            return True
        return False

    def _confirm_playback(self, observed: dict[str, Any]) -> bool:
        """Confirm a matching loop; return whether its receipt was durably changed."""
        if (
            self._owner is not None
            and not observed.get("foreign_player")
            and observed.get("file_matches")
            and observed.get("repeat") == "one"
        ):
            return self._confirm_looping()
        return False

    def _restore_repeat(
        self, latest: dict[str, Any], prior: str, deadline: float
    ) -> dict[str, Any]:
        if prior != "one":
            self._rpc("Player.SetRepeat", {"playerid": 1, "repeat": prior}, deadline)
            latest = self._read(deadline)
            if latest["foreign_player"] or latest["repeat"] == "one":
                return self._publish(latest, error="restore_unconfirmed")
            if latest["repeat"] != prior:
                return self._repeat_superseded(latest)
        self._forget()
        return self._publish(latest)

    # Preserve the mutually exclusive guards and their audited RPC/write order.
    def _observe(self, deadline: float, *, grace: bool = False) -> dict[str, Any]:  # noqa: PLR0911
        current = self._read(deadline)
        if self._owner is None:
            return self._publish(current)
        if current["foreign_player"]:
            self._idle_since = None
            return self._publish(current, error="foreign_player_active")
        if current["repeat"] != "one":
            # Covers external off/all, a crash before SetRepeat, and a crash
            # after successful restoration. Never replay an old repeat intent.
            return self._release_observed_repeat(current)
        if self._owner["phase"] == "armed":
            self._confirm_looping()
        if current["file_matches"]:
            self._idle_since = None
            return self._publish(current)
        if self._defer_absence(current, grace=grace):
            return self._publish(current)
        latest = self._read(deadline)
        if latest["foreign_player"]:
            return self._publish(latest, error="foreign_player_active")
        if latest["repeat"] != "one":
            return self._repeat_superseded(latest)
        if latest["file_matches"]:
            return self._publish(latest)
        prior = self._owner["prior_repeat"]
        receipt = {**self._owner, "phase": "restoring"}
        self._persist(receipt)
        self._owner = receipt
        latest = self._read(deadline)
        if latest["foreign_player"]:
            return self._publish(latest, error="foreign_player_active")
        if latest["repeat"] != "one":
            return self._repeat_superseded(latest)
        if latest["file_matches"]:
            return self._publish(latest)
        return self._restore_repeat(latest, prior, deadline)

    def _bounded_deadline(
        self, deadline: float | None, limit: float | None = None
    ) -> float:
        end = time.monotonic() + min(
            self._operation_timeout, limit or self._operation_timeout
        )
        return min(end, deadline) if deadline is not None else end

    def observe(self, *, deadline: float | None = None) -> dict[str, Any]:
        """Read current ownership evidence without exposing callback failures."""
        with self._lock:
            if self._closing.is_set():
                return self._failed("stopped")
            if self._load_error:
                return dict(self._snapshot)
            try:
                return self._observe(self._bounded_deadline(deadline, 3.0), grace=True)
            # Preserve a safe snapshot for every callback failure; never log responses.
            except Exception:  # noqa: BLE001
                return self._failed("observation_failed")

    def status(self) -> dict[str, Any]:
        """Return the latest snapshot, marking expired evidence as unknown."""
        with self._lock:
            snapshot = dict(self._snapshot)
            stamp = snapshot.get("observed_at")
            if snapshot["state"] in {"playing", "paused"} and (
                not stamp
                or (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
                > OBSERVATION_MAX_AGE_SECONDS
            ):
                snapshot.update(state="unknown", error="stale_observation")
            return snapshot

    def _open_confirmed(self, deadline: float) -> dict[str, Any]:
        # Exactly one mutation. Missing/ambiguous ACK never triggers another Open.
        self._rpc("Player.Open", {"item": {"file": str(self._media)}}, deadline)
        while True:
            try:
                current = self._read(deadline)
                if current["file_matches"] and current["speed"] > 0:
                    self._confirm_playback(current)
                    return current
            except FireplaceError as exc:
                # The old preferred playlist can persist briefly after Open.
                # Retry only this read; all transport and operation budgets hold.
                if str(exc) != "video_properties_unavailable":
                    raise
            if self._closing.wait(self._poll_interval):
                error_message = "stopped"
                raise FireplaceError(error_message)

    def _verify_asset(self) -> None:
        digest = hashlib.sha256()
        with self._media.open("rb") as source:
            for chunk in iter(lambda: source.read(65536), b""):
                digest.update(chunk)
        if digest.hexdigest() != self._sha:
            error_message = "asset_unverified"
            raise FireplaceError(error_message)

    def _existing_observation(
        self, deadline: float
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        try:
            return None, self._observe(deadline)
        except FireplaceError as exc:
            if str(exc) != "video_properties_unavailable":
                raise
            if self._rpc("Player.GetActivePlayers", {}, deadline) != []:
                error_message = "previous_owner_pending"
                raise FireplaceError(error_message) from exc
            opened = self._open_confirmed(deadline)
            return opened, self._observe(deadline)

    def _only_audio_active(self, deadline: float) -> bool:
        players = self._rpc("Player.GetActivePlayers", {}, deadline)
        return (
            isinstance(players, list)
            and len(players) == 1
            and isinstance(players[0], dict)
            and players[0].get("playerid") == 0
            and players[0].get("type") == "audio"
        )

    def _resume_owned(
        self, deadline: float, *, force_new: bool
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not self._owner:
            return None, None
        opened, proof = self._existing_observation(deadline)
        if (
            opened is None
            and proof.get("error") == "foreign_player_active"
            and self._only_audio_active(deadline)
        ):
            opened = self._open_confirmed(deadline)
            proof = self._observe(deadline)
        if force_new and opened is None and proof["state"] in {"playing", "paused"}:
            # Explicit replacement retains the existing prior-repeat lease.
            # The callback atomically records the pending source before Open.
            opened = self._open_confirmed(deadline)
            proof = self._observe(deadline)
        if opened is not None and proof.get("repeat_superseded") is True:
            return opened, proof
        if proof["state"] == "paused":
            self._rpc("Player.PlayPause", {"playerid": 1, "play": True}, deadline)
            proof = self._observe(deadline)
        if proof["state"] == "playing":
            return opened, proof
        if self._owner:
            error_message = "previous_owner_pending"
            raise FireplaceError(error_message)
        return opened, None

    def _prior_mode(self, deadline: float) -> str:
        try:
            previous = self._read(deadline)
            return previous["repeat"] if previous["repeat"] in MODES else "off"
        # An unavailable callback cannot supply a reliable prior repeat mode.
        except Exception:  # noqa: BLE001
            return "off"

    def _arm_loop(
        self, current: dict[str, Any], prior: str, deadline: float
    ) -> dict[str, Any]:
        prior = current["repeat"] if current["repeat"] in MODES else prior
        receipt = {
            "version": 1,
            "token": uuid4().hex,
            "prior_repeat": prior,
            "phase": "armed",
        }
        self._persist(receipt)
        self._owner = receipt
        # Keep validation inside the shared pre-mutation lease cleanup boundary.
        try:
            latest = self._read(deadline)
            if (
                not latest["foreign_player"]
                and latest["file_matches"]
                and latest["repeat"] != current["repeat"]
            ):
                return self._repeat_superseded(latest)
            if (
                latest["foreign_player"]
                or not latest["file_matches"]
                or latest["speed"] <= 0
                or latest["repeat"] != current["repeat"]
            ):
                error_message = "content_changed"
                raise FireplaceError(error_message)  # noqa: TRY301
        except Exception:
            # No repeat mutation has been sent. A failed/expired durable arm
            # must not leave a receipt that can later reclaim unrelated repeat.
            self._forget()
            raise
        try:
            self._rpc("Player.SetRepeat", {"playerid": 1, "repeat": "one"}, deadline)
        except RpcNotDispatchedError:
            self._forget()
            raise
        proof = self._observe(deadline)
        if proof.get("repeat_superseded") is True:
            return proof
        self.start_monitor()
        if proof["state"] != "playing":
            error_message = "loop_unconfirmed"
            raise FireplaceError(error_message)
        return proof

    def start(
        self, *, force_new: bool = False, deadline: float | None = None
    ) -> dict[str, Any]:
        """Open the requested source and confirm its owned repeat lease."""
        with self._lock:
            if self._closing.is_set():
                error_message = "stopped"
                raise FireplaceError(error_message)
            if self._load_error:
                error_message = "owner_unreadable"
                raise FireplaceError(error_message)
            deadline = self._bounded_deadline(deadline)
            self._stop_requested = False
            self._idle_since = None
            try:
                self._asset_check()
                opened, proof = self._resume_owned(deadline, force_new=force_new)
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
                error_message = "start_unconfirmed"
                raise FireplaceError(error_message) from exc

    def stop(self, *, deadline: float | None = None) -> dict[str, Any]:
        """Stop only the currently owned video while preserving newer user intent."""
        with self._lock:
            if self._closing.is_set():
                error_message = "stopped"
                raise FireplaceError(error_message)
            self._stop_requested = True
            if not self._owner:
                return {**self.status(), "stopped": False}
            deadline = self._bounded_deadline(deadline)
            try:
                current = self._read(deadline)
                if self._confirm_playback(current):
                    # The durable write may block; newer native intent wins.
                    current = self._read(deadline)
                if current["foreign_player"]:
                    return self._publish(current, error="foreign_player_active")
                if current["repeat"] != "one":
                    return {**self._release_observed_repeat(current), "stopped": False}
                if current["file_matches"]:
                    self._rpc("Player.Stop", {"playerid": 1}, deadline)
                proof = self._observe(deadline)
                if proof.get("repeat_superseded") is True:
                    return {**proof, "stopped": False}
                latest = self._read(deadline)
                self._confirm_playback(latest)
                if (
                    not latest["foreign_player"]
                    and proof["repeat"] in MODES
                    and latest["repeat"] != proof["repeat"]
                ):
                    proof = self._repeat_superseded(latest)
                return {
                    **proof,
                    "stopped": current["file_matches"]
                    and not latest["foreign_player"]
                    and not latest["active"],
                }
            except FireplaceError:
                raise
            except Exception as exc:
                self._failed("stop_unconfirmed")
                error_message = "stop_unconfirmed"
                raise FireplaceError(error_message) from exc

    def start_monitor(self) -> None:
        """Start one observer only while a durable repeat lease exists."""
        with self._lock:
            if (
                not self._auto_monitor
                or self._closing.is_set()
                or self._owner is None
                or (self._thread is not None and self._thread.is_alive())
            ):
                return
            self._thread = threading.Thread(
                target=self._monitor, name="kodi-fireplace", daemon=True
            )
            self._thread.start()

    def _monitor(self) -> None:
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

    def close(self) -> None:
        # Keep the receipt: restarting the bridge must not change user playback.
        # Each RPC has a bounded deadline; no new RPC starts after this flag.
        """Stop observation while retaining the durable receipt for recovery."""
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
    """Compose source rotation with the repeat-ownership implementation."""

    def __init__(
        self,
        rpc: Callable[[str, dict[str, Any], float], Any],
        receipt_path: Path,
        **options: Any,
    ) -> None:
        """Bind the shared ownership implementation and legacy asset metadata."""
        options.setdefault("expected_sha256", MEDIA_SHA256)
        super().__init__(
            OwnedPlayback,
            FireplaceError,
            rpc,
            receipt_path,
            state_writer=atomic_json,
            rejected_error_type=RpcNotDispatchedError,
            **options,
        )
