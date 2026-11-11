"""The native speaker selector accepts only supported transports."""
from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from custom_components.plant_care_alerts.config_flow import (
    PlantCareConfigFlow, PlantCareOptionsFlow, valid_target, voice_schema,
)


class TestSpeakerSettings(unittest.TestCase):
    def test_selector_has_native_entity_filter_and_saved_default(self):
        schema = voice_schema("notify.aisis_speaker_notify")
        key, value = next(iter(schema.schema.items()))
        self.assertEqual(key.default(), "notify.aisis_speaker_notify")
        self.assertEqual(
            value.config["filter"],
            [{"domain": ["notify"], "integration": "aisis_speaker"},
             {"domain": ["media_player"], "integration": "yandex_station"}],
        )

    def test_options_flow_is_available(self):
        self.assertIsInstance(
            PlantCareConfigFlow.async_get_options_flow(None), PlantCareOptionsFlow
        )

    def test_validation_rejects_mixed_domains_or_unrelated_notify(self):
        cases = [
            ("notify.aisis", "aisis_speaker", True),
            ("media_player.yandex", "yandex_station", True),
            ("media_player.aisis", "aisis_speaker", False),
            ("notify.phone", "mobile_app", False),
            ("media_player.cast", "cast", False),
        ]
        for entity_id, platform, expected in cases:
            with self.subTest(entity_id=entity_id):
                registry = SimpleNamespace(async_get=lambda _: SimpleNamespace(platform=platform))
                with patch("custom_components.plant_care_alerts.config_flow.er.async_get",
                           return_value=registry):
                    self.assertEqual(valid_target(None, entity_id), expected)

    def test_validation_rejects_unknown_entity(self):
        registry = SimpleNamespace(async_get=lambda _: None)
        with patch("custom_components.plant_care_alerts.config_flow.er.async_get",
                   return_value=registry):
            self.assertFalse(valid_target(None, "notify.missing"))
