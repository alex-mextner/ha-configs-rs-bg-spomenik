"""Exercise the registered HA WebSocket boundary, permissions and private editor data."""
import asyncio
from copy import deepcopy
import importlib
import inspect
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from uuid import uuid4

from homeassistant import loader
from homeassistant.auth.permissions.const import POLICY_READ, POLICY_CONTROL
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

PRIVATE = "PRIVATE-KEYBOARD-FIXTURE"
TV = "remote.televizor"
KODI = "media_player.home"
DOMAIN = "ekf_appliances"
PREFIX = DOMAIN + "/keyboard/"


class Permissions:
    def __init__(self):
        self.allowed = {(TV, POLICY_READ), (TV, POLICY_CONTROL),
                        (KODI, POLICY_READ), (KODI, POLICY_CONTROL)}
        self.checked = []

    def check_entity(self, entity_id, policy):
        self.checked.append((entity_id, policy))
        return (entity_id, policy) in self.allowed


class Provider:
    def __init__(self, text=PRIVATE, selection=True):
        self.state = {
            "session_id": str(uuid4()), "revision": 7, "focused": True,
            "sensitive": False, "text": text, "selection": {"start": 0, "end": 0},
            "capabilities": {"edit": True, "commit": True, "close": True, "selection": selection},
            "status": "ready", "fresh_at": "2026-11-13T18:00:00Z",
        }
        self.listeners = set()
        self.calls = []

    async def async_snapshot(self):
        self.calls.append(("snapshot",))
        return deepcopy(self.state)

    def subscribe(self, callback):
        self.listeners.add(callback)
        return lambda: self.listeners.discard(callback)

    def emit(self, **changes):
        self.state.update(changes)
        for callback in list(self.listeners):
            callback(deepcopy(self.state))

    async def async_edit(self, session_id, expected_revision, text, selection=None):
        self.calls.append(("edit", session_id, expected_revision, text, selection))
        assert type(expected_revision) is int, "Coordinator must restore the provider's native revision type"
        assert session_id == self.state["session_id"] and expected_revision == self.state["revision"]
        self.emit(text=text, selection=selection, revision=expected_revision+1)
        return deepcopy(self.state)

    async def async_commit(self, session_id, expected_revision):
        return await self._finish("commit", session_id, expected_revision)

    async def async_close(self, session_id, expected_revision):
        return await self._finish("close", session_id, expected_revision)

    async def _finish(self, action, session_id, expected_revision):
        self.calls.append((action, session_id, expected_revision))
        assert type(expected_revision) is int
        assert session_id == self.state["session_id"] and expected_revision == self.state["revision"]
        self.emit(focused=False, text=None, revision=expected_revision+1, capabilities={})
        return deepcopy(self.state)


class RecordLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class KeyboardWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = Path(__file__).resolve().parents[2] / "custom_components/ekf_appliances/keyboard.py"
        self.assertTrue(path.exists(), "Authenticated keyboard WebSocket coordinator is not implemented")
        self.module = importlib.import_module("custom_components.ekf_appliances.keyboard")
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.hass.states.async_set("input_select.universal_remote_pair", "tv")
        self.tv, self.kodi = Provider(), Provider("KODI-FIXTURE", selection=False)
        targets = {
            "tv": self.module.KeyboardTarget(entity_id=TV, provider=self.tv),
            "projector": self.module.KeyboardTarget(entity_id=KODI, provider=self.kodi),
        }
        self.coordinator = self.module.KeyboardCoordinator(self.hass, targets=targets)
        self.hass.data[DOMAIN] = {"keyboard_coordinator": self.coordinator}
        result = self.module.async_register_commands(self.hass)
        if inspect.isawaitable(result):
            await result
        self.permissions = Permissions()
        self.messages = []
        user = SimpleNamespace(id="fixture-user", name="Fixture user", is_active=True,
                               is_owner=False, is_admin=False, system_generated=False,
                               permissions=self.permissions)
        def send(message):
            if callable(message):
                message = message()
            if isinstance(message, (bytes, str)):
                message = json.loads(message)
            self.messages.append(deepcopy(message))
        self.connection = ActiveConnection(logging.LoggerAdapter(logging.getLogger("keyboard.ws.fixture"), {}),
                                           self.hass, send, user, None, "fixture")
        self.next_id = 1

    async def asyncTearDown(self):
        if not hasattr(self, "hass"):
            return
        self.connection.async_handle_close()
        close = getattr(self.coordinator, "async_close", None)
        if close:
            result = close()
            if inspect.isawaitable(result):
                await result
        await self.hass.async_stop()
        self.temp.cleanup()

    async def drain(self):
        for _ in range(10):
            await asyncio.sleep(0)

    async def call(self, action, **data):
        message_id = self.next_id
        self.next_id += 1
        self.connection.async_handle({"id": message_id, "type": PREFIX+action, **data})
        for _ in range(30):
            await asyncio.sleep(0)
            result = next((m for m in self.messages if m.get("id") == message_id and m.get("type") == "result"), None)
            if result is not None:
                await self.drain()
                return result
        self.fail("Registered WebSocket command did not return a result")

    async def subscribe(self, pair="tv"):
        result = await self.call("subscribe", pair=pair)
        self.assertTrue(result.get("success"), result.get("error"))
        events = [m["event"] for m in self.messages if m.get("type") == "event" and m.get("id") == result["id"]]
        self.assertTrue(events, "subscribeMessage needs an initial event, not only result payload")
        return events[-1], result["id"]

    def writes(self, provider=None):
        return [c for c in (provider or self.tv).calls if c[0] in ("edit", "commit", "close")]

    async def test_registered_subscription_returns_private_snapshot_only_to_authorized_user(self):
        snap, _ = await self.subscribe()
        self.assertEqual(snap["pair"], "tv")
        self.assertEqual(snap["text"], PRIVATE)
        self.assertIsInstance(snap["revision"], str)
        self.assertTrue(snap["revision"])
        self.assertEqual(snap["session_id"], self.tv.state["session_id"])
        self.assertIn((TV, POLICY_READ), self.permissions.checked)
        self.assertIn((TV, POLICY_CONTROL), self.permissions.checked)
        self.assertEqual(self.writes(), [])

    async def test_missing_read_or_control_denies_before_provider_io(self):
        for denied in (POLICY_READ, POLICY_CONTROL):
            with self.subTest(policy=denied):
                self.permissions.allowed.discard((TV, denied))
                self.tv.calls.clear()
                result = await self.call("subscribe", pair="tv")
                self.assertFalse(result["success"])
                self.assertEqual(self.tv.calls, [])
                self.permissions.allowed.add((TV, denied))

    async def test_revoked_permission_stops_publication_and_mutation(self):
        snap, subscription = await self.subscribe()
        self.messages.clear()
        self.permissions.allowed.discard((TV, POLICY_CONTROL))
        self.tv.emit(revision=8, text="REVOKED-PRIVATE")
        await self.drain()
        self.assertNotIn("REVOKED-PRIVATE", json.dumps(self.messages))
        result = await self.call("edit", session_id=snap["session_id"],
                                 expected_revision=snap["revision"], text="ATTEMPT")
        self.assertFalse(result["success"])
        self.assertEqual(self.writes(), [])
        self.assertTrue(any(policy == POLICY_CONTROL for _, policy in self.permissions.checked))

    async def test_unknown_session_and_stale_revision_never_reach_provider_write(self):
        snap, _ = await self.subscribe()
        for session, revision in [(str(uuid4()), snap["revision"]),
                                  (snap["session_id"], snap["revision"]+"-stale")]:
            result = await self.call("edit", session_id=session, expected_revision=revision, text="ATTEMPT")
            self.assertFalse(result["success"])
        self.assertEqual(self.writes(), [])

    async def test_sensitive_and_unknown_snapshot_cannot_leak_even_if_provider_advertises_edit(self):
        for sensitive, focused in [(True, True), (False, False), (None, True)]:
            with self.subTest(sensitive=sensitive, focused=focused):
                self.tv.state.update(session_id=str(uuid4()), sensitive=sensitive, focused=focused, text="DO-NOT-PUBLISH")
                before = len(self.messages)
                snap, _ = await self.subscribe()
                self.assertNotIn("text", snap)
                self.assertFalse(snap["capabilities"]["edit"])
                self.assertNotIn("DO-NOT-PUBLISH", json.dumps(self.messages[before:]))
                result = await self.call("edit", session_id=snap["session_id"],
                                         expected_revision=snap["revision"], text="ATTEMPT")
                self.assertFalse(result["success"])
        self.assertEqual(self.writes(), [])

    async def test_edit_restores_native_revision_returns_confirmed_snapshot_and_uses_no_ha_state_or_bus_text(self):
        snap, _ = await self.subscribe()
        events = []
        remove = self.hass.bus.async_listen("*", lambda event: events.append(event.as_dict()))
        handler = RecordLog()
        logging.getLogger().addHandler(handler)
        try:
            text = PRIVATE + "😀"
            result = await self.call("edit", session_id=snap["session_id"], expected_revision=snap["revision"],
                                     text=text, selection={"start": len(PRIVATE)+2, "end": len(PRIVATE)+2})
            self.assertTrue(result["success"], result.get("error"))
            answer = result["result"]
            self.assertEqual(answer["text"], text)
            self.assertIsInstance(answer["revision"], str)
            self.assertNotEqual(answer["revision"], snap["revision"])
            writes = self.writes()
            self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0][2], 7)
            self.assertNotIn(PRIVATE, json.dumps(events))
            self.assertNotIn(PRIVATE, json.dumps([state.as_dict() for state in self.hass.states.async_all()]))
            self.assertNotIn(PRIVATE, "\n".join(handler.messages))
        finally:
            remove()
            logging.getLogger().removeHandler(handler)

    async def test_invalid_utf16_length_and_selection_fail_safely_without_logging_input(self):
        snap, _ = await self.subscribe()
        handler = RecordLog()
        logging.getLogger().addHandler(handler)
        try:
            invalid = [
                {"text": "😀"*8193},
                {"text": PRIVATE+"\ud800"},
                {"text": "a😀b", "selection": {"start": 2, "end": 2}},
                {"text": PRIVATE, "selection": {"start": -1, "end": 0}},
            ]
            for payload in invalid:
                result = await self.call("edit", session_id=snap["session_id"],
                                         expected_revision=snap["revision"], **payload)
                self.assertFalse(result["success"])
                self.assertNotIn(PRIVATE, json.dumps(result.get("error")))
            self.assertEqual(self.writes(), [])
            self.assertNotIn(PRIVATE, "\n".join(handler.messages))
        finally:
            logging.getLogger().removeHandler(handler)

    async def test_commit_and_close_are_guarded_and_return_observed_terminal_snapshot(self):
        for action in ("commit", "close"):
            self.tv.state.update(session_id=str(uuid4()), revision=7, focused=True, sensitive=False,
                                 text=PRIVATE, capabilities={"edit": True, "commit": True, "close": True, "selection": True})
            snap, _ = await self.subscribe()
            result = await self.call(action, session_id=snap["session_id"], expected_revision=snap["revision"])
            self.assertTrue(result["success"], result.get("error"))
            self.assertFalse(result["result"]["focused"])
            self.assertNotIn("text", result["result"])
            self.assertEqual(self.writes()[-1][0], action)
            self.assertEqual(self.writes()[-1][2], 7)

    async def test_closed_connection_does_not_receive_later_private_snapshots(self):
        await self.subscribe()
        self.connection.async_handle_close()
        before = len(self.messages)
        self.tv.emit(revision=8, text="AFTER-CLOSE-PRIVATE")
        await self.drain()
        self.assertEqual(len(self.messages), before)
        self.assertEqual(self.connection.subscriptions, {})

    async def test_auto_subscription_follows_authoritative_pair_without_power_commands(self):
        snap, subscription = await self.subscribe("auto")
        self.assertEqual(snap["pair"], "tv")
        self.hass.states.async_set("input_select.universal_remote_pair", "projector")
        await self.drain()
        self.kodi.emit(revision=8)
        await self.drain()
        events = [m["event"] for m in self.messages if m.get("type") == "event" and m.get("id") == subscription]
        self.assertEqual(events[-1]["pair"], "projector")
        self.assertEqual(events[-1]["text"], "KODI-FIXTURE")
        self.assertEqual(self.writes(self.tv)+self.writes(self.kodi), [])


    async def test_late_operation_ack_cannot_resurrect_a_replaced_native_field(self):
        first, subscription = await self.subscribe()
        initial = deepcopy(self.tv.state)
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_edit(session_id, revision, text, selection=None):
            self.tv.calls.append(("edit", session_id, revision, text, selection))
            entered.set()
            await release.wait()
            return {**initial, "revision": initial["revision"]+1, "text": "OLDER-ACK"}
        self.tv.async_edit = delayed_edit
        operation = asyncio.create_task(self.call("edit", session_id=first["session_id"],
                                                 expected_revision=first["revision"], text="OLDER-ACK"))
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            newer = str(uuid4())
            self.tv.emit(session_id=newer, revision=0, text="NEW-NATIVE-FIELD")
            await self.drain()
            events = [m["event"] for m in self.messages if m.get("type")=="event" and m.get("id")==subscription]
            self.assertEqual(events[-1]["session_id"], newer, "The newer field must be observed before the old response")
        finally:
            release.set()
        result = await operation
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["result"]["session_id"], newer)
        self.assertEqual(result["result"]["text"], "NEW-NATIVE-FIELD")
        events = [m["event"] for m in self.messages if m.get("type")=="event" and m.get("id")==subscription]
        self.assertEqual(events[-1]["session_id"], newer)
        self.assertEqual(len(self.writes()), 1)


    async def test_initial_auto_subscription_rechecks_pair_after_a_slow_native_snapshot(self):
        initial = deepcopy(self.tv.state)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_snapshot():
            self.tv.calls.append(("snapshot",))
            entered.set()
            await release.wait()
            return initial
        self.tv.async_snapshot = slow_snapshot
        subscribing = asyncio.create_task(self.call("subscribe", pair="auto"))
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            self.hass.states.async_set("input_select.universal_remote_pair", "projector")
            await self.drain()
        finally:
            release.set()
        result = await subscribing
        self.assertTrue(result["success"], result.get("error"))
        events = [m["event"] for m in self.messages if m.get("type")=="event" and m.get("id")==result["id"]]
        self.assertTrue(events)
        self.assertEqual(events[-1]["pair"], "projector")
        self.assertEqual(events[-1]["text"], "KODI-FIXTURE")
        self.assertEqual(self.writes(self.tv)+self.writes(self.kodi), [])

class AFrameworkLogPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_websocket_logger_redacts_keyboard_inbound_outbound_coalesced_and_overflow(self):
        from homeassistant.components.websocket_api.http import _WS_LOGGER
        with tempfile.TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            loader.async_setup(hass)
            frame.async_setup(hass)
            handler = RecordLog()
            previous_filters = list(_WS_LOGGER.filters)
            previous_level, previous_propagate = _WS_LOGGER.level, _WS_LOGGER.propagate
            _WS_LOGGER.addHandler(handler)
            _WS_LOGGER.setLevel(logging.DEBUG)
            _WS_LOGGER.propagate = False
            try:
                path = Path(__file__).resolve().parents[2] / "custom_components/ekf_appliances/keyboard.py"
                if path.exists():
                    module = importlib.import_module("custom_components.ekf_appliances.keyboard")
                    result = module.async_register_commands(hass)
                    if inspect.isawaitable(result):
                        await result
                incoming = {"id":1,"type":PREFIX+"edit","session_id":"fixture","expected_revision":"7","text":PRIVATE}
                malformed = {**incoming,"expected_revision":[]}
                snapshot = {"pair":"tv","session_id":"fixture","revision":"8","focused":True,
                            "sensitive":False,"text":PRIVATE,"capabilities":{"edit":True}}
                outgoing = {"id":1,"type":"event","event":snapshot}
                success = {"id":2,"type":"result","success":True,"result":snapshot}
                _WS_LOGGER.debug("%s: Received %s","fixture",incoming)
                _WS_LOGGER.debug("%s: Received %s","fixture",[incoming,malformed])
                _WS_LOGGER.debug("%s: Sending %s","fixture",json.dumps(outgoing).encode())
                _WS_LOGGER.debug("%s: Sending %s","fixture",json.dumps([success,outgoing]).encode())
                _WS_LOGGER.error("%s: Client unable to keep up. Last message was: %s","fixture",json.dumps(outgoing).encode())
                _WS_LOGGER.debug("%s: Received %s","fixture",{"id":3,"type":"get_states","diagnostic":"UNRELATED-DIAGNOSTIC"})
                output = "\n".join(handler.messages)
                self.assertNotIn(PRIVATE,output)
                self.assertIn("UNRELATED-DIAGNOSTIC",output,"The privacy filter must not disable unrelated HA diagnostics")
            finally:
                _WS_LOGGER.removeHandler(handler)
                _WS_LOGGER.filters[:] = previous_filters
                _WS_LOGGER.setLevel(previous_level)
                _WS_LOGGER.propagate = previous_propagate
                await hass.async_stop()
