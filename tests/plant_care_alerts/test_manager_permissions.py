# Copyright (c) 2026 Alex Ultra
# SPDX-License-Identifier: MIT
"""Check companion service authorization without calling live devices."""

from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized, UnknownUser

from custom_components.plant_care_alerts import DOMAIN, CareManager, async_setup

from .service_fixtures import make_service_registry

MODULE = "custom_components.plant_care_alerts"
PLANT = "sensor.test_plant_care"
SPEAKER = "media_player.test_speaker"
TEST_TIMEOUT = 5
CALL_DATA = {
    "announce": {"message": "Проверь грунт перед поливом."},
    "snooze": {"entity_id": PLANT},
    "acknowledge": {"entity_id": PLANT, "status": "due"},
}


class TestManagerPermissions(unittest.IsolatedAsyncioTestCase):
    """Exercise the actual registered callbacks with native HA contexts."""

    async def asyncSetUp(self) -> None:
        self.controlled_entities: set[str] = set()
        self.permissions = Mock()
        self.permissions.check_entity.side_effect = lambda entity_id, policy: (
            policy == POLICY_READ
            or (policy == POLICY_CONTROL and entity_id in self.controlled_entities)
        )
        self.user = SimpleNamespace(permissions=self.permissions)
        self.hass = SimpleNamespace(
            data={},
            states=SimpleNamespace(get=lambda _: None),
            auth=SimpleNamespace(async_get_user=AsyncMock(return_value=self.user)),
            services=make_service_registry(),
            bus=SimpleNamespace(
                async_fire=Mock(),
                async_listen=Mock(return_value=lambda: None),
                async_listen_once=Mock(),
            ),
        )
        with patch(f"{MODULE}.Store"):
            self.manager = CareManager(
                self.hass,
                {
                    "confirmation_hours": 6,
                    "overdue_hours": 24,
                    "stale_minutes": 20,
                    "voice_speaker": SPEAKER,
                },
            )
        self.manager.records["a"] = {
            "status": "due",
            "due_since": 123456,
            "notified_status": None,
            "delivery_attempts": 3,
        }
        self.manager.entities["a"] = SimpleNamespace(entity_id=PLANT)
        self.manager.load = AsyncMock()
        self.manager.async_refresh = AsyncMock()
        with (
            patch(f"{MODULE}.CareManager", return_value=self.manager),
            patch(f"{MODULE}.discovery.async_load_platform", new=AsyncMock()),
            patch(f"{MODULE}.async_track_time_interval"),
            patch(f"{MODULE}._register_photo_service"),
        ):
            await async_setup(self.hass, {DOMAIN: self.manager.config})
        self.callbacks = {
            call.args[1]: call.args[2]
            for call in self.hass.services.async_register.call_args_list
        }
        self.announce = AsyncMock()
        self.enterContext(patch(f"{MODULE}.async_announce", new=self.announce))

    async def _call(self, service: str, context: Context) -> None:
        """Invoke a captured callback with an authentic caller context."""
        await self.callbacks[service](
            SimpleNamespace(data=dict(CALL_DATA[service]), context=context)
        )

    def _assert_no_delivery_or_save(self) -> None:
        """Check externally visible effects remain absent after a denial."""
        self.announce.assert_not_awaited()
        self.manager.async_refresh.assert_not_awaited()
        self.manager.store.async_delay_save.assert_not_called()

    async def test_observer_cannot_announce(self) -> None:
        before = deepcopy(self.manager.records)
        with self.assertRaises(Unauthorized):
            await self._call("announce", Context(user_id="observer"))
        self.assertEqual(self.manager.records, before)
        self.permissions.check_entity.assert_called_once_with(SPEAKER, POLICY_CONTROL)
        self._assert_no_delivery_or_save()

    async def test_allowed_announcement_preserves_caller_context(self) -> None:
        self.controlled_entities.add(SPEAKER)
        context = Context(user_id="speaker_operator")
        await self._call("announce", context)
        self.announce.assert_awaited_once_with(
            self.hass,
            SPEAKER,
            CALL_DATA["announce"]["message"],
            context=context,
        )
        self.assertIs(self.announce.await_args.kwargs["context"], context)
        self.permissions.check_entity.assert_called_once_with(SPEAKER, POLICY_CONTROL)

    async def test_target_change_during_auth_uses_the_authorized_target(self) -> None:
        self.controlled_entities.add(SPEAKER)
        entered, release = asyncio.Event(), asyncio.Event()
        context = Context(user_id="speaker_operator")

        async def get_user(_user_id: str) -> SimpleNamespace:
            entered.set()
            await release.wait()
            return self.user

        self.hass.auth.async_get_user.side_effect = get_user
        operation = asyncio.create_task(self._call("announce", context))
        try:
            await asyncio.wait_for(entered.wait(), timeout=TEST_TIMEOUT)
            self.manager.config["voice_speaker"] = "media_player.uncontrolled_speaker"
        finally:
            release.set()
            await asyncio.wait_for(operation, timeout=TEST_TIMEOUT)
        self.permissions.check_entity.assert_called_once_with(SPEAKER, POLICY_CONTROL)
        self.announce.assert_awaited_once_with(
            self.hass,
            SPEAKER,
            CALL_DATA["announce"]["message"],
            context=context,
        )

    async def test_denied_reminder_controls_do_not_mutate_records(self) -> None:
        # Controlling the speaker must not confer control of the plant sensor.
        self.controlled_entities.add(SPEAKER)
        before = deepcopy(self.manager.records)
        for service in ("snooze", "acknowledge"):
            with self.subTest(service=service):
                with self.assertRaises(Unauthorized):
                    await self._call(service, Context(user_id="speaker_operator"))
                self.assertEqual(self.manager.records, before)
                self.permissions.check_entity.assert_called_with(PLANT, POLICY_CONTROL)
                self._assert_no_delivery_or_save()

    async def test_unknown_user_is_rejected_before_effects(self) -> None:
        self.hass.auth.async_get_user.return_value = None
        before = deepcopy(self.manager.records)
        for service in CALL_DATA:
            with self.subTest(service=service):
                with self.assertRaises(UnknownUser):
                    await self._call(service, Context(user_id="deleted_user"))
                self.assertEqual(self.manager.records, before)
                self._assert_no_delivery_or_save()
        self.permissions.check_entity.assert_not_called()

    async def test_allowed_plant_user_can_control_reminders(self) -> None:
        self.controlled_entities.add(PLANT)
        context = Context(user_id="plant_operator")
        await self._call("acknowledge", context)
        record = self.manager.records["a"]
        self.assertEqual(record["notified_status"], "due")
        self.assertEqual(record["delivery_attempts"], 0)
        await self._call("snooze", context)
        self.assertIn("snoozed_until", record)
        self.assertIsNone(record["notified_status"])
        self.manager.async_refresh.assert_awaited_once_with(wait=True)
        self.assertEqual(self.manager.store.async_delay_save.call_count, 2)
        self.assertEqual(self.permissions.check_entity.call_count, 2)
        self.permissions.check_entity.assert_called_with(PLANT, POLICY_CONTROL)
        self.announce.assert_not_awaited()

    async def test_userless_internal_automation_remains_allowed(self) -> None:
        context = Context()
        await self._call("announce", context)
        await self._call("acknowledge", context)
        record = self.manager.records["a"]
        self.assertEqual(record["notified_status"], "due")
        await self._call("snooze", context)
        self.assertIn("snoozed_until", record)
        self.assertIsNone(record["notified_status"])
        self.announce.assert_awaited_once_with(
            self.hass,
            SPEAKER,
            CALL_DATA["announce"]["message"],
            context=context,
        )
        self.hass.auth.async_get_user.assert_not_awaited()
        self.permissions.check_entity.assert_not_called()
        self.manager.async_refresh.assert_awaited_once_with(wait=True)
        self.assertEqual(self.manager.store.async_delay_save.call_count, 2)
