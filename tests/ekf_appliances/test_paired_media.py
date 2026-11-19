"""Power-pair HA bindings and authenticated host-process status."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame
from homeassistant.util import dt as dt_util

from custom_components.ekf_appliances.media_player import ProjectorMediaPlayer
from custom_components.ekf_appliances.media_routing import TelevisionMediaRouting
from custom_components.ekf_appliances.paired_media import (
    install_power_pairs,
    observed_boolean,
)

ROOT = Path(__file__).resolve().parents[2]


class PairBindingsTests(unittest.IsolatedAsyncioTestCase):
    """Exercise pair bindings tests contracts."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        er.async_get(self.hass).async_get_or_create(
            "media_player",
            "androidtv_remote",
            "android-fixture",
            suggested_object_id="android",
        )
        self.unload = []
        self.entry = SimpleNamespace(
            data={
                "tv_androidtv_entity_id": "media_player.android",
                "tv_kodi_entity_id": "media_player.home",
            },
            async_on_unload=self.unload.append,
        )
        self.calls = []
        self.starts = []

        async def projector_power(*, turn_on) -> None:
            self.calls.append(("projector", turn_on))
            self.hass.states.async_set("binary_sensor.hdmi", "on" if turn_on else "off")

        self.runtime = SimpleNamespace(
            hass=self.hass,
            entry=self.entry,
            projector_power_entity_id="binary_sensor.hdmi",
            async_projector_power=projector_power,
            power_pairs=[],
            projector_power_pair=None,
            tv_power_pair=None,
            note_projector_off_requested=Mock(),
            projector_shutting_down=False,
        )
        self.projector = SimpleNamespace(
            hass=self.hass,
            entity_id="media_player.projector",
            async_write_ha_state=Mock(),
        )
        self.tv_listeners = []

        async def tv_power(target) -> None:
            self.calls.append(("tv", target))
            self.television.coordinator.data = target
            for callback in self.tv_listeners:
                callback()

        self.television = SimpleNamespace(
            hass=self.hass,
            entity_id="media_player.tv",
            async_write_ha_state=Mock(),
            _async_power=tv_power,
            media_routing=TelevisionMediaRouting(self.hass, self.entry),
            coordinator=SimpleNamespace(
                data=False,
                async_add_listener=lambda callback: (
                    self.tv_listeners.append(callback) or (lambda: None)
                ),
            ),
        )
        for entity in (
            "binary_sensor.hdmi",
            "sensor.kodi_service_state",
            "media_player.android",
        ):
            self.hass.states.async_set(entity, "off")

        async def service(call) -> None:
            self.calls.append((call.domain, call.service, dict(call.data)))
            if call.domain == "rest_command":
                self.hass.states.async_set(
                    "sensor.kodi_service_state",
                    "on" if call.service == "host_kodi_start" else "off",
                )
            else:
                self.hass.states.async_set(
                    call.data["entity_id"], "on" if call.service == "turn_on" else "off"
                )

        for name in ("host_kodi_start", "host_kodi_stop"):
            self.hass.services.async_register("rest_command", name, service)
        for name in ("turn_on", "turn_off"):
            self.hass.services.async_register("media_player", name, service)

        def at_started(_hass, start) -> Any:
            self.starts.append(start)
            return lambda: None

        with patch(
            "custom_components.ekf_appliances.paired_media.async_at_started", at_started
        ):
            install_power_pairs(self.runtime, self.projector, self.television)
        self.assertEqual(self.calls, [])
        for start in self.starts:
            await start(self.hass)

    async def asyncTearDown(self) -> None:
        for pair in self.runtime.power_pairs:
            await pair.close()
        for unsubscribe in reversed(self.unload):
            unsubscribe()
        await self.hass.async_block_till_done()
        self.temp.cleanup()

    async def test_real_ha_services_start_and_stop_both_pairs(self) -> None:
        self.assertTrue(self.runtime.tv_power_pair.endpoints["player"].wait_for_feedback)
        self.assertFalse(self.runtime.tv_power_pair.endpoints["display"].wait_for_feedback)
        for endpoint in self.runtime.projector_power_pair.endpoints.values():
            self.assertFalse(endpoint.wait_for_feedback)
        await self.runtime.projector_power_pair.request(target=True)
        self.assertIn(("rest_command", "host_kodi_start", {}), self.calls)
        self.assertIn(("projector", True), self.calls)
        await self.runtime.projector_power_pair.request(target=False)
        self.assertIn(("rest_command", "host_kodi_stop", {}), self.calls)
        await self.runtime.tv_power_pair.request(target=True)
        self.assertIn(
            ("media_player", "turn_on", {"entity_id": "media_player.android"}),
            self.calls,
        )
        self.assertIn(("tv", True), self.calls)

    async def test_kodi_json_rpc_unavailable_does_not_switch_projector_off(
        self,
    ) -> None:
        await self.runtime.projector_power_pair.request(target=True)
        self.calls.clear()
        self.hass.states.async_set("media_player.home", "unavailable")
        await self.hass.async_block_till_done()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.hass.states.get("binary_sensor.hdmi").state, "on")

    async def test_cast_session_off_does_not_switch_tv_off(self) -> None:
        await self.runtime.tv_power_pair.request(target=True)
        self.calls.clear()
        self.hass.states.async_set("media_player.gostinaia_2", "off")
        await self.hass.async_block_till_done()
        self.assertEqual(self.calls, [])
        self.assertTrue(self.television.coordinator.data)

    async def test_process_status_off_follows_to_projector(self) -> None:
        await self.runtime.projector_power_pair.request(target=True)
        self.calls.clear()
        self.hass.states.async_set("sensor.kodi_service_state", "off")
        await self.hass.async_block_till_done()
        self.assertIn(("projector", False), self.calls)

    async def test_android_rename_rebinds_commands_and_external_power_observation(
        self,
    ) -> None:
        old_id = "media_player.android"
        new_id = "media_player.android_renamed"
        er.async_get(self.hass).async_update_entity(old_id, new_entity_id=new_id)
        self.hass.states.async_remove(old_id)
        self.hass.states.async_set(new_id, "off")
        await self.hass.async_block_till_done()
        self.runtime.tv_power_pair.timeout = 0.2
        await self.runtime.tv_power_pair.request(target=True)
        self.assertIn(("media_player", "turn_on", {"entity_id": new_id}), self.calls)
        self.assertTrue(self.television.coordinator.data)
        self.calls.clear()
        self.hass.states.async_set(new_id, "off")
        await self.hass.async_block_till_done()
        self.assertIn(("tv", False), self.calls)

    async def test_projector_without_optional_kodi_does_not_start_unselected_player(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as config_dir:
            hass = HomeAssistant(config_dir)
            calls, starts, unload = [], [], []
            hass.states.async_set("binary_sensor.hdmi", "off")
            hass.states.async_set("sensor.kodi_service_state", "off")

            async def projector_power(*, turn_on) -> None:
                calls.append(("projector", turn_on))
                hass.states.async_set("binary_sensor.hdmi", "on" if turn_on else "off")

            async def kodi_power(call) -> None:
                calls.append(("kodi", call.service))
                hass.states.async_set("sensor.kodi_service_state", "on")

            hass.services.async_register("rest_command", "host_kodi_start", kodi_power)
            entry = SimpleNamespace(
                entry_id="no-kodi", data={}, async_on_unload=unload.append
            )
            runtime = SimpleNamespace(
                hass=hass,
                entry=entry,
                device_info=lambda _kind: {},
                projector_power_entity_id="binary_sensor.hdmi",
                projector_power_state=False,
                async_projector_power=projector_power,
                projector_shutting_down=False,
                power_pairs=[],
                projector_power_pair=None,
                tv_power_pair=None,
                note_projector_off_requested=Mock(),
            )
            projector = ProjectorMediaPlayer(runtime)
            with patch(
                "custom_components.ekf_appliances.paired_media.async_at_started",
                lambda _hass, start: starts.append(start) or (lambda: None),
            ):
                install_power_pairs(runtime, projector)
            try:
                for start in starts:
                    await start(hass)
                await projector.async_turn_on()
                self.assertEqual(calls, [("projector", True)])
                self.assertEqual(hass.states.get("binary_sensor.hdmi").state, "on")
            finally:
                for pair in runtime.power_pairs:
                    await pair.close()
                for unsubscribe in reversed(unload):
                    unsubscribe()
                await hass.async_block_till_done()

    async def test_observation_rejects_restored_unknown_and_stale_data(self) -> None:
        self.assertIsNone(observed_boolean(self.hass, None))
        self.hass.states.async_set("sensor.sample", "on", {"restored": True})
        self.assertIsNone(observed_boolean(self.hass, "sensor.sample"))
        self.hass.states.async_set("sensor.sample", "unknown")
        self.assertIsNone(observed_boolean(self.hass, "sensor.sample"))
        self.hass.states.async_set("sensor.sample", "on")
        self.hass.states.get("sensor.sample").last_reported = (
            dt_util.utcnow() - timedelta(seconds=60)
        )
        self.assertIsNone(observed_boolean(self.hass, "sensor.sample", max_age=20))
        self.assertTrue(observed_boolean(self.hass, "media_player.android") is False)


class KodiHostStatusTests(unittest.TestCase):
    """Exercise kodi host status tests contracts."""

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location(
            "host_status_test", ROOT / "scripts/host/ha-host-actions.py"
        )
        cls.module = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(ROOT / "scripts/host"), *sys.path]):
            spec.loader.exec_module(cls.module)

    def status(self, text, returncode=0) -> Any:
        def boundary(args, timeout) -> Any:  # noqa: ARG001 - Mock preserves the run helper timeout keyword contract.
            if args[:2] == ["systemctl", "show"]:
                return subprocess.CompletedProcess(args, returncode, text, "")
            if args[0] == "pgrep":
                return subprocess.CompletedProcess(args, 1, "", "")
            error_message = "Status must not send JSON-RPC or control commands"
            raise AssertionError(error_message)

        with patch.object(self.module, "run", side_effect=boundary) as run:
            value = self.module.kodi_status()
            self.assertEqual(
                run.call_args_list[0].args[0][:3],
                ["systemctl", "show", self.module.KODI_UNIT],
            )
            return value

    def test_running_process_is_on_without_a_json_rpc_probe(self) -> None:
        value = self.status(
            "LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=123\n"
        )
        self.assertEqual(value["state"], "on")
        self.assertTrue(value["process_present"])

    def test_inactive_without_process_is_off(self) -> None:
        self.assertEqual(
            self.status(
                "LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0"
            )["state"],
            "off",
        )

    def test_starting_or_inconsistent_supervisor_is_unknown(self) -> None:
        for active, sub, pid in (
            ("activating", "start", "123"),
            ("deactivating", "stop", "123"),
            ("active", "exited", "0"),
            ("inactive", "dead", "123"),
        ):
            with self.subTest(active=active, pid=pid):
                self.assertEqual(
                    self.status(
                        f"LoadState=loaded\nActiveState={active}\nSubState={sub}\nMainPID={pid}"
                    )["state"],
                    "unknown",
                )

    def test_missing_unit_and_read_error_are_failures_not_off(self) -> None:
        for text, code in (("LoadState=not-found", 0), ("", 1)):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                self.status(text, code)

    def test_endpoint_requires_authentication_before_reading_process_status(
        self,
    ) -> None:
        handler = object.__new__(self.module.Handler)
        handler.path = "/kodi/status"
        handler.command = "GET"
        handler.authorized = Mock(return_value=False)
        handler.send_json = Mock()
        with patch.dict(
            self.module.ROUTES, {("GET", "/kodi/status"): Mock()}
        ) as routes:
            handler.handle_route()
            routes[("GET", "/kodi/status")].assert_not_called()
        handler.send_json.assert_called_once_with(
            403, {"ok": False, "error": "forbidden"}
        )


if __name__ == "__main__":
    unittest.main()
