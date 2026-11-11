"""Samsung power observations with mocked HTTP and real HA coordinator lifecycle."""

import asyncio
import errno
import importlib
import socket
import tempfile
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame


_DEFAULT_PAYLOAD = object()


class SamsungPowerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tempdir.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.addCleanup(self.tempdir.cleanup)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        self.entry = ConfigEntry(
            domain="ekf_appliances", title="EKF Appliances", data={}, options={},
            source="user", unique_id="power-probe", version=1, minor_version=1,
            discovery_keys=MappingProxyType({}), subentries_data=None,
        )
        try:
            module = importlib.import_module("custom_components.ekf_appliances.samsung_power")
        except ModuleNotFoundError as err:
            if err.name != "custom_components.ekf_appliances.samsung_power":
                raise
            self.fail("The passive Samsung power coordinator has not been implemented.")
        self.session = MagicMock(spec=aiohttp.ClientSession)
        self.get_session = self.enterContext(patch.object(
            module, "async_get_clientsession", return_value=self.session,
        ))
        self.coordinator = module.SamsungPowerCoordinator(
            self.hass, self.entry, "192.0.2.10", "UE48J5500",
        )
        self.addAsyncCleanup(self.coordinator.async_shutdown)
        self.connection_key = SimpleNamespace(host="192.0.2.10", port=8001, ssl=False)
        self.respond()

    def respond(self, *, status=200, payload=_DEFAULT_PAYLOAD, error=None, json_error=None):
        response = MagicMock(spec=aiohttp.ClientResponse)
        response.status = status
        response.json = AsyncMock(
            return_value={"device": {"modelName": "UE48J5500"}} if payload is _DEFAULT_PAYLOAD else payload,
            side_effect=json_error,
        )
        request = MagicMock()
        request.__aenter__ = AsyncMock(return_value=response, side_effect=error)
        request.__aexit__ = AsyncMock(return_value=False)
        self.session.get.return_value = request
        self.response = response
        self.request = request

    async def test_construction_is_passive_and_uses_five_second_interval(self):
        self.get_session.assert_not_called()
        self.session.get.assert_not_called()
        self.assertEqual(self.coordinator.update_interval, timedelta(seconds=5))
        self.assertIsNone(self.coordinator.data)

    async def test_matching_identity_is_on_and_only_passive_get_is_used(self):
        await self.coordinator.async_refresh()
        self.assertIs(self.coordinator.data, True)
        self.get_session.assert_called_once_with(self.hass)
        self.assertEqual([call[0] for call in self.session.method_calls], ["get"])
        args, kwargs = self.session.get.call_args
        self.assertEqual(args, ("http://192.0.2.10:8001/api/v2/",))
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["timeout"].total, 2)
        self.request.__aexit__.assert_awaited_once()

    async def test_valid_identity_json_allows_nonstandard_content_type(self):
        self.response.headers = {"Content-Type": "text/plain"}
        await self.coordinator.async_refresh()
        self.assertIs(self.coordinator.data, True)
        self.response.json.assert_awaited_once_with(content_type=None)

    async def test_unexpected_status_clears_previous_on_to_unknown(self):
        for status in (301, 302, 401, 404, 503):
            with self.subTest(status=status):
                self.respond()
                await self.coordinator.async_refresh()
                self.assertIs(self.coordinator.data, True)
                self.respond(status=status)
                await self.coordinator.async_refresh()
                self.assertIsNone(self.coordinator.data)
                self.response.json.assert_not_awaited()

    async def test_invalid_schema_or_different_model_is_unknown(self):
        payloads = (
            None, {}, [], "not an object", {"device": None}, {"device": []},
            {"device": {}}, {"device": {"modelName": "UE55OTHER"}},
            {"device": {"modelName": "ue48j5500"}},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.respond()
                await self.coordinator.async_refresh()
                self.respond(payload=payload)
                await self.coordinator.async_refresh()
                self.assertIsNone(self.coordinator.data)

    async def test_request_and_body_timeouts_are_off(self):
        for options in ({"error": TimeoutError()}, {"json_error": TimeoutError()}):
            with self.subTest(options=options):
                self.respond(**options)
                await self.coordinator.async_refresh()
                self.assertIs(self.coordinator.data, False)
                self.assertTrue(self.coordinator.last_update_success)

    async def test_connection_refused_is_off(self):
        refused = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
        for error in (refused, aiohttp.ClientConnectorError(self.connection_key, refused)):
            with self.subTest(error=type(error).__name__):
                self.respond(error=error)
                await self.coordinator.async_refresh()
                self.assertIs(self.coordinator.data, False)

    async def test_dns_unreachable_disconnect_and_other_errors_are_unknown(self):
        errors = (
            aiohttp.ClientConnectorDNSError(
                self.connection_key, socket.gaierror(socket.EAI_NONAME, "No address"),
            ),
            aiohttp.ClientConnectorError(
                self.connection_key, OSError(errno.EHOSTUNREACH, "No route"),
            ),
            aiohttp.ServerDisconnectedError("Disconnected"),
            RuntimeError("Session is closed"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.respond()
                await self.coordinator.async_refresh()
                self.respond(error=error)
                await self.coordinator.async_refresh()
                self.assertIsNone(self.coordinator.data)

    async def test_invalid_json_is_unknown(self):
        self.respond()
        await self.coordinator.async_refresh()
        self.respond(json_error=ValueError("Invalid JSON"))
        await self.coordinator.async_refresh()
        self.assertIsNone(self.coordinator.data)
        self.request.__aexit__.assert_awaited_once()

    async def test_cancellation_is_propagated(self):
        self.respond(error=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.coordinator._async_update_data()

    async def test_listener_removal_and_shutdown_cancel_polling_without_closing_shared_session(self):
        await self.coordinator.async_refresh()
        scheduled = []
        original_call_at = self.hass.loop.call_at

        def schedule(when, callback, *args, **kwargs):
            handle = original_call_at(when, callback, *args, **kwargs)
            scheduled.append(handle)
            return handle

        with patch.object(self.hass.loop, "call_at", side_effect=schedule):
            remove_listener = self.coordinator.async_add_listener(lambda: None)
            self.assertEqual(len(scheduled), 1)
            self.assertFalse(scheduled[0].cancelled())
            remove_listener()
            self.assertTrue(scheduled[0].cancelled())
            remove_listener = self.coordinator.async_add_listener(lambda: None)
            self.assertEqual(len(scheduled), 2)
            await self.coordinator.async_shutdown()
            self.assertTrue(scheduled[1].cancelled())
            remove_listener()
        self.session.get.assert_called_once()
        self.session.close.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
