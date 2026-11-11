"""Transport migration validates identity and preserves concurrent configuration."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from homeassistant.exceptions import HomeAssistantError
from custom_components.ekf_appliances.samsung_network import (
    async_prepare_websocket, async_apply_websocket,
)


class SamsungNetworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entry = SimpleNamespace(
            entry_id="native-samsung", domain="samsungtv", unique_id="same-tv",
            data={
                "host": "192.0.2.74", "model": "UE48J5500",
                "mac": "24:4b:03:ce:05:eb", "method": "encrypted", "port": 8000,
                "token": "old-test-token", "session_id": "old-test-session",
                "ssdp_rendering_control_location": "http://192.0.2.74:9197/dmr",
            },
        )
        self.hass = SimpleNamespace(config_entries=SimpleNamespace(
            async_get_entry=Mock(return_value=self.entry),
            async_update_entry=Mock(),
        ))
        self.info = {
            "id": "uuid:same-tv",
            "device": {"modelName": "UE48J5500", "wifiMac": "24:4B:03:CE:05:EB"},
        }

    async def test_preparation_does_not_mutate_then_apply_changes_only_transport(self):
        before = copy.deepcopy(self.entry.data)
        with patch(
            "custom_components.ekf_appliances.samsung_network._async_probe",
            AsyncMock(return_value=(self.info, "new-ws-test-token")),
        ) as probe:
            prepared = await async_prepare_websocket(self.hass, self.entry)
        probe.assert_awaited_once_with(self.hass, "192.0.2.74")
        self.assertEqual(self.entry.data, before)
        self.hass.config_entries.async_update_entry.assert_not_called()
        async_apply_websocket(self.hass, prepared)
        target, = self.hass.config_entries.async_update_entry.call_args.args
        data = self.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        self.assertIs(target, self.entry)
        self.assertEqual(data["method"], "websocket")
        self.assertEqual(data["port"], 8001)
        self.assertEqual(data["token"], "new-ws-test-token")
        self.assertNotIn("session_id", data)
        for key in ("host", "model", "mac", "ssdp_rendering_control_location"):
            self.assertEqual(data[key], before[key])

    async def test_wrong_device_identity_is_rejected_without_mutation(self):
        for key, value in (("id", "uuid:other-tv"), ("mac", "11:22:33:44:55:66")):
            with self.subTest(key=key):
                info = copy.deepcopy(self.info)
                if key == "id":
                    info["id"] = value
                else:
                    info["device"]["wifiMac"] = value
                with patch(
                    "custom_components.ekf_appliances.samsung_network._async_probe",
                    AsyncMock(return_value=(info, None)),
                ):
                    with self.assertRaises(HomeAssistantError):
                        await async_prepare_websocket(self.hass, self.entry)
        self.hass.config_entries.async_update_entry.assert_not_called()

    async def test_network_failure_preserves_existing_pairing(self):
        before = copy.deepcopy(self.entry.data)
        with patch(
            "custom_components.ekf_appliances.samsung_network._async_probe",
            AsyncMock(side_effect=TimeoutError),
        ):
            with self.assertRaises(HomeAssistantError):
                await async_prepare_websocket(self.hass, self.entry)
        self.assertEqual(self.entry.data, before)
        self.hass.config_entries.async_update_entry.assert_not_called()

    async def test_concurrent_configuration_is_never_overwritten(self):
        with patch(
            "custom_components.ekf_appliances.samsung_network._async_probe",
            AsyncMock(return_value=(self.info, None)),
        ):
            prepared = await async_prepare_websocket(self.hass, self.entry)
        self.entry.data = {**self.entry.data, "host": "192.0.2.75"}
        with self.assertRaises(HomeAssistantError):
            async_apply_websocket(self.hass, prepared)
        self.hass.config_entries.async_update_entry.assert_not_called()

    async def test_already_configured_transport_does_not_probe_standby_tv(self):
        self.entry.data = {**self.entry.data, "method": "websocket", "port": 8001}
        with patch(
            "custom_components.ekf_appliances.samsung_network._async_probe",
            AsyncMock(),
        ) as probe:
            self.assertIsNone(await async_prepare_websocket(self.hass, self.entry))
        probe.assert_not_awaited()
