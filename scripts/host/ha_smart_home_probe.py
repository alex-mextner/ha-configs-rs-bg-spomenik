#!/usr/bin/env python3
# Copyright (c) 2026 Alex Mextner
"""One-shot exact-speaker announcement; never a periodic health probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

from ha_smart_home_common import (
    CONFIG,
    HA,
    HOME,
    LOCAL_STATE,
    atomic_json,
    hardware_state,
    item_identity,
    ma_command,
    maintenance_status,
)
from ha_smart_home_recovery import LOG, init_log

MAX_VOLUME = 100

ASSETS = {
    "network.ogg": "8c56a8bb345c79a7f18bbf5e74d770b1f31435d3c17cad2ce349c131f7eb9dcb",
    "connection.ogg": (
        "551e97fac86ca0ad82c09bc216d466266d8a318225dcc5e4283017c9f774ceba"
    ),
    "service.ogg": "339e7f11f7cc78b9039962a451d53bb2245bb42d2ab7a47c517972d2772e6f9f",
}
REASON_ASSET = {
    "network": "network.ogg",
    "auth": "connection.ogg",
    "configuration": "connection.ogg",
    "code": "service.ogg",
    "unknown": "service.ogg",
}


def safe_route(player: dict) -> bool:
    """Require the exact reachable, unmuted endpoint without group fan-out."""
    pid = player.get("player_id")
    if (
        player.get("playback_state", player.get("state")) in {"playing", "paused"}
        or player.get("available") is not True
        or player.get("enabled") is not True
    ):
        return False
    if (
        player.get("volume_muted") is not False
        or type(player.get("volume_level")) not in (int, float)
        or not 0 < player["volume_level"] <= MAX_VOLUME
    ):
        return False
    if (
        player.get("type") == "group"
        or player.get("synced_to")
        or player.get("active_group")
    ):
        return False
    if set(player.get("group_members", [])) - {pid}:
        return False
    # Music Assistant's supported announcement API also has an emulated path.
    # We admit it only for an already interrupted standalone endpoint; its
    # original state is idle, so it must not resume or replace a music queue.
    return bool(
        set(player.get("supported_features", [])) & {"play_announcement", "play_media"}
        or any(p.get("available") is True for p in player.get("output_protocols", []))
    )


def same_interruption(action: dict, evidence: dict, now: float) -> bool:
    """Reject obsolete timestamps, a changed queue or subsequent user intent."""
    player, queue, intents = (evidence[k] for k in ("player", "queue", "intents"))
    pid, qid = action.get("player_id"), action.get("queue_id")
    return (
        action.get("deadline", float("inf")) <= now <= action.get("not_after", 0)
        and all(intents.get(t, 0) < action.get("interrupted_at", 0) for t in (pid, qid))
        and player.get("player_id") == pid
        and queue.get("queue_id") == qid
        and bool(action.get("item_id"))
        and item_identity(queue.get("current_item")) == action["item_id"]
        and safe_route(player)
    )


def announcement_plan(
    action: dict,
    evidence: dict,
    config: dict,
    now: float,
) -> dict | None:
    """Prepare native audio arguments without changing playback or volume."""
    if action.get("player_id") not in config.get("players", {}):
        return None
    if not same_interruption(action, evidence, now):
        return None
    asset = REASON_ASSET.get(action.get("reason"))
    if asset is None:
        return None
    return {
        "player_id": action["player_id"],
        "asset": asset,
        "pre_announce": False,
        "volume_level": evidence["player"]["volume_level"],
    }


def resume_plan(action: dict, evidence: dict, config: dict, now: float) -> dict | None:
    """Resume once only an unchanged queue after a verified reconnection."""
    if action.get("player_id") not in config.get("players", {}):
        return None
    player, queue = evidence["player"], evidence["queue"]
    if not same_interruption(action, evidence, now) or queue.get("state") != "idle":
        return None
    # A different source, group or user-selected queue never becomes our target.
    if player.get("active_source") not in {action["queue_id"], None}:
        return None
    return {"queue_id": action["queue_id"]}


def current_intents(action: dict) -> dict | None:
    """Require the same live event source and its accepted-intent subscription."""
    status = json.loads((LOCAL_STATE / "status.json").read_text())
    if status.get("stream_id") != action.get("stream_id") or not status.get(
        "intent_ready"
    ):
        return None
    if not all(status.get("connected", {}).get(name) for name in ("ma", "logs", "ha")):
        return None
    pid = status.get("pid")
    if type(pid) is not int or not (Path("/proc") / str(pid) / "cmdline").is_file():
        return None
    if (
        b"ha_smart_home_events.py"
        not in (Path("/proc") / str(pid) / "cmdline").read_bytes()
    ):
        return None
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if status.get("boot_id") != boot:
        return None
    return json.loads((LOCAL_STATE / "intents.json").read_text()).get("targets", {})


def prepare(action: dict, config: dict) -> dict | None:
    """Revalidate the target and trusted cached clip immediately before audio."""
    if maintenance_status() in {"active", "unknown"}:
        return None
    intents = current_intents(action)
    if intents is None:
        return None
    player = ma_command("players/get", {"player_id": action["player_id"]})
    queue = ma_command("player_queues/get", {"queue_id": action["queue_id"]})
    if not isinstance(player, dict) or not isinstance(queue, dict):
        return None
    player = active_mute_state(player, config["players"][action["player_id"]])
    evidence = {"player": player, "queue": queue, "intents": intents}
    plan = (resume_plan if action.get("type") == "resume" else announcement_plan)(
        action, evidence, config, time.time()
    )
    if not plan:
        return None
    if player.get("hardware_state"):
        plan["hardware_before"] = player["hardware_state"]
    if action.get("type") != "resume":
        asset = plan.pop("asset")
        path = HOME / "homeassistant/www/smart-home-recovery" / asset
        if hashlib.sha256(path.read_bytes()).hexdigest() != ASSETS[asset]:
            msg = "unverified_audio_asset"
            raise RuntimeError(msg)
        plan["url"] = HA + "/local/smart-home-recovery/" + asset
    # A user command accepted while the REST snapshots were fetched still wins.
    latest = current_intents(action)
    if (
        latest is None
        or any(
            latest.get(t, 0) >= action["interrupted_at"]
            for t in (action["player_id"], action["queue_id"])
        )
        or time.time() > action["not_after"]
        or maintenance_status() in {"active", "unknown"}
    ):
        return None
    return plan


def active_mute_state(player: dict, configured: dict) -> dict:
    """Use only the actual active protocol, never an unrelated convenient alias."""
    if configured.get("hardware_mute") is True:
        state = hardware_state(configured.get("physical_id"))
        return {
            **player,
            "volume_muted": state["muted"] if state else None,
            "hardware_state": state,
        }
    active = player.get("active_output_protocol")
    if player.get("volume_muted") is not None or active not in configured.get(
        "aliases", []
    ):
        return player
    child = ma_command("players/get", {"player_id": active})
    if not isinstance(child, dict) or child.get("available") is not True:
        return player
    muted = child.get("volume_muted")
    if (
        "volume_mute" not in child.get("supported_features", [])
        and child.get("volume_level", 0) > 0
    ):
        muted = False
    return {**player, "volume_muted": muted}


def execute(action: dict, plan: dict) -> dict:
    """Mark our own clip before submission so it cannot masquerade as recovery."""
    hardware_before = plan.pop("hardware_before", None)
    if action.get("type") == "resume":
        tag = "ha-recovery:" + uuid.uuid4().hex
        atomic_json(
            LOCAL_STATE
            / "controls"
            / (hashlib.sha256(tag.encode()).hexdigest() + ".json"),
            {
                "origin_tag": tag,
                "queue_id": action["queue_id"],
                "expires": time.time() + 30,
            },
        )
        ma_command("player_queues/resume", plan, message_id=tag, deadline_seconds=23)
        return {
            "status": "accepted",
            "player_id": action["player_id"],
            "playback_verified": False,
        }
    filename = hashlib.sha256(action["player_id"].encode()).hexdigest() + ".json"
    marker = LOCAL_STATE / "announcements" / filename
    claim = {
        "player_id": action["player_id"],
        "claim": action["claim"],
        "expires": time.time() + 45,
        "active": True,
    }
    atomic_json(marker, claim)
    # An ambiguous HTTP response keeps the marker until its bounded expiry.
    # There is no automatic retry of the audible action.
    ma_command("players/cmd/play_announcement", plan, deadline_seconds=23)
    atomic_json(marker, {**claim, "active": False})
    hardware_after = None
    if hardware_before:
        try:
            hardware_after = hardware_state(hardware_before["physical_id"])
        except (OSError, ValueError, TypeError, RuntimeError):
            hardware_after = None
    return {
        "status": "accepted",
        "player_id": action["player_id"],
        "audibility": "unverified",
        "hardware_before": hardware_before,
        "hardware_after": hardware_after,
        "hardware_level_unchanged": bool(
            hardware_before
            and hardware_after
            and hardware_before["level"] is not None
            and hardware_before == hardware_after
        ),
    }


def main() -> None:
    """Read one bounded private request; default mode never invokes audio."""
    log_path = init_log()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    action = json.loads(sys.stdin.read(8192))
    try:
        plan = prepare(action, json.loads(CONFIG.read_text()))
        if plan is None:
            result = {"status": "suppressed", "reason": "no_current_safe_route"}
        elif args.execute:
            result = execute(action, plan)
        else:
            result = {
                "status": "prepared_without_audio",
                "player_id": action["player_id"],
            }
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        result = {"status": "suppressed", "reason": type(error).__name__}
    LOG.info("Completed status=%s log=%s", result.get("status"), log_path)
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
