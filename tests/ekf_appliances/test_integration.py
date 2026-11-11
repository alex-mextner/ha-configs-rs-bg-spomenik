"""Appliance behavior using real HA state/services and an in-memory IR boundary."""
import asyncio
from datetime import timedelta
import logging
import base64
from pathlib import Path
import struct
import sys
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er, device_registry as dr, restore_state, frame
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from custom_components.ekf_appliances.runtime import ApplianceRuntime
from custom_components.ekf_appliances.climate import HaierClimate
from custom_components.ekf_appliances.media_player import ProjectorMediaPlayer
from custom_components.ekf_appliances.button import ProjectorButton

CONFIG = {
    "remote_entity_id":"remote.ekf_connect_ir",
    "temperature_entity_id":"sensor.room_temperature",
    "projector_power_entity_id":"input_boolean.projector_power",
    "projector_on_script":"script.projector_turn_on",
    "projector_off_script":"script.projector_turn_off",
}


class ApplianceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        await restore_state.async_load(self.hass, load_empty=True)
        self.entry = ConfigEntry(
            entry_id="ekf-test", data=CONFIG, domain="ekf_appliances", options={},
            discovery_keys=MappingProxyType({}), subentries_data=[], title="Test EKF",
            source="user", version=1, minor_version=1, unique_id="test-remote",
        )
        with patch.object(self.hass.config_entries, "async_setup", AsyncMock(return_value=True)):
            await self.hass.config_entries.async_add(self.entry)
        self.platforms = {}

        self.hass.states.async_set("remote.ekf_connect_ir", "on")
        self.hass.states.async_set("sensor.room_temperature", "23.5", {"unit_of_measurement":"°C"})
        self.hass.states.async_set("input_boolean.projector_power", "off")
        self.commands = []
        self.script_calls = []
        self.fail_at = None
        self.entities = []
        self.real_sleep = asyncio.sleep

        async def send(call):
            self.commands.append(dict(call.data))
            if self.fail_at == len(self.commands):
                raise HomeAssistantError("transmitter failure")

        async def on(call):
            self.script_calls.append("on")
            self.hass.states.async_set("input_boolean.projector_power", "on")

        async def off(call):
            self.script_calls.append("off")
            self.hass.states.async_set("input_boolean.projector_power", "off")

        self.hass.services.async_register("remote", "send_command", send)
        self.hass.services.async_register("script", "projector_turn_on", on)
        self.hass.services.async_register("script", "projector_turn_off", off)
        self.runtime = ApplianceRuntime(self.hass, self.entry)
        self.climate = HaierClimate(self.runtime)
        self.player = ProjectorMediaPlayer(self.runtime)
        self.no_delay = patch(
            "custom_components.ekf_appliances.transport.asyncio.sleep",
            self.fast_sleep,
        )
        self.no_delay.start()

    async def fast_sleep(self, seconds):
        await self.real_sleep(0)

    async def attach(self, entity, entity_id, restored=None):
        domain = entity_id.split(".",1)[0]
        if domain not in self.platforms:
            platform = EntityPlatform(
                hass=self.hass, logger=logging.getLogger(__name__), domain=domain,
                platform_name="ekf_appliances", platform=None,
                scan_interval=timedelta(seconds=30), entity_namespace=None,
            )
            platform.config_entry = self.entry
            self.platforms[domain] = platform
        entity.entity_id = entity_id
        if isinstance(entity, HaierClimate):
            with patch.object(entity, "async_get_last_state", AsyncMock(return_value=restored)):
                await self.platforms[domain].async_add_entities([entity])
        else:
            await self.platforms[domain].async_add_entities([entity])
        self.entities.append(entity)
        return entity

    async def asyncTearDown(self):
        self.no_delay.stop()
        for platform in self.platforms.values():
            await platform.async_reset()
        await self.hass.async_block_till_done()
        self.tmp.cleanup()

    async def test_legacy_comfort_restore_normalizes_without_transmitting(self):
        restored = State("climate.old", "heat", {
            "temperature":27, "fan_mode":"low", "swing_mode":"vertical",
            "preset_mode":"comfort", "last_hvac_mode":"heat",
        })
        await self.attach(self.climate, "climate.ekf_haier", restored)
        await self.attach(self.player, "media_player.ekf_projector")
        self.assertEqual(self.commands, [])
        self.assertEqual(self.script_calls, [])
        self.assertEqual(self.climate.hvac_mode, "heat")
        self.assertEqual(self.climate.target_temperature, 27)
        self.assertEqual(self.climate.fan_mode, "low")
        self.assertEqual(self.climate.swing_mode, "vertical")
        self.assertEqual(self.climate.preset_mode, "none")
        self.assertEqual(self.climate.extra_state_attributes["last_hvac_mode"], "heat")
        self.assertTrue(self.climate.assumed_state)

    async def test_comfort_preset_is_not_exposed_or_accepted(self):
        self.assertEqual(self.climate.preset_mode, "none")
        await self.attach(self.climate, "climate.ekf_haier", State(
            "climate.old", "heat", {"temperature":27, "preset_mode":"none"}
        ))
        self.assertEqual(self.climate.preset_modes, ["none", "sleep", "boost"])
        with self.assertRaises(ServiceValidationError):
            await self.climate.async_set_preset_mode("comfort")
        self.assertEqual(self.climate.preset_mode, "none")
        self.assertEqual(self.climate.hvac_mode, "heat")
        self.assertEqual(self.climate.target_temperature, 27)
        self.assertEqual(self.commands, [])

    async def test_off_temperature_change_is_saved_without_turning_on(self):
        await self.attach(self.climate, "climate.ekf_haier")
        await self.climate.async_set_temperature(temperature=26)
        self.assertEqual(self.climate.hvac_mode, "off")
        self.assertEqual(self.climate.target_temperature, 26)
        self.assertEqual(self.commands, [])

    async def test_explicit_off_sends_even_when_assumed_off(self):
        await self.attach(self.climate, "climate.ekf_haier")
        await self.climate.async_turn_off()
        self.assertEqual(len(self.commands), 1)
        self.assertTrue(all(x["command"] == self.commands[0]["command"] for x in self.commands))
        self.assertEqual(self.commands[0]["entity_id"], "remote.ekf_connect_ir")
        self.assertEqual(self.commands[0]["num_repeats"], 1)
        self.assertEqual(self.commands[0]["delay_secs"], 0)

    async def test_failed_send_keeps_last_operating_state_and_settings(self):
        await self.attach(self.climate, "climate.ekf_haier")
        self.fail_at = 1
        with self.assertRaisesRegex(HomeAssistantError, "transmitter"):
            await self.climate.async_set_temperature(temperature=28, hvac_mode="cool")
        self.assertEqual(self.climate.hvac_mode, "off")
        self.assertEqual(self.climate.target_temperature, 24)
        self.assertEqual(len(self.commands), 1)

    async def test_invalid_values_do_not_transmit_or_mutate(self):
        await self.attach(self.climate, "climate.ekf_haier")
        for temp in (15,31,24.5,float("nan"),float("inf"),True):
            with self.subTest(temp=temp), self.assertRaises(ServiceValidationError):
                await self.climate.async_set_temperature(temperature=temp)
        with self.assertRaises(ServiceValidationError):
            await self.climate.async_set_hvac_mode("heat_cool")
        with self.assertRaises(ServiceValidationError):
            await self.climate.async_set_fan_mode("max")
        self.assertEqual(self.climate.target_temperature, 24)
        self.assertEqual(self.commands, [])

    async def test_turn_on_restores_previous_non_off_mode(self):
        await self.attach(self.climate, "climate.ekf_haier", State(
            "climate.old","off",{"temperature":25,"last_hvac_mode":"heat"}
        ))
        await self.climate.async_turn_on()
        self.assertEqual(self.climate.hvac_mode, "heat")
        self.assertEqual(len(self.commands), 1)

    async def test_concurrent_climate_changes_do_not_lose_settings(self):
        await self.attach(self.climate, "climate.ekf_haier")
        await self.climate.async_set_hvac_mode("cool")
        self.commands.clear()
        await asyncio.gather(
            self.climate.async_set_temperature(temperature=28),
            self.climate.async_set_fan_mode("high"),
        )
        self.assertEqual(self.climate.target_temperature, 28)
        self.assertEqual(self.climate.fan_mode, "high")
        self.assertEqual(len(self.commands), 2)
        first = self.commands[0]["command"]
        second = self.commands[1]["command"]
        self.assertNotEqual(first, second)
        self.assertEqual([x["command"] for x in self.commands], [first,second])

    async def test_temperature_subscription_handles_real_units_and_unknowns(self):
        await self.attach(self.climate, "climate.ekf_haier")
        self.assertEqual(self.climate.current_temperature, 23.5)
        self.hass.states.async_set("sensor.room_temperature", "77", {"unit_of_measurement":"°F"})
        await self.hass.async_block_till_done()
        self.assertAlmostEqual(self.climate.current_temperature, 25)
        for value in ("unavailable","unknown","nan","inf","not-a-temperature"):
            self.hass.states.async_set("sensor.room_temperature", value, {"unit_of_measurement":"°C"})
            await self.hass.async_block_till_done()
            self.assertIsNone(self.climate.current_temperature)
        self.assertEqual(self.commands, [])

    async def test_invalid_restored_settings_fall_back_without_ir(self):
        await self.attach(self.climate, "climate.ekf_haier", State(
            "climate.old","invalid",{
                "temperature":float("nan"),"fan_mode":"max",
                "swing_mode":"both","preset_mode":"holiday","last_hvac_mode":"invalid",
            }
        ))
        self.assertEqual(self.climate.hvac_mode, "off")
        self.assertEqual(self.climate.target_temperature, 24)
        self.assertEqual(self.climate.fan_mode, "auto")
        self.assertEqual(self.commands, [])

    async def test_boost_is_not_claimed_in_unsupported_active_mode(self):
        await self.attach(self.climate, "climate.ekf_haier")
        await self.climate.async_set_hvac_mode("dry")
        self.commands.clear()
        with self.assertRaises(ServiceValidationError):
            await self.climate.async_set_preset_mode("boost")
        self.assertEqual(self.climate.preset_mode, "none")
        self.assertEqual(self.commands, [])

    async def test_unavailable_remote_blocks_send_and_exposes_availability(self):
        await self.attach(self.climate, "climate.ekf_haier")
        self.hass.states.async_set("remote.ekf_connect_ir", "unavailable")
        await self.hass.async_block_till_done()
        self.assertFalse(self.climate.available)
        with self.assertRaises(HomeAssistantError):
            await self.climate.async_turn_on()
        self.assertEqual(self.commands, [])

    async def test_projector_power_is_script_based_and_idempotent(self):
        await self.attach(self.player, "media_player.ekf_projector")
        self.assertEqual(self.player.state, "off")
        await asyncio.gather(self.player.async_turn_on(), self.player.async_turn_on())
        self.assertEqual(self.script_calls, ["on"])
        self.assertEqual(self.player.state, "on")
        await self.player.async_turn_off()
        await self.player.async_turn_off()
        self.assertEqual(self.script_calls, ["on","off"])
        self.assertEqual(self.commands, [])

    async def test_unknown_projector_power_is_unavailable_and_not_guessed(self):
        await self.attach(self.player, "media_player.ekf_projector")
        self.hass.states.async_set("input_boolean.projector_power", "unknown")
        await self.hass.async_block_till_done()
        self.assertFalse(self.player.available)
        with self.assertRaises(HomeAssistantError):
            await self.player.async_turn_on()
        self.assertEqual(self.script_calls, [])

    async def test_projector_media_controls_and_button_use_same_verified_frame(self):
        await self.attach(self.player, "media_player.ekf_projector")
        button = ProjectorButton(self.runtime, "vol_up")
        await self.attach(button, "button.ekf_projector_vol_up")
        await self.player.async_volume_up()
        await button.async_press()
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(self.commands[0]["command"], self.commands[1]["command"])
        self.assertTrue(self.commands[0]["command"].startswith("b64:"))

    async def test_devices_are_separate_with_stable_command_identifiers(self):
        button = ProjectorButton(self.runtime, "power")
        self.assertEqual(self.climate.device_info["manufacturer"], "Haier")
        self.assertEqual(self.player.device_info["manufacturer"], "Toshiba")
        self.assertNotEqual(self.climate.device_info["identifiers"], self.player.device_info["identifiers"])
        self.assertEqual(button.device_info["identifiers"], self.player.device_info["identifiers"])
        self.assertEqual(button.unique_id, "ekf-test_projector_power")

    async def test_invalid_temperature_unit_is_unknown_without_breaking_entity(self):
        await self.attach(self.climate, "climate.ekf_haier")
        for unit in ("%", "C", None):
            self.hass.states.async_set("sensor.room_temperature", "25", {"unit_of_measurement":unit})
            self.assertIsNone(self.climate.current_temperature)
        self.assertEqual(self.commands, [])

    async def test_projector_mute_is_raw_toggle_not_absolute_media_feature(self):
        self.assertFalse(self.player.supported_features & MediaPlayerEntityFeature.VOLUME_MUTE)
        mute = ProjectorButton(self.runtime, "mute")
        await self.attach(mute, "button.ekf_projector_mute")
        await mute.async_press()
        self.assertEqual(len(self.commands), 1)

    async def test_native_target_survives_display_unit_conversion(self):
        await self.attach(self.climate, "climate.ekf_haier", State(
            "climate.old", "off", {"temperature":75, "native_target_temperature":24}
        ))
        self.assertEqual(self.climate.target_temperature, 24)
        self.assertEqual(self.climate.extra_state_attributes["native_target_temperature"], 24)

    async def test_registered_gateway_survives_canonical_entity_renames(self):
        gateway_entry = ConfigEntry(
            entry_id="tuya-local-test", data={}, domain="tuya_local", options={},
            discovery_keys=MappingProxyType({}), subentries_data=[], title="EKF gateway",
            source="user", version=1, minor_version=1, unique_id="gateway-test",
        )
        with patch.object(self.hass.config_entries, "async_setup", AsyncMock(return_value=True)):
            await self.hass.config_entries.async_add(gateway_entry)
        devices = dr.async_get(self.hass)
        gateway = devices.async_get_or_create(
            config_entry_id=gateway_entry.entry_id, config_subentry_id=None,
            identifiers={("tuya_local", "gateway-test")}, name="EKF gateway",
        )
        registry = er.async_get(self.hass)
        self.hass.states.async_remove("remote.ekf_connect_ir")
        remote = registry.async_get_or_create(
            "remote", "tuya_local", "gateway-remote",
            suggested_object_id="ekf_connect_ir", config_entry=gateway_entry,
            device_id=gateway.id,
        )
        self.assertEqual(remote.entity_id, "remote.ekf_connect_ir")
        self.hass.states.async_set(remote.entity_id, "on")
        mapping = [
            (HaierClimate(self.runtime), "climate.ekf_haier", "climate.kondei_1"),
            (ProjectorMediaPlayer(self.runtime), "media_player.ekf_projector", "media_player.projector"),
        ]
        for key in ("power", "play_pause", "source", "up", "down", "left", "right",
                    "menu", "back", "vol_down", "mute", "vol_up"):
            mapping.append((ProjectorButton(self.runtime,key), f"button.ekf_projector_{key}", f"button.projector_{key}"))
        for entity, initial, canonical in mapping:
            await self.attach(entity, initial)
            self.assertIsNotNone(self.hass.states.get(initial), f"Gateway-linked entity failed to register: {initial}")
            before = registry.async_get(initial)
            registry.async_update_entity(initial, new_entity_id=canonical)
            await self.hass.async_block_till_done()
            after = registry.async_get(canonical)
            state = self.hass.states.get(canonical)
            self.assertIsNotNone(state, canonical)
            self.assertNotEqual(state.state, "unavailable", canonical)
            self.assertEqual(after.unique_id, entity.unique_id)
            self.assertEqual(after.device_id, before.device_id)
            self.assertEqual(devices.async_get(after.device_id).via_device_id, gateway.id)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.script_calls, [])
