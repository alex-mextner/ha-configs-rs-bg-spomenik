#!/usr/bin/env python3
# Copyright (c) 2026 Alex Mextner
"""
Bounded HA/MA probes and guarded speech; run only on the production Odroid.

Credentials are read locally and are never included in the JSON returned to the monitor.
No runtime state or credentials of HA/MA are modified except through supported APIs.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

HA = "http://127.0.0.1:8123"
MA = "http://127.0.0.1:8095"
PHRASE = "Умный дом настраивается, минутку."
ROOT = Path.home() / ".local/state/ha-smart-home-recovery/probe"
ENTRIES = Path.home() / "homeassistant/.storage/core.config_entries"
CONFIG = Path.home() / ".config/ha-smart-home-recovery/config.json"
PRESENCE_CACHE_AGE = 300
QUASAR_PORT = 31116
MAX_DIRECT_VOLUME = 4
LOG_RETENTION = 172800
LOG = logging.getLogger("ha_smart_home_probe")
ERRORS = (OSError, ValueError, TypeError, KeyError, RuntimeError, URLError)
DEFAULTS = {
    "speaker": "media_player.yandex_station_m0011hm00z86hg",
    "persons": ["person.ultra", "person.lena"],
    "playback_entities": [
        "media_player.yandex_station_m0011hm00z86hg",
        "media_player.ue48j5500_ue48j5500",
        "media_player.home",
    ],
    "integration_domains": [
        "music_assistant",
        "yandex_station",
        "yandex_smart_home",
        "ekf_appliances",
        "aisis_speaker",
    ],
    "quiet_start": 22,
    "quiet_end": 9,
    "timezone": "Europe/Belgrade",
}


class NoRedirect(HTTPRedirectHandler):
    """Refuse HTTP redirects before forwarding authentication."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        """Reject redirected requests without forwarding credentials."""
        msg_0 = "redirect_refused"
        raise RuntimeError(msg_0)


def request_json(
    url: str,
    token: str | None = None,
    payload: dict | None = None,
    timeout: int = 7,
    *,
    quasar_origin: str | None = None,
) -> object:
    """Call only allowlisted origins without following credential redirects."""
    allowed = (HA + "/", MA + "/") + ((quasar_origin + "/",) if quasar_origin else ())
    if not url.startswith(allowed):
        msg = "unsupported_origin"
        raise TypeError(msg)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(  # noqa: S310 - origins allowlisted above, redirects refused.
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
    )
    with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        content = response.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            msg = "oversized_response"
            raise TypeError(msg)
        return json.loads(content) if content else None


def atomic_json(path: Path, value: dict) -> None:
    """Persist private monitor state atomically before side effects."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".pending")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def load_config() -> dict:
    """Read optional nonsecret policy overrides for this home."""
    configured = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    if not isinstance(configured, dict):
        msg = "invalid_configuration"
        raise TypeError(msg)
    return {**DEFAULTS, **configured}


def ha_token() -> str:
    """Read the existing local HA credential without shell evaluation."""
    for line in (Path.home() / ".env").read_text().splitlines():
        key, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and key.strip() == "HA_TOKEN":
            return value.strip().strip("'\"")
    msg = "ha_credential_missing"
    raise RuntimeError(msg)


def runtime_entries() -> list:
    """Read existing integration settings without modifying HA storage."""
    value = json.loads(ENTRIES.read_text())["data"]["entries"]
    if not isinstance(value, list):
        msg = "invalid_entries"
        raise TypeError(msg)
    return value


def ma_token() -> str:
    """Read the existing local Music Assistant integration credential."""
    entry = next(
        (e for e in runtime_entries() if e.get("domain") == "music_assistant"), None
    )
    token = entry and entry.get("data", {}).get("token")
    if not isinstance(token, str) or not token:
        msg = "ma_credential_missing"
        raise RuntimeError(msg)
    return token


def ma_command(command: str, args: dict | None = None) -> object:
    """Use the authenticated local Music Assistant API."""
    data = request_json(
        MA + "/api", ma_token(), {"command": command, "args": args or {}}
    )
    if isinstance(data, dict) and ("error_code" in data or "error" in data):
        msg = "ma_api_error"
        raise RuntimeError(msg)
    return data


