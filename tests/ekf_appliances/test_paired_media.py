"""Power-pair HA bindings and authenticated host-process status."""
import asyncio
from datetime import timedelta
import importlib.util
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame
from homeassistant.util import dt as dt_util

from custom_components.ekf_appliances.paired_media import install_power_pairs, observed_boolean

ROOT = Path(__file__).resolve().parents[2]


class PairBindingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.entry = SimpleNamespace(
            data={"tv_androidtv_entity_id": "media_player.android"},
            async_on_unload=Mock(),
        )
        self.calls = []
        self.starts = []

        async def projector_power(target):
            self.calls.append(("projector", target))
            self.hass.states.async_set("binary_sensor.hdmi", "on" if target else "off")

        self.runtime = SimpleNamespace(
            hass=self.hass, entry=self.entry,
            projector_power_entity_id="binary_sensor.hdmi",
            async_projector_power=projector_power,
            power_pairs=[], projector_power_pair=None, tv_power_pair=None,
            note_projector_off_requested=Mock(), projector_shutting_down=False,
        )
        self.projector = SimpleNamespace(
            hass=self.hass, entity_id="media_player.projector", async_write_ha_state=Mock(),
        )
        self.tv_listeners = []

        async def tv_power(target):
            self.calls.append(("tv", target))
            self.television.coordinator.data = target
            for callback in self.tv_listeners:
                callback()

        self.television = SimpleNamespace(
            hass=self.hass, entity_id="media_player.tv", async_write_ha_state=Mock(),
            _async_power=tv_power,
            coordinator=SimpleNamespace(
                data=False,
                async_add_listener=lambda callback: self.tv_listeners.append(callback) or (lambda: None),
            ),
        )
        for entity in ("binary_sensor.hdmi", "sensor.kodi_service_state", "media_player.android"):
            self.hass.states.async_set(entity, "off")

        async def service(call):
            self.calls.append((call.domain, call.service, dict(call.data)))
            if call.domain == "rest_command":
                self.hass.states.async_set(
                    "sensor.kodi_service_state", "on" if call.service == "host_kodi_start" else "off"
                )
            else:
                self.hass.states.async_set("media_player.android", "on" if call.service == "turn_on" else "off")
        for name in ("host_kodi_start", "host_kodi_stop"):
            self.hass.services.async_register("rest_command", name, service)
        for name in ("turn_on", "turn_off"):
            self.hass.services.async_register("media_player", name, service)

        def at_started(hass, start):
            self.starts.append(start)
            return lambda: None
        with patch("custom_components.ekf_appliances.paired_media.async_at_started", at_started):
            install_power_pairs(self.runtime, self.projector, self.television)
        self.assertEqual(self.calls, [])
        for start in self.starts:
            await start(self.hass)

    async def asyncTearDown(self):
        for pair in self.runtime.power_pairs:
            await pair.close()
        await self.hass.async_block_till_done()
        self.temp.cleanup()

    async def test_real_ha_services_start_and_stop_both_pairs(self):
        await self.runtime.projector_power_pair.request(True)
        self.assertIn(("rest_command", "host_kodi_start", {}), self.calls)
        self.assertIn(("projector", True), self.calls)
        await self.runtime.projector_power_pair.request(False)
        self.assertIn(("rest_command", "host_kodi_stop", {}), self.calls)
        await self.runtime.tv_power_pair.request(True)
        self.assertIn(("media_player", "turn_on", {"entity_id": "media_player.android"}), self.calls)
        self.assertIn(("tv", True), self.calls)

    async def test_kodi_json_rpc_unavailable_does_not_switch_projector_off(self):
        await self.runtime.projector_power_pair.request(True)
        self.calls.clear()
        self.hass.states.async_set("media_player.home", "unavailable")
        await self.hass.async_block_till_done()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.hass.states.get("binary_sensor.hdmi").state, "on")

    async def test_cast_session_off_does_not_switch_tv_off(self):
        await self.runtime.tv_power_pair.request(True)
        self.calls.clear()
        self.hass.states.async_set("media_player.gostinaia_2", "off")
        await self.hass.async_block_till_done()
        self.assertEqual(self.calls, [])
        self.assertTrue(self.television.coordinator.data)

    async def test_process_status_off_follows_to_projector(self):
        await self.runtime.projector_power_pair.request(True)
        self.calls.clear()
        self.hass.states.async_set("sensor.kodi_service_state", "off")
        await self.hass.async_block_till_done()
        self.assertIn(("projector", False), self.calls)

    async def test_observation_rejects_restored_unknown_and_stale_data(self):
        self.hass.states.async_set("sensor.sample", "on", {"restored": True})
        self.assertIsNone(observed_boolean(self.hass, "sensor.sample"))
        self.hass.states.async_set("sensor.sample", "unknown")
        self.assertIsNone(observed_boolean(self.hass, "sensor.sample"))
        self.hass.states.async_set("sensor.sample", "on")
        self.hass.states.get("sensor.sample").last_reported = dt_util.utcnow() - timedelta(seconds=60)
        self.assertIsNone(observed_boolean(self.hass, "sensor.sample", max_age=20))
        self.assertTrue(observed_boolean(self.hass, "media_player.android") is False)


class KodiHostStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("host_status_test", ROOT / "scripts/host/ha-host-actions.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def status(self, text, returncode=0):
        def boundary(args, timeout):
            if args[:2] == ["systemctl", "show"]:
                return subprocess.CompletedProcess(args, returncode, text, "")
            if args[0] == "pgrep":
                return subprocess.CompletedProcess(args, 1, "", "")
            raise AssertionError("Status must not send JSON-RPC or control commands")
        with patch.object(self.module, "run", side_effect=boundary) as run:
            value = self.module.kodi_status()
            self.assertEqual(run.call_args_list[0].args[0][:3],
                             ["systemctl", "show", self.module.KODI_UNIT])
            return value

    def test_running_process_is_on_without_a_json_rpc_probe(self):
        value = self.status("LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=123\n")
        self.assertEqual(value["state"], "on")
        self.assertTrue(value["process_present"])

    def test_inactive_without_process_is_off(self):
        self.assertEqual(self.status("LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0")["state"], "off")

    def test_starting_or_inconsistent_supervisor_is_unknown(self):
        for active, sub, pid in (("activating", "start", "123"), ("deactivating", "stop", "123"), ("active", "exited", "0"), ("inactive", "dead", "123")):
            with self.subTest(active=active, pid=pid):
                self.assertEqual(self.status(f"LoadState=loaded\nActiveState={active}\nSubState={sub}\nMainPID={pid}")["state"], "unknown")

    def test_missing_unit_and_read_error_are_failures_not_off(self):
        for text, code in (("LoadState=not-found", 0), ("", 1)):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                self.status(text, code)

    def test_endpoint_requires_authentication_before_reading_process_status(self):
        handler = object.__new__(self.module.Handler)
        handler.path = "/kodi/status"
        handler.command = "GET"
        handler.authorized = Mock(return_value=False)
        handler.send_json = Mock()
        with patch.dict(self.module.ROUTES, {("GET", "/kodi/status"): Mock()}) as routes:
            handler.handle_route()
            routes[("GET", "/kodi/status")].assert_not_called()
        handler.send_json.assert_called_once_with(403, {"ok": False, "error": "forbidden"})


if __name__ == "__main__":
    unittest.main()
