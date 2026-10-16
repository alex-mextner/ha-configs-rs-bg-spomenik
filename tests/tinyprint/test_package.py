# Copyright (c) 2026 Alex Ultra
"""Verify core font configuration without reading local secret values."""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


class _TaggedSafeLoader(yaml.SafeLoader):
    """Preserve HA tag values without resolving includes or secrets."""


def _tagged_value(loader: yaml.SafeLoader, _tag: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_TaggedSafeLoader.add_multi_constructor("!", _tagged_value)


def _load(path: Path) -> dict[str, Any]:
    loader = _TaggedSafeLoader(path.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def test_font_allowlist_is_in_core_not_package() -> None:
    core = _load(ROOT / "configuration.yaml")
    package = _load(ROOT / "packages/tinyprint.yaml")
    font = Path(package["tinyprint"]["printers"][0]["font_path"])
    if str(font.parent) not in core["homeassistant"].get("allowlist_external_dirs", []):
        pytest.fail("Font allowlist must be in the root homeassistant configuration")
    if "allowlist_external_dirs" in package.get("homeassistant", {}):
        pytest.fail("HA ignores this core option when it is inside a package")


def test_address_uses_local_secret_and_script_is_manual() -> None:
    text = (ROOT / "packages/tinyprint.yaml").read_text()
    package = _load(ROOT / "packages/tinyprint.yaml")
    if "!secret tinyprint_ble_address" not in text:
        pytest.fail("Printer MAC must reference the local secret")
    if "automation" in package:
        pytest.fail("The persistent package must not trigger automatic print jobs")
    if package["script"]["tinyprint_test_ticket"]["mode"] != "single":
        pytest.fail("The test script must not overlap print jobs")
