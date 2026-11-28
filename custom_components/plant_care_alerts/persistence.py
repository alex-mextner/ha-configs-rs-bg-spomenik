"""Confirm exact durable snapshots through independent public Store readers."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.storage import Store


class PersistenceNotConfirmedError(Exception):
    """The independent persisted data did not confirm the requested snapshot."""


def new_store(hass: HomeAssistant, key: str) -> Store:
    """Create native Stores with identical atomic and off-loop serialization options."""
    storage = import_module("homeassistant.helpers.storage")
    return storage.Store(
        hass, 1, key, atomic_writes=True, serialize_in_event_loop=False
    )


async def async_save_confirmed(
    store: Any, reader: Callable[[], Awaitable[dict | None]], snapshot: dict
) -> None:
    """Require readback because a save call alone cannot acknowledge a native write."""
    await store.async_save(snapshot)
    if await reader() != snapshot:
        message = "Independent storage read did not confirm the persisted snapshot"
        raise PersistenceNotConfirmedError(message)