def _provider_reason(provider: dict, runtime: dict | None) -> str:
    if provider.get("last_error"):
        return "provider_error"
    if runtime is None:
        return "not_loaded"
    return "healthy" if runtime.get("available") is True else "unavailable"


def _provider_check(provider: dict, live: dict) -> dict | None:
    if (
        provider.get("domain") not in {"hass", "hass_players"}
        or provider.get("enabled") is False
    ):
        return None
    if provider.get("enabled") is not True or not isinstance(
        provider.get("instance_id"), str
    ):
        message = "invalid_provider_contract"
        raise TypeError(message)
    reason = _provider_reason(provider, live.get(provider["instance_id"]))
    return {
        "id": "ma.provider." + provider["instance_id"],
        "domain": provider["domain"],
        "ok": reason == "healthy",
        "reason": reason,
    }


def _missing_provider_checks(configured: list) -> list:
    domains = {p.get("domain") for p in configured}
    return [
        {
            "id": "ma.provider." + domain,
            "domain": domain,
            "ok": False,
            "reason": "configuration_missing",
        }
        for domain in ("hass", "hass_players")
        if domain not in domains
    ]


def provider_checks(configured: list, loaded: list) -> list:
    """Check enabled transport providers, ignoring intentionally disabled ones."""
    if (
        not isinstance(configured, list)
        or not isinstance(loaded, list)
        or not configured
    ):
        message = "invalid_provider_response"
        raise TypeError(message)
    if any(not isinstance(p, dict) for p in [*configured, *loaded]):
        message = "invalid_provider_entry"
        raise TypeError(message)
    live = {p["instance_id"]: p for p in loaded if "instance_id" in p}
    checks = []
    for provider in configured:
        check = _provider_check(provider, live)
        if check is not None:
            checks.append(check)
    checks.extend(_missing_provider_checks(configured))
    return checks


def speech_state(states: list, config: dict, now: float) -> dict:
    """Read current presence and playback without location polling."""
    by_id = {
        s["entity_id"]: s for s in states if isinstance(s, dict) and "entity_id" in s
    }
    present = False
    for entity in config["persons"]:
        person = by_id.get(entity, {})
        source = person.get("attributes", {}).get("source")
        tracker = by_id.get(source, {})
        if (
            person.get("state") == "home"
            and tracker.get("state") == "home"
            and not tracker.get("attributes", {}).get("restored")
        ):
            present = True
    speaker = by_id.get(config["speaker"], {})
    attrs = speaker.get("attributes", {})
    return {
        "present": present,
        "observed_at": now,
        "speaker": {
            "state": speaker.get("state"),
            "muted": attrs.get("is_volume_muted"),
            "volume": attrs.get("volume_level"),
            "assumed": attrs.get("assumed_state", False),
        },
        "playback_active": any(
            by_id.get(e, {}).get("state") in {"playing", "buffering"}
            for e in config["playback_entities"]
        ),
    }


def _integration_checks(entries: object, config: dict) -> list:
    if not isinstance(entries, list) or not entries:
        message = "invalid_integration_response"
        raise TypeError(message)
    checks = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("domain"), str):
            message = "invalid_integration_entry"
            raise TypeError(message)
        if entry.get("domain") not in config["integration_domains"] or entry.get(
            "disabled_by"
        ):
            continue
        checks.append(
            {
                "id": "ha.integration." + entry["entry_id"],
                "domain": entry["domain"],
                "ok": entry.get("state") == "loaded",
                "reason": entry.get("state", "unknown"),
            }
        )
    return checks


