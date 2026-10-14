"""Regression for HA's core options being ignored inside a package."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]

class Loader(yaml.SafeLoader):
    pass

def tagged_value(loader, tag, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)

Loader.add_multi_constructor("!", tagged_value)

class TinyPrintPackageTest(unittest.TestCase):
    def test_font_allowlist_is_in_core_not_package(self):
        core = yaml.load((ROOT / "configuration.yaml").read_text(), Loader=Loader)
        package = yaml.load((ROOT / "packages/tinyprint.yaml").read_text(), Loader=Loader)
        font = Path(package["tinyprint"]["printers"][0]["font_path"])
        self.assertIn(str(font.parent), core["homeassistant"].get("allowlist_external_dirs", []))
        self.assertNotIn("allowlist_external_dirs", package.get("homeassistant", {}))

    def test_address_uses_local_secret_and_script_is_manual(self):
        text = (ROOT / "packages/tinyprint.yaml").read_text()
        package = yaml.load(text, Loader=Loader)
        self.assertIn("!secret tinyprint_ble_address", text)
        self.assertNotIn("automation", package)
        self.assertEqual(package["script"]["tinyprint_test_ticket"]["mode"], "single")

if __name__ == "__main__":
    unittest.main()
