"""Phone-only discovery and private keyboard notification lifecycle; no real push."""
import asyncio
import attr
from copy import deepcopy
import importlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from homeassistant import loader
from homeassistant.auth.permissions.const import POLICY_READ, POLICY_CONTROL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame

TV = "remote.televizor"
KODI = "media_player.home"
PRIVATE = "NEVER-PUSH-PRIVATE-FIELD"


class Permissions:
    def __init__(self):
        self.allowed = {(TV, POLICY_READ),(TV,POLICY_CONTROL),(KODI,POLICY_READ),(KODI,POLICY_CONTROL)}
    def check_entity(self,entity,policy):
        return (entity,policy) in self.allowed


class KeyboardNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(__file__).resolve().parents[2]
        self.assertTrue((root/"custom_components/ekf_appliances/keyboard_notifications.py").exists(),
                        "Authorized phone keyboard notifications are not implemented")
        self.module = importlib.import_module("custom_components.ekf_appliances.keyboard_notifications")
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.users = {}
        self.hass.auth = SimpleNamespace(
            async_get_users=AsyncMock(side_effect=lambda:list(self.users.values())),
            async_get_user=AsyncMock(side_effect=lambda user_id:self.users.get(user_id)),
        )
        self.rows, self.entries, self.calls = [], {}, []
        self.hass.data["notify_services"] = {"mobile_app":self.rows}
        self.hass.data["mobile_app"] = {"config_entries":self.entries}
        self.targets = {"tv":SimpleNamespace(entity_id=TV,provider=None),
                        "projector":SimpleNamespace(entity_id=KODI,provider=None)}
        self.notifier = self.module.KeyboardNotifications(self.hass, targets=self.targets)
        dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        self.registry = er.async_get(self.hass)
        await self.registry.async_load()

    async def asyncTearDown(self):
        if not hasattr(self,"hass"):
            return
        result = self.notifier.async_close()
        if inspect.isawaitable(result): await result
        await self.hass.async_stop()
        self.temp.cleanup()

    def user(self,user_id="main",home=True,active=True):
        user = SimpleNamespace(id=user_id,is_active=active,is_admin=False,is_owner=False,permissions=Permissions())
        self.users[user_id]=user
        if home is not None:
            self.hass.states.async_set("person."+user_id,"home" if home else "not_home",{"user_id":user_id})
        return user

    def phone(self,name="mobile_app_phone",user_id="main",model="iPhone16,1",os_name="iOS",register=True):
        webhook = "webhook-"+name
        entry = SimpleNamespace(entry_id="entry-"+name,domain="mobile_app",data={
            "user_id":user_id,"os_name":os_name,"model":model,"app_id":"io.robbie.HomeAssistant" if os_name=="iOS" else "io.homeassistant.companion.android",
            "device_id":"device-"+name,"device_name":name,
        })
        self.entries[webhook]=entry
        self.rows.append(SimpleNamespace(registered_targets={name:webhook}))
        if register:
            async def capture(call):
                self.calls.append((name,deepcopy(dict(call.data))))
            self.hass.services.async_register("notify",name,capture)
        return entry,webhook

    def tracker(self,entry,state="home",suffix="phone"):
        entity = self.registry.async_get_or_create("device_tracker", "mobile_app",
                                                    "tracker-"+suffix, suggested_object_id=suffix)
        # Model an existing persisted registration using HA's complete immutable
        # registry entry, rather than relying on obsolete constructor defaults.
        entity = attr.evolve(entity, config_entry_id=entry.entry_id)
        self.registry.entities[entity.entity_id]=entity
        self.hass.states.async_set(entity.entity_id,state)

    def snapshot(self,**changes):
        return {
            "pair":"tv","session_id":str(uuid4()),"revision":"7","focused":True,"sensitive":False,
            "text":PRIVATE,"selection":None,"capabilities":{"edit":True,"commit":True,"close":True,"selection":False},
            "status":"ready","fresh_at":"2026-11-13T18:00:00Z",**changes,
        }

    async def refresh(self,pair,snapshot):
        await self.notifier.async_refresh(pair,snapshot)
        for _ in range(10):await asyncio.sleep(0)

    async def test_registered_home_phone_is_discovered_once_and_unregistered_names_are_ignored(self):
        self.user()
        _,webhook=self.phone()
        self.rows.append(SimpleNamespace(registered_targets={"mobile_app_phone":webhook}))
        self.phone("mobile_app_missing_service",register=False)
        # A similarly named service is not an authenticated mobile_app registration.
        self.hass.services.async_register("notify","mobile_app_unregistered_iphone",AsyncMock())
        targets=await self.module.async_phone_targets(self.hass,TV)
        self.assertEqual(targets,{"mobile_app_phone":"main"})
        await self.refresh("tv",self.snapshot())
        self.assertEqual(len(self.calls),1)

    async def test_watches_tvs_tablets_and_inactive_or_unauthorized_users_are_not_targets(self):
        self.user()
        self.phone()
        for name,model in [("watch","Watch6,1"),("tv","AppleTV6,2"),("tablet","iPad13,4")]:
            self.phone("mobile_app_"+name,model=model)
        inactive=self.user("inactive",active=False);self.phone("mobile_app_inactive","inactive")
        denied=self.user("denied");denied.permissions.allowed.discard((TV,POLICY_CONTROL));self.phone("mobile_app_denied","denied")
        self.assertEqual(await self.module.async_phone_targets(self.hass,TV),{"mobile_app_phone":"main"})

    async def test_home_requires_linked_person_or_tracker_of_that_same_mobile_entry(self):
        self.user(home=False);first,_=self.phone()
        self.user("away",home=None);second,_=self.phone("mobile_app_away","away",model="Pixel 8",os_name="Android")
        self.tracker(first,"home","first")
        self.tracker(second,"unknown","second")
        targets=await self.module.async_phone_targets(self.hass,TV)
        self.assertEqual(targets,{"mobile_app_phone":"main"})
        self.hass.states.async_set("device_tracker.first","not_home")
        self.assertEqual(await self.module.async_phone_targets(self.hass,TV),{})

    async def test_one_private_deeplink_per_session_and_no_text_or_selection_in_push(self):
        self.user();self.phone()
        snap=self.snapshot()
        await self.refresh("tv",snap)
        await self.refresh("tv",{**snap,"revision":"8","text":"ANOTHER-PRIVATE"})
        await self.refresh("tv",{**snap,"revision":"9","selection":{"start":1,"end":1}})
        self.assertEqual(len(self.calls),1)
        payload=self.calls[0][1]
        serialized=json.dumps(payload,ensure_ascii=False)
        self.assertNotIn(PRIVATE,serialized);self.assertNotIn("ANOTHER-PRIVATE",serialized)
        self.assertNotIn("selection",serialized);self.assertNotIn("revision",serialized)
        self.assertIn("/living-room/keyboard?pair=tv",serialized)
        self.assertIn("ekf-keyboard-tv",serialized)
        self.assertNotIn(snap["session_id"],serialized)

    async def test_absent_phone_is_not_invented_and_new_registration_can_receive_active_session(self):
        self.user();self.phone()
        self.user("lena",home=True)
        snap=self.snapshot()
        await self.refresh("projector",{**snap,"pair":"projector"})
        self.assertEqual([name for name,_ in self.calls],["mobile_app_phone"])
        self.phone("mobile_app_lena","lena")
        await self.refresh("projector",{**snap,"pair":"projector","revision":"8"})
        self.assertEqual([name for name,_ in self.calls],["mobile_app_phone","mobile_app_lena"])
        self.assertIn("/living-room/keyboard?pair=projector",json.dumps(self.calls[-1][1]))

    async def test_blur_clears_only_previously_notified_phones_and_revision_updates_do_not_renotify(self):
        self.user();self.phone()
        snap=self.snapshot()
        await self.refresh("tv",snap)
        await self.refresh("tv",{**snap,"focused":False,"text":None,"capabilities":{}})
        await self.refresh("tv",{**snap,"focused":False,"text":None,"capabilities":{}})
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[1][0],"mobile_app_phone")
        self.assertEqual(self.calls[1][1]["message"],"clear_notification")
        self.assertIn("ekf-keyboard-tv",json.dumps(self.calls[1][1]))

    async def test_sensitive_unknown_or_noneditable_fields_do_not_notify(self):
        self.user();self.phone()
        for override in [{"sensitive":True},{"sensitive":None},{"focused":False},{"status":"unknown"},{"capabilities":{"edit":False}}]:
            await self.refresh("tv",self.snapshot(**override))
        self.assertEqual(self.calls,[])

    async def test_revoked_control_clears_existing_notification_and_blocks_new_session(self):
        user=self.user();self.phone()
        snap=self.snapshot()
        await self.refresh("tv",snap)
        user.permissions.allowed.discard((TV,POLICY_CONTROL))
        await self.refresh("tv",{**snap,"revision":"8"})
        await self.refresh("tv",self.snapshot())
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[1][1]["message"],"clear_notification")

    async def test_usable_own_phone_location_overrides_person_home_but_unknown_can_fallback(self):
        self.user(home=True)
        entry,_=self.phone()
        self.tracker(entry,"not_home","owned")
        for state in ("not_home","studio"):
            self.hass.states.async_set("device_tracker.owned",state)
            self.assertEqual(await self.module.async_phone_targets(self.hass,TV),{})
        for state,attributes in (("unknown",{}),("unavailable",{}),("not_home",{"restored":True})):
            self.hass.states.async_set("device_tracker.owned",state,attributes)
            self.assertEqual(await self.module.async_phone_targets(self.hass,TV),{"mobile_app_phone":"main"})

    async def test_registration_revoked_while_first_push_waits_is_not_used_for_second_push(self):
        self.user()
        self.phone("mobile_app_a")
        _,second_webhook=self.phone("mobile_app_b")
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow_first(call):
            self.calls.append(("mobile_app_a",deepcopy(dict(call.data))))
            entered.set()
            await release.wait()
        self.hass.services.async_register("notify","mobile_app_a",slow_first)
        sending=asyncio.create_task(self.refresh("tv",self.snapshot()))
        try:
            await asyncio.wait_for(entered.wait(),0.5)
            del self.entries[second_webhook]
        finally:
            release.set()
        await sending
        self.assertEqual([name for name,_ in self.calls],["mobile_app_a"])

    async def test_uncertain_failed_push_is_still_cleared_when_keyboard_closes(self):
        from homeassistant.exceptions import HomeAssistantError
        self.user();self.phone()
        async def uncertain(call):
            data=deepcopy(dict(call.data));self.calls.append(("mobile_app_phone",data))
            if data.get("message")!="clear_notification":
                raise HomeAssistantError("Delivery may have completed before connection loss")
        self.hass.services.async_register("notify","mobile_app_phone",uncertain)
        snap=self.snapshot()
        await self.refresh("tv",snap)
        await self.refresh("tv",{**snap,"focused":False,"text":None,"capabilities":{}})
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[-1][1]["message"],"clear_notification")