def _observe_ha(result: dict, config: dict) -> None:
    try:
        token = ha_token()
        alive = request_json(HA + "/api/", token)
        if not isinstance(alive, dict) or alive.get("message") != "API running.":
            msg = "invalid_ha_api_response"
            raise TypeError(msg)
        result["checks"].append({"id": "ha.api", "ok": True})
        states = request_json(HA + "/api/states", token)
        if not isinstance(states, list):
            msg = "invalid_entity_response"
            raise TypeError(msg)
        result["speech"] = speech_state(states, config, time.time())
        atomic_json(ROOT / "speech.json", result["speech"])
        entries = request_json(HA + "/api/config/config_entries/entry", token)
        result["checks"].extend(_integration_checks(entries, config))
    except ERRORS as error:
        if not any(c["id"] == "ha.api" for c in result["checks"]):
            result["checks"].append(
                {"id": "ha.api", "ok": False, "reason": type(error).__name__}
            )
        else:
            result["checks"].append(
                {"id": "monitor.ha", "ok": False, "reason": type(error).__name__}
            )
        cache = ROOT / "speech.json"
        if "speech" not in result and cache.exists():
            result["speech"] = json.loads(cache.read_text())


def _observe_music(result: dict) -> None:
    try:
        configured = ma_command("config/providers")
        loaded = ma_command("providers")
        providers = provider_checks(configured, loaded)
        result["checks"].append({"id": "ma.api", "ok": True})
        result["checks"].extend(providers)
    except ERRORS as error:
        result["checks"].append(
            {"id": "ma.api", "ok": False, "reason": type(error).__name__}
        )


def probe() -> dict:
    """Return fresh, sanitized HA and MA health evidence."""
    config = load_config()
    result = {"observed_at": time.time(), "complete": True, "checks": []}
    _observe_ha(result, config)
    _observe_music(result)
    result["observed_at"] = time.time()
    return result


def speech_allowed(
    speech: dict,
    now: float,
    local_hour: int,
    *,
    quiet_start: int = 22,
    quiet_end: int = 9,
) -> str | None:
    """Suppress speech outside trusted presence and speaker conditions."""
    quiet = (
        quiet_start <= local_hour < quiet_end
        if quiet_start < quiet_end
        else quiet_start <= local_hour or local_hour < quiet_end
    )
    if quiet:
        return "quiet_hours"
    observed = speech.get("observed_at")
    if (
        type(observed) not in (float, int)
        or not 0 <= now - observed <= PRESENCE_CACHE_AGE
    ):
        return "stale_presence"
    if speech.get("present") is not True:
        return "nobody_confirmed_home"
    speaker = speech.get("speaker", {})
    if speaker.get("state") not in {
        "idle",
        "paused",
        "playing",
        "buffering",
        "on",
    } or speaker.get("assumed"):
        return "speaker_unavailable"
    volume = speaker.get("volume")
    if speaker.get("muted") is True or volume == 0:
        return "muted"
    unknown = (
        speaker.get("muted") is not False
        or type(volume) not in (float, int)
        or not 0 < volume <= 1
    )
    return "unknown_speaker_state" if unknown else None


def announcement_payload(config: dict, speech: dict) -> dict:
    """Use native speech with the requested temporary volume."""
    return {
        "entity_id": config["speaker"],
        "media_content_type": "text",
        "media_content_id": PHRASE,
        "extra": {"volume_level": 0.6 if speech.get("playback_active") else 0.4},
    }


def _quasar_settings(entry: dict) -> tuple[str, str | None] | None:
    if entry.get("domain") != "aisis_speaker":
        return None
    data = entry.get("data", {})
    host = str(data.get("host", ""))
    if not ipaddress.ip_address(host).is_private or data.get("port") != QUASAR_PORT:
        return None
    return f"http://{host}:31116", data.get("api_token")


def _direct_target_connected(devices: object, expected: str) -> bool:
    return isinstance(devices, list) and any(
        isinstance(d, dict) and d.get("duid") == expected and d.get("connected") is True
        for d in devices
    )


