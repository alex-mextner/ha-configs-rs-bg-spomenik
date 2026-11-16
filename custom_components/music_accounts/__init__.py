"""
Music accounts per person (aisis#17).

Play, search, and like with a person's own accounts. The mapping contains no
secrets and lives next to the voice studio's guests.json. It uses the same
speaker IDs: "user-<HA user id>" for household members and "guest-<slug>" for
guests. scripts/ma_accounts.py writes it through the host helpers ytm-cookie,
yandex-music-token, and music-accounts. This integration reads it on every call.

The mapping holds a version, default speaker, and people keyed by speaker ID.
Each entry contains its name, ma_user, and providers keyed by service domain.
Guest entries also contain guest=true and expires_at as an ISO timestamp.

Each person with own accounts has a Music Assistant user whose provider filter
holds their instances and the household default for services they lack.
MA's play_media/search username selects that person's streams and search.
Likes use the person_likes plugin in docker/music-assistant/person_likes,
which changes exactly one account; MA 2.10 itself would like in every account
of a service.

The caller identifies the speaker through speaker (user-.../guest-...) or
person (a person.* entity), supplied by recognition or voice commands.
An absent, unknown, or expired guest speaker plays and searches as the
household default. A like always requires the person's own account.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

if TYPE_CHECKING:
    from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)
DOMAIN = "music_accounts"
MA_DOMAIN = "music_assistant"
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
MAPPING = Path("/config/oww-dataset/voices/music_accounts.json")

WHO = {
    vol.Optional("speaker"): cv.string,
    vol.Optional("person"): cv.entity_id,
}
PLAY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required("media_id"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("media_type"): cv.string,
        vol.Optional("artist"): cv.string,
        vol.Optional("album"): cv.string,
        vol.Optional("enqueue"): cv.string,
        vol.Optional("radio_mode"): cv.boolean,
        **WHO,
    }
)
SEARCH_SCHEMA = vol.Schema(
    {
        vol.Required("name"): cv.string,
        vol.Optional("media_type"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("artist"): cv.string,
        vol.Optional("album"): cv.string,
        vol.Optional("limit", default=5): vol.Coerce(int),
        **WHO,
    }
)
LIKE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional("liked", default=True): cv.boolean,
        vol.Optional("dry_run", default=False): cv.boolean,
        **WHO,
    }
)


def _read_mapping() -> dict[str, Any]:
    try:
        data = json.loads(MAPPING.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        error_message = f"Cannot read {MAPPING}: {err}"
        raise HomeAssistantError(error_message) from err
    return data if isinstance(data, dict) else {}


def _expired(entry: dict[str, Any], now: dt.datetime) -> bool:
    expires = entry.get("expires_at")
    return bool(expires) and dt.datetime.fromisoformat(expires) < now


class Accounts:
    """One read of the mapping plus the who-is-it resolution."""

    def __init__(self, hass: HomeAssistant, data: dict[str, Any]) -> None:
        """Retain the account mapping and capture the time used for expiry."""
        self.hass = hass
        self.people: dict[str, dict[str, Any]] = data.get("people") or {}
        self.default_speaker: str | None = data.get("default")
        self.now = dt.datetime.now(dt.UTC)

    @property
    def default(self) -> dict[str, Any] | None:
        """Return the configured household default account entry."""
        return self.people.get(self.default_speaker or "")

    def speaker_of(self, call: ServiceCall) -> str | None:
        """Resolve the supplied person entity or explicit speaker identifier."""
        if person := call.data.get("person"):
            state = self.hass.states.get(person)
            user_id = state.attributes.get("user_id") if state else None
            if not user_id:
                error_message = f"{person} is not linked to a Home Assistant user"
                raise ServiceValidationError(error_message)
            return f"user-{user_id}"
        return call.data.get("speaker") or None

    def own(self, speaker: str | None) -> dict[str, Any] | None:
        """Return the speaker's own account entry if it has not expired."""
        entry = self.people.get(speaker or "")
        if not entry or not entry.get("providers") or _expired(entry, self.now):
            return None
        return entry

    def ma_user(self, speaker: str | None) -> str | None:
        """Choose the speaker's MA user, then the household default, or none."""
        entry = self.own(speaker) or self.default
        return (entry or {}).get("ma_user") or None


