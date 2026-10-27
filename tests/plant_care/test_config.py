# Stdlib unittest runs inside the installed HA container without pytest.
# ruff: noqa: PT009
# Copyright (c) 2026 Alex Ultra
"""Plant-care regression tests using standard-library unittest."""

import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


class Loader(yaml.SafeLoader):
    """Exercise plant-care regression scenarios."""


Loader.add_constructor("!input", lambda loader, node: loader.construct_scalar(node))


class DeliveryTests(unittest.TestCase):
    """Exercise plant-care regression scenarios."""

    def load(self, path: Any) -> Any:
        p = ROOT / path
        self.assertTrue(p.exists(), f"Missing implementation: {path}")
        loader = Loader(p.read_text())  # Loader subclasses yaml.SafeLoader.
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()

    def test_blueprint_is_generic_and_hourly(self) -> None:
        d = self.load("blueprints/automation/plant_care/alerts.yaml")
        self.assertIn("speaker", d["blueprint"]["input"])
        self.assertIn("notify_target", d["blueprint"]["input"])
        self.assertTrue(
            any(
                t.get("minutes") == "0" and t.get("id") == "hourly"
                for t in d["triggers"]
            )
        )
        self.assertNotIn("strelitzia", str(d))
        self.assertNotIn("m00zrw", str(d))
        self.assertIn("voice_start", d["blueprint"]["input"])
        self.assertIn("voice_end", d["blueprint"]["input"])

    def test_instance_uses_verified_speaker_and_no_new_sidebar(self) -> None:
        d = self.load("packages/plant_care.yaml")
        inp = d["automation"][0]["use_blueprint"]["input"]
        self.assertEqual(inp["speaker"], "media_player.yandex_station_m00zrw3008rajg")
        self.assertEqual(inp["notify_target"], "mobile_app_echowhiskey536")
        self.assertEqual(inp["voice_start"], "08:00:00")
        self.assertEqual(inp["voice_end"], "22:00:00")
        self.assertNotIn("panel_custom", d)
        self.assertNotIn("lovelace", d)

    def test_freshness_uses_physical_sample_not_display_changes(self) -> None:
        d = self.load("packages/plant_care.yaml")
        self.assertEqual(
            d["plant_care_alerts"]["freshness_sources"][
                "sensor.strelitzia_soil_smoothed"
            ],
            "sensor.strelitzia_soil_adc",
        )

    def test_manifest_uses_hassfest_key_order(self) -> None:
        data = self.load("custom_components/plant_care_alerts/manifest.json")
        self.assertEqual(
            list(data),
            [
                "domain",
                "name",
                *sorted(key for key in data if key not in {"domain", "name"}),
            ],
        )

    def test_card_is_own_mit_code(self) -> None:
        p = ROOT / "www/plant-care/plant-care-card.js"
        self.assertTrue(p.exists(), "Plant card missing")
        text = p.read_text()
        self.assertIn("SPDX-License-Identifier: MIT", text)
        self.assertIn("preview_status", text)
        self.assertNotIn("innerHTML = name", text)


if __name__ == "__main__":
    unittest.main()
