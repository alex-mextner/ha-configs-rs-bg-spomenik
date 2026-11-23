# Copyright (c) 2026 Alex Mextner
"""Private accepted-control telemetry for the scoped smart-home event bridge."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

VERSION = 1
LOGGER = logging.getLogger("music_assistant.ha_recovery_intent")
IDENTITY = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
CONTROLS = {
    "stop",
    "pause",
    "play",
    "play_pause",
    "resume",
    "next",
    "previous",
    "skip",
    "seek",
    "clear",
    "play_index",
    "play_media",
    "select_source",
    "power",
    "volume_mute",
    "volume_set",
    "transfer",
}


def intent_payload(command: str, args: dict, state_getter: object) -> dict | None:
    """Validate target access with the existing permission-aware getter."""
    prefix, _, name = command.rpartition("/")
    if prefix not in {"players/cmd", "player_queues"} or name not in CONTROLS:
        return None
    target = args.get("player_id") if prefix == "players/cmd" else args.get("queue_id")
    if not isinstance(target, str) or not IDENTITY.fullmatch(target):
        return None
    state = state_getter(target)
    if state is None or not state.enabled:
        return None
    return {
        "command": command,
        "player_ids": [target] if prefix == "players/cmd" else [],
        "queue_ids": [target] if prefix == "player_queues" else [],
        "correlation": uuid.uuid4().hex,
        "observed_at": time.time(),
    }


def begin_intent(
    mass: object, command: str, args: dict, message_id: object = None
) -> dict | None:
    """Log a validated pending request before effects; never alter authorization."""
    try:
        from music_assistant.controllers.players.helpers import (  # noqa: PLC0415 - native optional runtime.
            is_own_client_player,
        )
        from music_assistant.controllers.webserver.helpers.auth_middleware import (  # noqa: PLC0415 - native optional runtime.
            get_current_user,
        )

        user = get_current_user()
        if user is None:
            return None

        def allowed_state(target: str) -> object:
            player = mass.players.get_player(target)
            if player and player.protocol_parent_id:
                player = mass.players.get_player(player.protocol_parent_id)
            if player is None:
                return None
            if (
                user.player_filter
                and player.player_id not in user.player_filter
                and not is_own_client_player(player)
            ):
                return None
            return mass.players.get_player_state(player.player_id)

        payload = intent_payload(command, args, allowed_state)
        if payload:
            if isinstance(message_id, str) and re.fullmatch(
                r"ha-recovery:[a-f0-9]{32}", message_id
            ):
                payload["origin_tag"] = message_id
            LOGGER.info(
                "HA_RECOVERY_INTENT %s", json.dumps({**payload, "phase": "pending"})
            )
    except Exception:  # noqa: BLE001 - telemetry cannot alter command authorization or results.
        # Telemetry must not weaken permission checks or break a command. A missing
        # record is conservatively handled by the event consumer's quiet guards.
        return None
    else:
        return payload


def finish_intent(payload: dict | None) -> None:
    """Only a successfully completed API handler produces accepted telemetry."""
    if payload:
        LOGGER.info(
            "HA_RECOVERY_INTENT %s", json.dumps({**payload, "phase": "accepted"})
        )