async def _accounts(hass: HomeAssistant) -> Accounts:
    return Accounts(hass, await hass.async_add_executor_job(_read_mapping))


def _mass_entry(hass: HomeAssistant) -> Any:
    for entry in hass.config_entries.async_entries(MA_DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return entry
    error_message = "Music Assistant is not loaded"
    raise HomeAssistantError(error_message)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the music_accounts services."""
    del config  # Preserve the public callback parameter names.

    async def play(call: ServiceCall) -> None:
        accounts = await _accounts(hass)
        data = {k: v for k, v in call.data.items() if k not in ("speaker", "person")}
        if username := accounts.ma_user(accounts.speaker_of(call)):
            data["username"] = username
        await hass.services.async_call(
            MA_DOMAIN, "play_media", data, blocking=True, context=call.context
        )

    async def search(call: ServiceCall) -> ServiceResponse:
        accounts = await _accounts(hass)
        data = {k: v for k, v in call.data.items() if k not in ("speaker", "person")}
        data["config_entry_id"] = _mass_entry(hass).entry_id
        if username := accounts.ma_user(accounts.speaker_of(call)):
            data["username"] = username
        return await hass.services.async_call(
            MA_DOMAIN,
            "search",
            data,
            blocking=True,
            context=call.context,
            return_response=True,
        )

    async def like(call: ServiceCall) -> ServiceResponse:
        accounts = await _accounts(hass)
        speaker = accounts.speaker_of(call)
        entry = accounts.own(speaker)
        if entry is None:
            error_message = (
                f"{speaker or 'An unknown speaker'} has no own music account "
                "(or the guest "
                "account expired); a like is never sent to someone else's account"
            )
            raise ServiceValidationError(error_message)
        reg = er.async_get(hass).async_get(call.data[ATTR_ENTITY_ID])
        if reg is None or reg.platform != MA_DOMAIN:
            error_message = (
                f"{call.data[ATTR_ENTITY_ID]} is not a Music Assistant player"
            )
            raise ServiceValidationError(error_message)
        mass = _mass_entry(hass).runtime_data.mass
        try:
            result = await mass.send_command(
                "person_likes/like",
                provider_instances=list(entry["providers"].values()),
                player_id=reg.unique_id,
                liked=call.data["liked"],
                dry_run=call.data["dry_run"],
            )
        except (
            Exception
        ) as err:  # MA errors arrive as music_assistant_models exceptions
            error_message = f"Like failed: {err}"
            raise HomeAssistantError(error_message) from err
        _LOGGER.info("%s: %s", entry.get("name") or speaker, result)
        return {"speaker": speaker, "name": entry.get("name"), **(result or {})}

    async def list_accounts(call: ServiceCall) -> ServiceResponse:
        del call  # Preserve the public callback parameter names.
        accounts = await _accounts(hass)
        return {
            "default": accounts.default_speaker,
            "people": [
                {
                    "speaker": speaker,
                    "name": entry.get("name"),
                    "ma_user": entry.get("ma_user"),
                    "providers": entry.get("providers") or {},
                    "guest": bool(entry.get("guest")),
                    "expires_at": entry.get("expires_at"),
                    "expired": _expired(entry, accounts.now),
                }
                for speaker, entry in accounts.people.items()
            ],
        }

    hass.services.async_register(DOMAIN, "play", play, schema=PLAY_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        "search",
        search,
        schema=SEARCH_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "like",
        like,
        schema=LIKE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "accounts", list_accounts, supports_response=SupportsResponse.ONLY
    )
    return True
