"""Lifecycle adapter for the existing Android TV Remote keyboard provider."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import uuid4

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .keyboard import KeyboardError


def native_remote_id(hass, media_entity_id: str) -> str | None:
    registry = er.async_get(hass)
    media = registry.async_get(media_entity_id)
    if media is None or media.platform != "androidtv_remote" or not media.config_entry_id:
        return None
    matches = [record.entity_id for record in
               er.async_entries_for_config_entry(registry, media.config_entry_id)
               if record.domain == "remote" and record.platform == "androidtv_remote"
               and record.disabled_by is None]
    return matches[0] if len(matches) == 1 else None


class AndroidKeyboardProvider:
    """Rebind across native entry reloads without retaining stale field callbacks."""

    def __init__(self, hass, media_entity_id: str) -> None:
        self.hass, self.media_entity_id = hass, media_entity_id
        self._provider = None
        self._remove_provider = None
        self._remove_state = None
        self._listeners: set[Callable] = set()
        self._tasks: set[asyncio.Task] = set()
        self._generation = 0
        self._closed = False
        self._blank = self._empty()

    @staticmethod
    def _empty():
        return {"session_id": uuid4().hex, "revision": 0, "focused": False,
                "sensitive": False, "status": "unavailable", "fresh_at": None,
                "selection": None, "capabilities": {}}

    def _resolve(self):
        record = er.async_get(self.hass).async_get(self.media_entity_id)
        if record is None or record.platform != "androidtv_remote" or not record.config_entry_id:
            return None
        entry = self.hass.config_entries.async_get_entry(record.config_entry_id)
        runtime = getattr(entry, "runtime_data", None) if entry else None
        return getattr(runtime, "keyboard", None)

    def _publish(self, snapshot, generation: int) -> None:
        if self._closed or generation != self._generation:
            return
        for listener in tuple(self._listeners):
            listener(snapshot)

    def _bind(self):
        if self._closed:
            return None
        provider = self._resolve()
        if provider is self._provider:
            return provider
        self._generation += 1
        generation = self._generation
        if self._remove_provider is not None:
            self._remove_provider()
            self._remove_provider = None
        self._provider = provider
        self._blank = self._empty()
        self._publish(self._blank, generation)
        if provider is not None:
            self._remove_provider = provider.subscribe(
                lambda snapshot: self._publish(snapshot, generation))
        return provider

    def subscribe(self, listener):
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    async def async_snapshot(self) -> dict:
        provider = self._bind()
        if provider is None:
            return dict(self._blank)
        generation = self._generation
        snapshot = await provider.async_snapshot()
        if self._closed or generation != self._generation or self._resolve() is not provider:
            return dict(self._blank)
        return snapshot

    @callback
    def _state_changed(self, event) -> None:
        if self._closed:
            return
        previous = self._provider
        provider = self._bind()
        if provider is not previous and provider is not None:
            task = self.hass.async_create_task(self._refresh(self._generation))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _refresh(self, generation: int) -> None:
        try:
            self._publish(await self.async_snapshot(), generation)
        except Exception:
            self._publish(self._blank, generation)

    async def async_start(self) -> None:
        if self._closed:
            return
        self._remove_state = async_track_state_change_event(
            self.hass, [self.media_entity_id], self._state_changed)
        self._bind()

    async def _operate(self, action: str, *args):
        provider = self._bind()
        if provider is None:
            raise KeyboardError("unavailable")
        generation = self._generation
        result = await getattr(provider, "async_" + action)(*args)
        if self._closed or generation != self._generation or self._resolve() is not provider:
            raise KeyboardError("session_changed")
        return result

    async def async_edit(self, session_id, expected_revision, text, selection=None):
        return await self._operate("edit", session_id, expected_revision, text, selection)

    async def async_commit(self, session_id, expected_revision):
        return await self._operate("commit", session_id, expected_revision)

    async def async_close(self, session_id, expected_revision):
        return await self._operate("close", session_id, expected_revision)

    async def async_shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        if self._remove_state is not None:
            self._remove_state()
        if self._remove_provider is not None:
            self._remove_provider()
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._tasks.clear()
        self._provider = None
        self._listeners.clear()
