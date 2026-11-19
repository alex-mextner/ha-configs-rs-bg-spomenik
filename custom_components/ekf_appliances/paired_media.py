"""Home Assistant bindings for the independent display/player power pairs."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.components.media_player import MediaPlayerEntity
    from homeassistant.core import Event, HomeAssistant

    from .runtime import ApplianceRuntime
    from .samsung_tv import SamsungTvMediaPlayer


from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import CONF_TV_ANDROIDTV_ENTITY_ID, CONF_TV_KODI_ENTITY_ID
from .power_pair import PowerEndpoint, PowerPair, PowerPairHooks


def observed_boolean(
    hass: HomeAssistant, entity_id: str | None, *, max_age: float | None = None
) -> bool | None:
    """
    Accept live HA State observations, never unavailable or restored cache.

    Use the in-memory last_reported property. HA 2026.9 caches REST state JSON,
    which can show the initial timestamp even as unchanged successful polls
    refresh this property. Both polling integrations clear failed observations.
    """
    state = hass.states.get(entity_id) if entity_id is not None else None
    if (
        state is None
        or state.attributes.get("restored")
        or state.state not in ("on", "off")
    ):
        return None
    if (
        max_age is not None
        and (dt_util.utcnow() - state.last_reported).total_seconds() > max_age
    ):
        return None
    return state.state == "on"


async def async_guarded_wake(
    runtime: ApplianceRuntime, pair: PowerPair | None, expected_revision: str
) -> None:
    """Preserve physical-power safety before accepting a queued wake."""
    if pair is None:
        error_message = "Power synchronization is not ready"
        raise HomeAssistantError(error_message)
    if pair is runtime.projector_power_pair and runtime.projector_shutting_down:
        error_message = "Projector is shutting down; queued wake was discarded"
        raise HomeAssistantError(error_message)
    if pair.endpoints["display"].read() is False and not runtime.available:
        error_message = f"IR transmitter {runtime.remote_entity_id} is unavailable"
        raise HomeAssistantError(error_message)
    await pair.wake_if_current(expected_revision)


def _projector_pair(
    runtime: ApplianceRuntime, changed: Callable[[], None]
) -> PowerPair:
    """Couple the selected Kodi host only to actual HDMI and process feedback."""
    hass = runtime.hass

    async def kodi_power(*, target: bool) -> None:
        await hass.services.async_call(
            "rest_command",
            "host_kodi_start" if target else "host_kodi_stop",
            {},
            blocking=True,
        )

    return PowerPair(
        "Projector / Kodi",
        PowerEndpoint(
            lambda: observed_boolean(
                hass, runtime.projector_power_entity_id, max_age=12
            ),
            lambda target: runtime.async_projector_power(turn_on=target),
        ),
        PowerEndpoint(
            lambda: observed_boolean(hass, "sensor.kodi_service_state", max_age=20),
            lambda target: kodi_power(target=target),
        ),
        hooks=PowerPairHooks(
            changed=changed,
            create_task=hass.async_create_task,
            requested=lambda target: runtime.note_projector_off_requested(
                target=target
            ),
        ),
    )


def _track_android_power(
    runtime: ApplianceRuntime, television: SamsungTvMediaPlayer | None, pair: PowerPair
) -> None:
    """Follow the media router's registry identity across entity-ID changes."""
    hass = runtime.hass
    unsubscribers = []

    @callback
    def remove() -> None:
        for unsubscribe in unsubscribers:
            unsubscribe()
        unsubscribers.clear()

    @callback
    def changed(_event: Event | None) -> None:
        pair.observe()

    @callback
    def rebind(_event: Event | None = None) -> None:
        remove()
        entity_id = television.media_routing.entity_id(CONF_TV_ANDROIDTV_ENTITY_ID)
        if entity_id:
            unsubscribers.extend(
                (
                    async_track_state_change_event(hass, [entity_id], changed),
                    async_track_entity_registry_updated_event(
                        hass, [entity_id], rebind
                    ),
                )
            )
        pair.observe()

    rebind()
    runtime.entry.async_on_unload(remove)


def install_power_pairs(
    runtime: ApplianceRuntime,
    projector: MediaPlayerEntity,
    television: SamsungTvMediaPlayer | None = None,
) -> None:
    """Register after platforms are created; perform no power I/O during setup."""
    hass = runtime.hass
    entry = runtime.entry

    def watch(pair: PowerPair, entity_ids: list[str]) -> None:
        @callback
        def changed(_event: Event | None) -> None:
            pair.observe()

        if entity_ids:
            entry.async_on_unload(
                async_track_state_change_event(hass, entity_ids, changed)
            )

        async def start(_hass: HomeAssistant) -> None:
            pair.start()

        entry.async_on_unload(async_at_started(hass, start))
        runtime.power_pairs.append(pair)

    def notify(entity: MediaPlayerEntity) -> Callable[[], None]:
        @callback
        def changed() -> None:
            if entity.hass is not None and entity.entity_id is not None:
                entity.async_write_ha_state()

        return changed

    if entry.data.get(CONF_TV_KODI_ENTITY_ID):
        projector_pair = _projector_pair(runtime, notify(projector))
        runtime.projector_power_pair = projector_pair
        watch(
            projector_pair,
            [runtime.projector_power_entity_id, "sensor.kodi_service_state"],
        )

    if television is None:
        return
    routing = television.media_routing
    if not routing.entity_id(CONF_TV_ANDROIDTV_ENTITY_ID):
        return

    async def android_power(*, target: bool) -> None:
        android_id = routing.entity_id(CONF_TV_ANDROIDTV_ENTITY_ID)
        if android_id is None:
            error_message = "The configured Android TV player is unavailable"
            raise HomeAssistantError(error_message)
        await hass.services.async_call(
            "media_player",
            "turn_on" if target else "turn_off",
            {"entity_id": android_id},
            blocking=True,
        )

    television_pair = PowerPair(
        "Samsung / Chromecast",
        PowerEndpoint(
            lambda: television.coordinator.data,
            television._async_power,  # noqa: SLF001 - Bind the sibling TV power endpoint without a media service.
            allow_unknown_command=True,  # Samsung has verified discrete ON/OFF.
        ),
        PowerEndpoint(
            lambda: observed_boolean(
                hass, routing.entity_id(CONF_TV_ANDROIDTV_ENTITY_ID)
            ),
            lambda target: android_power(target=target),
            wait_for_feedback=True,
        ),
        hooks=PowerPairHooks(
            changed=notify(television),
            create_task=hass.async_create_task,
        ),
    )
    runtime.tv_power_pair = television_pair
    entry.async_on_unload(
        television.coordinator.async_add_listener(television_pair.observe)
    )
    _track_android_power(runtime, television, television_pair)
    watch(television_pair, [])
