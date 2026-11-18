"""Text-free keyboard notifications to registered household phones at home."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers import entity_registry

from .keyboard import KeyboardTarget, allowed

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event, HomeAssistant


def _phone(entry: ConfigEntry) -> bool:
    data = entry.data
    os_name = str(data.get("os_name", "")).lower()
    model = str(data.get("model", "")).lower()
    app_id = str(data.get("app_id", "")).lower()
    identity = f"{os_name} {model} {app_id}"
    if any(
        word in identity
        for word in (
            "watch",
            "androidtv",
            "android tv",
            "television",
            "chromecast",
            "shield",
            "ipad",
            "tablet",
            "tvos",
            "apple tv",
        )
    ):
        return False
    if os_name in ("ios", "iphone os"):
        return model.startswith("iphone")
    return os_name == "android" and bool(model) and "tv" not in model.split()


def _registrations(hass: HomeAssistant) -> dict[str, Any]:
    """Read HA's actual registered service→webhook mapping; never guess a service."""
    entries = hass.data.get("mobile_app", {}).get("config_entries", {})
    result = {}
    for platform in hass.data.get("notify_services", {}).get("mobile_app", ()):
        for service_name, webhook_id in getattr(
            platform, "registered_targets", {}
        ).items():
            entry = entries.get(webhook_id)
            if entry is not None and hass.services.has_service("notify", service_name):
                result[service_name] = entry
    return result


def _at_home(hass: HomeAssistant, entry: ConfigEntry, user_id: str) -> bool:
    # A watch or another phone can keep person=home while this phone is away.
    # Prefer the freshest usable observation belonging to this exact registration.
    registry = entity_registry.async_get(hass)
    own_states = []
    for record in entity_registry.async_entries_for_config_entry(
        registry, entry.entry_id
    ):
        if record.domain != "device_tracker" or record.disabled_by is not None:
            continue
        state = hass.states.get(record.entity_id)
        if (
            state is not None
            and state.state not in ("unknown", "unavailable", "")
            and not state.attributes.get("restored")
        ):
            own_states.append(state)
    if own_states:
        return max(own_states, key=lambda state: state.last_reported).state == "home"
    return any(
        person.attributes.get("user_id") == user_id
        and person.state == "home"
        and not person.attributes.get("restored")
        for person in hass.states.async_all("person")
    )


async def async_phone_targets(hass: HomeAssistant, entity_id: str) -> dict[str, str]:
    """Resolve eligible registered phones without reading location coordinates."""
    result = {}
    for service_name, entry in _registrations(hass).items():
        user_id = entry.data.get("user_id")
        if (
            not isinstance(user_id, str)
            or not _phone(entry)
            or getattr(entry, "disabled_by", None)
        ):
            continue
        user = await hass.auth.async_get_user(user_id)
        if not allowed(user, entity_id) or not _at_home(hass, entry, user_id):
            continue
        result[service_name] = user_id
    return result


