"""Lifecycle regressions for private keyboard provider binding and host setup."""
import asyncio
from copy import deepcopy
import importlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4


class Provider:
    def __init__(self, text):
        self.snapshot = {"session_id": uuid4().hex, "revision": 1, "focused": True,
                         "sensitive": False, "text": text, "status": "ready",
                         "capabilities": {"edit": True}, "fresh_at": None}
        self.listeners = set()
        self.pending = None

    def subscribe(self, listener):
        self.listeners.add(listener)
        return lambda: self.listeners.discard(listener)

    async def async_snapshot(self):
        return dict(self.snapshot)

    async def async_edit(self, *args):
        if self.pending is not None:
            await self.pending.wait()
        return dict(self.snapshot)


class KeyboardBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(__file__).resolve().parents[2]
        self.assertTrue((root / "custom_components/ekf_appliances/keyboard_android.py").exists(),
                        "Native keyboard reload adapter is not implemented")
        self.module = importlib.import_module("custom_components.ekf_appliances.keyboard_android")
        self.setup = importlib.import_module("custom_components.ekf_appliances.keyboard_setup")
        self.old, self.new = Provider("OLD-FIELD"), Provider("NEW-FIELD")
        self.entry = SimpleNamespace(runtime_data=SimpleNamespace(keyboard=self.old))
        self.record = SimpleNamespace(platform="androidtv_remote", config_entry_id="native")
        self.registry = SimpleNamespace(async_get=lambda entity: self.record)
        self.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_get_entry=lambda identifier: self.entry),
            async_create_task=asyncio.create_task,
        )
        self.patch_registry = patch.object(self.module.er, "async_get", return_value=self.registry)
        self.patch_registry.start()
        self.adapter = self.module.AndroidKeyboardProvider(self.hass, "media_player.televizor")
        self.observed = []
        self.adapter.subscribe(lambda snapshot: self.observed.append(deepcopy(snapshot)))

    async def asyncTearDown(self):
        if hasattr(self, "adapter"):
            await self.adapter.async_shutdown()
            self.patch_registry.stop()

    async def test_only_unique_remote_in_configured_native_entry_is_permission_target(self):
        remote = SimpleNamespace(entity_id="remote.televizor", platform="androidtv_remote",
                                 domain="remote", disabled_by=None)
        with patch.object(self.module.er, "async_entries_for_config_entry", return_value=[remote]):
            self.assertEqual(self.module.native_remote_id(self.hass, "media_player.televizor"),
                             "remote.televizor")
        with patch.object(self.module.er, "async_entries_for_config_entry", return_value=[remote, remote]):
            self.assertIsNone(self.module.native_remote_id(self.hass, "media_player.televizor"))
        self.record.platform = "cast"
        with patch.object(self.module.er, "async_entries_for_config_entry", return_value=[remote]):
            self.assertIsNone(self.module.native_remote_id(self.hass, "media_player.televizor"))

    async def test_entry_reload_detaches_old_callback_and_late_callback_cannot_restore_old_field(self):
        first = await self.adapter.async_snapshot()
        old_callback = next(iter(self.old.listeners))
        self.entry.runtime_data.keyboard = self.new
        current = await self.adapter.async_snapshot()
        self.assertNotEqual(first["session_id"], current["session_id"])
        self.assertFalse(self.old.listeners)
        self.observed.clear()
        old_callback(self.old.snapshot)
        self.assertEqual(self.observed, [])
        callback = next(iter(self.new.listeners))
        callback(self.new.snapshot)
        self.assertEqual(self.observed[-1]["text"], "NEW-FIELD")
        await self.adapter.async_shutdown()
        self.assertFalse(self.new.listeners)
        callback(self.new.snapshot)
        self.assertEqual(len(self.observed), 1)

    async def test_completed_write_from_previous_native_instance_is_rejected_after_rebind(self):
        first = await self.adapter.async_snapshot()
        self.old.pending = asyncio.Event()
        task = asyncio.create_task(self.adapter.async_edit(first["session_id"], 1, "DRAFT", None))
        await asyncio.sleep(0)
        self.entry.runtime_data.keyboard = self.new
        await self.adapter.async_snapshot()
        self.old.pending.set()
        with self.assertRaises(self.module.KeyboardError) as raised:
            await task
        self.assertEqual(raised.exception.code, "session_changed")
        self.assertNotIn("OLD-FIELD", str(raised.exception))

    async def test_host_origin_and_token_reuse_only_known_resolved_bridge_url(self):
        config = {"rest_command": {"host_kodi_start": {
            "url": "http://192.0.2.10:8787/kodi/start",
            "headers": {"X-HA-Host-Token": "FIXTURE-TOKEN"},
        }}}
        original = deepcopy(config)
        self.assertEqual(self.setup.resolved_host_config(config),
                         ("http://192.0.2.10:8787", "FIXTURE-TOKEN"))
        self.assertEqual(config, original)
        for url in ("ftp://192.0.2.10/kodi/start", "http://user:pass@host/kodi/start",
                    "http://host/arbitrary", "", None):
            config["rest_command"]["host_kodi_start"]["url"] = url
            self.assertIsNone(self.setup.resolved_host_config(config))
        self.assertIsNone(self.setup.resolved_host_config({}))

    async def test_failed_keyboard_start_can_retry_but_unload_remains_closed(self):
        import tempfile
        from homeassistant.core import HomeAssistant

        with tempfile.TemporaryDirectory() as config_dir:
            hass = HomeAssistant(config_dir)
            hass.config_entries = self.hass.config_entries
            runtime = SimpleNamespace()
            entry = SimpleNamespace(
                entry_id="ekf-test", runtime_data=runtime,
                data={self.setup.CONF_TV_ANDROIDTV_ENTITY_ID: "media_player.televizor"},
                options={},
            )
            try:
                with patch.object(self.setup, "native_remote_id", return_value="remote.televizor"):
                    with self.assertLogs(self.setup.__name__, level="WARNING") as logs:
                        with patch.object(self.old, "subscribe", side_effect=RuntimeError("PRIVATE-FAILURE-TEXT")):
                            await self.setup.async_start_keyboard(hass, entry)
                    self.assertNotIn("PRIVATE-FAILURE-TEXT", "\n".join(logs.output))
                    self.assertNotIn("keyboard_owner", hass.data[self.setup.DOMAIN])
                    self.assertFalse(getattr(runtime, "keyboard_closing", False))
                    await self.setup.async_start_keyboard(hass, entry)
                    provider = runtime.keyboard_coordinator.targets["tv"].provider
                    self.assertEqual((await provider.async_snapshot())["text"], "OLD-FIELD")
                    await self.setup.async_stop_keyboard(hass, entry)
                    await self.setup.async_start_keyboard(hass, entry)
                    self.assertIsNone(runtime.keyboard_coordinator)
                    self.assertNotIn("keyboard_owner", hass.data[self.setup.DOMAIN])
            finally:
                await self.setup.async_stop_keyboard(hass, entry)
                await hass.async_block_till_done()