def _direct_notice(origin: str, token: str | None, expected: str) -> dict:
    current = request_json(
        origin + f"/devices/{expected}/state", token, quasar_origin=origin
    )
    state = current.get("deviceState", {})
    volume = state.get("SoundLevel")
    if (
        state.get("SoundMuted")
        or type(volume) not in (float, int)
        or not 0 < volume <= MAX_DIRECT_VOLUME
    ):
        return {"status": "suppressed", "reason": "direct_mute_or_volume"}
    if (state.get("AudioPlayer") or {}).get("PlayerState", "").lower() in {
        "playing",
        "play",
    }:
        return {"status": "suppressed", "reason": "direct_playback_active"}
    request_json(
        origin + f"/devices/{expected}/push/text",
        token,
        {"eventText": PHRASE},
        quasar_origin=origin,
    )
    return {"status": "accepted", "route": "direct_quasar"}


def direct_speech(config: dict, _speech: dict) -> dict:
    """Speak only through the exact configured room's existing bridge."""
    expected = config["speaker"].removeprefix("media_player.yandex_station_").upper()
    if not expected or expected == config["speaker"].upper():
        return {"status": "suppressed", "reason": "no_matching_direct_target"}
    for entry in runtime_entries():
        settings = _quasar_settings(entry)
        if settings is None:
            continue
        origin, token = settings
        devices = request_json(origin + "/devices", token, quasar_origin=origin)
        if _direct_target_connected(devices, expected):
            return _direct_notice(origin, token, expected)
    return {"status": "suppressed", "reason": "no_matching_direct_target"}


def announce(payload: dict) -> dict:
    """Request one guarded native announcement after dispatch."""
    config = load_config()
    now = time.time()
    hour = datetime.fromtimestamp(now, ZoneInfo(config["timezone"])).hour
    speech = payload.get("speech", {})
    token = None
    try:
        token = ha_token()
        states = request_json(HA + "/api/states", token)
        if not isinstance(states, list):
            msg = "invalid_entity_response"
            raise TypeError(msg)
        speech = speech_state(states, config, now)
    except ERRORS:
        token = None
    reason = speech_allowed(
        speech,
        now,
        hour,
        quiet_start=config["quiet_start"],
        quiet_end=config["quiet_end"],
    )
    if reason:
        return {"status": "suppressed", "reason": reason}
    if token:
        # Supported YandexStation native text route; its temporary Alice volume restores
        # when speaking ends. This monitor never replaces or pauses the queue.
        request_json(
            HA + "/api/services/media_player/play_media",
            token,
            announcement_payload(config, speech),
            timeout=12,
        )
        return {
            "status": "accepted",
            "route": "ha_yandex_station",
            "physical_heard": "unverified",
        }
    return direct_speech(config, speech)


def reconnect() -> dict:
    """Request one supported Home Assistant provider reload."""
    configured = ma_command("config/providers")
    provider = next(
        (
            p
            for p in configured
            if p.get("domain") == "hass" and p.get("enabled") is True
        ),
        None,
    )
    if provider is None:
        return {"status": "skipped", "reason": "hass_disabled_or_missing"}
    ma_command("config/providers/reload", {"instance_id": provider["instance_id"]})
    return {"status": "requested", "provider": provider["instance_id"]}


def init_log() -> Path:
    """Create a private run log and expire this monitor's old logs."""
    directory = Path("/tmp/ha-smart-home-probe")  # noqa: S108 - private 0700 log directory.
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    for old in directory.glob("run-*.log"):
        if (
            old.is_file()
            and old.stat().st_uid == os.getuid()
            and time.time() - old.stat().st_mtime > LOG_RETENTION
        ):
            old.unlink()
    path = directory / f"run-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}.log"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.StreamHandler(os.fdopen(fd, "w")),
        ],
    )
    LOG.info("Starting; log=%s", path)
    return path


def main() -> int:
    """Execute one bounded probe, monitor tick or incident runner."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("probe", "announce", "reconnect"))
    args = parser.parse_args()
    log = init_log()
    try:
        result = (
            probe()
            if args.command == "probe"
            else reconnect()
            if args.command == "reconnect"
            else announce(json.load(sys.stdin))
        )
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        LOG.info("Completed; command=%s log=%s", args.command, log)
    except ERRORS as error:
        LOG.error(  # noqa: TRY400 - never log credential-bearing exceptions.
            "Failed; command=%s error=%s log=%s",
            args.command,
            type(error).__name__,
            log,
        )
        return 1
    else:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