class KeyboardNotifications:
    """One notification per field and phone; no retry on each typed character."""

    def __init__(self, hass: HomeAssistant, targets: dict[str, KeyboardTarget]) -> None:
        """Track household phone presence and session notification ownership."""
        self.hass, self.targets = hass, targets
        self._closed = False
        self._latest: dict[str, dict] = {}
        self._generation = dict.fromkeys(targets, 0)
        self._locks = {pair: asyncio.Lock() for pair in targets}
        self._sessions: dict[str, str] = {}
        self._attempted: dict[str, set[str]] = {}
        self._sent: dict[str, dict[str, str]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._remove = [
            hass.bus.async_listen("state_changed", self._presence_changed),
            hass.bus.async_listen("service_registered", self._service_changed),
        ]

    @staticmethod
    def _safe(snapshot: Any) -> bool:
        return bool(
            isinstance(snapshot, Mapping)
            and isinstance(snapshot.get("session_id"), str)
            and snapshot["session_id"]
            and snapshot.get("focused") is True
            and snapshot.get("sensitive") is False
            and snapshot.get("status") == "ready"
            and isinstance(snapshot.get("capabilities"), Mapping)
            and snapshot["capabilities"].get("edit") is True
        )

    @callback
    def _presence_changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id", "")
        if entity_id.startswith(("person.", "device_tracker.")):
            self._schedule_refresh()

    @callback
    def _service_changed(self, event: Event) -> None:
        if event.data.get("domain") == "notify":
            self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if self._closed:
            return
        for pair, snapshot in tuple(self._latest.items()):
            task = self.hass.async_create_task(self.async_refresh(pair, snapshot))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _clear(self, pair: str, services: set[str] | None = None) -> None:
        all_sent = self._sent.get(pair, {})
        sent = {
            name: user
            for name, user in all_sent.items()
            if services is None or name in services
        }
        for name in sent:
            all_sent.pop(name, None)
        if not all_sent:
            self._sent.pop(pair, None)
        registrations = _registrations(self.hass)
        for service_name, user_id in sent.items():
            entry = registrations.get(service_name)
            if entry is None or entry.data.get("user_id") != user_id:
                continue
            # Clearing a phone notification is best effort; no external error text.
            with suppress(Exception):
                await self.hass.services.async_call(
                    "notify",
                    service_name,
                    {
                        "message": "clear_notification",
                        "data": {"tag": "ekf-keyboard-" + pair},
                    },
                    blocking=True,
                )

    async def async_refresh(self, pair: str, snapshot: dict) -> None:
        """Update one field notification without retaining editor text."""
        if self._closed or pair not in self.targets:
            return
        # Do not retain input text even if a caller passes a provider snapshot.
        metadata = {
            key: snapshot.get(key)
            for key in ("session_id", "focused", "sensitive", "status", "capabilities")
        }
        self._latest[pair] = metadata
        self._generation[pair] += 1
        generation = self._generation[pair]
        async with self._locks[pair]:
            if not self._current(pair, generation):
                return
            session = metadata.get("session_id") if self._safe(metadata) else None
            await self._update_session(pair, session)
            if session is None or not self._current(pair, generation):
                return
            eligible = await async_phone_targets(
                self.hass, self.targets[pair].entity_id
            )
            if not self._current(pair, generation):
                return
            withdrawn = set(self._sent.get(pair, {})) - eligible.keys()
            if withdrawn:
                await self._clear(pair, withdrawn)
            await self._notify_eligible(pair, generation, eligible)

    def _current(self, pair: str, generation: int) -> bool:
        return not self._closed and generation == self._generation[pair]

    async def _update_session(self, pair: str, session: str | None) -> None:
        if session != self._sessions.get(pair):
            await self._clear(pair)
            self._sessions.pop(pair, None)
            self._attempted[pair] = set()
            if session is not None:
                self._sessions[pair] = session

    async def _notify_eligible(
        self, pair: str, generation: int, eligible: dict[str, str]
    ) -> None:
        attempted = self._attempted.setdefault(pair, set())
        for service_name, user_id in eligible.items():
            if not self._current(pair, generation):
                break
            if service_name in attempted:
                continue
            # Revalidate this exact registered phone after any preceding push await.
            registration = _registrations(self.hass).get(service_name)
            if registration is None or registration.data.get("user_id") != user_id:
                continue
            user = await self.hass.auth.async_get_user(user_id)
            if (
                not self._current(pair, generation)
                or not allowed(user, self.targets[pair].entity_id)
                or not _at_home(self.hass, registration, user_id)
            ):
                continue
            attempted.add(service_name)
            self._sent.setdefault(pair, {})[service_name] = user_id
            await self._send_notification(pair, service_name)

    async def _send_notification(self, pair: str, service_name: str) -> None:
        uri = "/living-room/keyboard?pair=" + pair
        # The attempted send is recorded even if the transport acknowledgement fails.
        with suppress(Exception):
            await self.hass.services.async_call(
                "notify",
                service_name,
                {
                    "title": "Клавиатура в гостиной",
                    "message": (
                        "Введите текст для телевизора"
                        if pair == "tv"
                        else "Введите текст для проектора"
                    ),
                    "data": {
                        "tag": "ekf-keyboard-" + pair,
                        "url": uri,
                        "clickAction": uri,
                        "actions": [
                            {"action": "URI", "title": "Открыть клавиатуру", "uri": uri}
                        ],
                    },
                },
                blocking=True,
            )

    async def async_close(self) -> None:
        """Remove presence listeners and clear only owned notifications."""
        if self._closed:
            return
        self._closed = True
        for remove in self._remove:
            remove()
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        for pair in tuple(self._locks):
            async with self._locks[pair]:
                await self._clear(pair)
        self._latest.clear()
        self._sessions.clear()
        self._attempted.clear()
