#!/usr/bin/env python3
"""
Validate Lovelace YAML dashboards and local custom-card resources.

This script is intended to run inside the Home Assistant container, where
PyYAML is already available. It deliberately validates the repo-level YAML
dashboard files, not HA runtime storage.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path("/config") if Path("/config/configuration.yaml").exists() else Path.cwd()
CONFIG_PATH = ROOT / "configuration.yaml"


class HALoader(yaml.SafeLoader):
    """YAML loader that accepts Home Assistant tags such as !secret."""


def construct_ha_tag(loader: HALoader, _tag_suffix: str, node: yaml.Node) -> object:
    """Read a Home Assistant tag using its underlying safe YAML node type."""
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


HALoader.add_multi_constructor("!", construct_ha_tag)


def repo_path(path: str | Path) -> Path:
    """Resolve a dashboard path against the configuration root."""
    raw = Path(path)
    if raw.is_absolute():
        return raw
    return ROOT / raw


def display_path(path: Path) -> str:
    """Return a readable path relative to the configuration root when possible."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_yaml(path: Path) -> object:
    """Load configuration with the Home Assistant tag-aware safe loader."""
    with path.open(encoding="utf-8") as handle:
        # HALoader extends SafeLoader; its tags use safe node constructors.
        return yaml.load(handle, Loader=HALoader)  # noqa: S506


def clean_resource_url(url: str) -> str:
    """Extract a resource path without query parameters or fragments."""
    split = urlsplit(url)
    return split.path or url.split("?", 1)[0].split("#", 1)[0]


def local_resource_path(url: str) -> Path | None:
    """Map a local Lovelace URL to its file beneath the configuration root."""
    path = clean_resource_url(url)
    if path.startswith("/local/"):
        return ROOT / "www" / path.removeprefix("/local/")
    if path.startswith("local/"):
        return ROOT / "www" / path.removeprefix("local/")
    return None


def resource_name_hint(url: str) -> str:
    """Return the filename stem used to match custom-card resource names."""
    path = clean_resource_url(url)
    return Path(path).stem


def iter_cards(
    cards: object, location: str
) -> Iterator[tuple[str, dict | None, str | None]]:
    """Yield each nested card with its location or a structural error."""
    if cards is None:
        return
    if not isinstance(cards, list):
        yield location, None, "cards must be a list"
        return
    for index, card in enumerate(cards):
        card_location = f"{location}.cards[{index}]"
        if not isinstance(card, dict):
            yield card_location, None, "card must be a mapping"
            continue
        yield card_location, card, None
        for key in ("cards",):
            if key in card:
                yield from iter_cards(card[key], f"{card_location}.{key}")


def _local_custom_elements(
    local_path: Path | None, url: str, define_re: re.Pattern[str], errors: list[str]
) -> list[str]:
    """Read one local resource after preserving its missing-file diagnostic."""
    if local_path is None:
        return []
    if not local_path.exists():
        errors.append(
            f"configuration.yaml: local Lovelace resource {url!r} "
            "points to missing "
            f"{display_path(local_path)}"
        )
        return []
    if local_path.suffix == ".js":
        text = local_path.read_text(encoding="utf-8", errors="replace")
        return define_re.findall(text)

    return []


def collect_resources(config: dict) -> tuple[list[dict], set[str], set[str], list[str]]:
    """Collect resource names, registered custom elements, and validation errors."""
    errors: list[str] = []
    lovelace = config.get("lovelace") or {}
    resources = lovelace.get("resources") or []
    if resources is None:
        resources = []
    if not isinstance(resources, list):
        return (
            [],
            set(),
            set(),
            ["configuration.yaml: lovelace.resources must be a list"],
        )
    if resources and lovelace.get("resource_mode") != "yaml":
        errors.append(
            "configuration.yaml: lovelace.resources requires "
            "lovelace.resource_mode: yaml"
        )

    resource_names: set[str] = set()
    defined_custom_elements: set[str] = set()
    normalized: list[dict] = []
    define_re = re.compile(r"customElements\.define\(\s*['\"]([^'\"]+)['\"]")

    for index, item in enumerate(resources):
        if not isinstance(item, dict):
            errors.append(
                f"configuration.yaml: lovelace.resources[{index}] must be a mapping"
            )
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            errors.append(
                f"configuration.yaml: lovelace.resources[{index}].url "
                "must be a non-empty string"
            )
            continue
        resource_type = item.get("type")
        if resource_type not in {"module", "js", "css"}:
            errors.append(
                f"configuration.yaml: lovelace.resources[{index}].type must be one of "
                "module, js, css"
            )
        resource_names.add(resource_name_hint(url))
        local_path = local_resource_path(url)
        normalized.append({"url": url, "local_path": local_path})
        defined_custom_elements.update(
            _local_custom_elements(local_path, url, define_re, errors)
        )

    return normalized, resource_names, defined_custom_elements, errors


def _validate_sections(sections: object, location: str) -> list[str]:
    """Validate section mappings without changing the reported error order."""
    if not isinstance(sections, list) or not sections:
        return [f"{location}: sections view requires a non-empty sections list"]
    errors: list[str] = []
    for section_index, section in enumerate(sections):
        section_location = f"{location}.sections[{section_index}]"
        if not isinstance(section, dict):
            errors.append(f"{section_location}: section must be a mapping")
            continue
        for _, _, err in iter_cards(section.get("cards"), section_location):
            if err:
                errors.append(f"{section_location}: {err}")
    return errors


def _card_type_errors(
    card: dict,
    card_location: str,
    resource_names: set[str],
    defined_custom_elements: set[str],
) -> list[str]:
    """Return the existing type and resource errors for one valid card mapping."""
    errors: list[str] = []
    card_type = card.get("type")
    if not isinstance(card_type, str) or not card_type:
        errors.append(f"{card_location}: card type must be a non-empty string")
        return errors
    if card_type.startswith("custom:"):
        custom_name = card_type.removeprefix("custom:")
        if (
            custom_name not in defined_custom_elements
            and custom_name not in resource_names
            and not any(custom_name in name for name in resource_names)
        ):
            errors.append(
                f"{card_location}: {card_type!r} "
                "has no matching Lovelace resource "
                "or customElements.define registration"
            )
    return errors


def _validate_card_types(
    cards: object,
    location: str,
    resource_names: set[str],
    defined_custom_elements: set[str],
) -> list[str]:
    """Validate card types and their existing custom-element references."""
    errors: list[str] = []
    for card_location, card, err in iter_cards(cards, location):
        if err:
            errors.append(f"{card_location}: {err}")
            continue
        if card is None:
            raise AssertionError
        errors.extend(
            _card_type_errors(
                card, card_location, resource_names, defined_custom_elements
            )
        )
    return errors


def _validate_view(
    view: dict,
    location: str,
    resource_names: set[str],
    defined_custom_elements: set[str],
) -> list[str]:
    """Validate one view before checking its card resource references."""
    errors: list[str] = []
    title = view.get("title")
    if title is not None and not isinstance(title, str):
        errors.append(f"{location}: title must be a string")

    cards = view.get("cards")
    sections = view.get("sections")
    view_type = view.get("type")
    if view_type == "sections":
        if cards is not None:
            errors.append(f"{location}: sections view must not also define cards")
        errors.extend(_validate_sections(sections, location))
    else:
        if sections is not None:
            errors.append(f"{location}: sections requires type: sections")
        if cards is None:
            errors.append(f"{location}: non-sections view requires cards")
    if view.get("panel") is True and isinstance(cards, list) and len(cards) != 1:
        errors.append(f"{location}: panel view must contain exactly one card")
    errors.extend(
        _validate_card_types(cards, location, resource_names, defined_custom_elements)
    )
    return errors


def validate_dashboard(
    path: Path,
    resource_names: set[str],
    defined_custom_elements: set[str],
) -> list[str]:
    """Validate dashboard structure and its custom-card resource references."""
    errors: list[str] = []
    if not path.exists():
        return [f"{display_path(path)}: dashboard file does not exist"]
    try:
        data = load_yaml(path)
    except Exception as err:  # noqa: BLE001 - report parser exception text.
        return [f"{display_path(path)}: YAML parse error: {type(err).__name__}: {err}"]
    if not isinstance(data, dict):
        return [f"{display_path(path)}: dashboard root must be a mapping"]
    views = data.get("views")
    if not isinstance(views, list) or not views:
        return [f"{display_path(path)}: views must be a non-empty list"]
    for view_index, view in enumerate(views):
        location = f"{display_path(path)}.views[{view_index}]"
        if not isinstance(view, dict):
            errors.append(f"{location}: view must be a mapping")
            continue
        errors.extend(
            _validate_view(view, location, resource_names, defined_custom_elements)
        )
    return errors


def target_dashboards(config: dict, args: list[str]) -> set[Path]:
    """Select configured dashboards and explicitly requested YAML files."""
    dashboards = (config.get("lovelace") or {}).get("dashboards") or {}
    files: set[Path] = set()

    if (not args or "configuration.yaml" in args) and isinstance(dashboards, dict):
        for item in dashboards.values():
            if isinstance(item, dict) and isinstance(item.get("filename"), str):
                files.add(repo_path(item["filename"]))

    for arg in args:
        path = Path(arg)
        if path.name.startswith("ui-") and path.suffix in {".yaml", ".yml"}:
            files.add(repo_path(path))

    return files


def main(argv: list[str]) -> int:
    """Print validation results and return a failing status for any errors."""
    args = [arg.removeprefix("./") for arg in argv]
    try:
        config = load_yaml(CONFIG_PATH)
    except Exception as err:  # noqa: BLE001 - report parser exception text.
        print(
            f"configuration.yaml: YAML parse error: {type(err).__name__}: {err}",
            file=sys.stderr,
        )
        return 1

    if not isinstance(config, dict):
        print("configuration.yaml: root must be a mapping", file=sys.stderr)
        return 1

    _resources, resource_names, defined_custom_elements, errors = collect_resources(
        config
    )
    for dashboard_path in sorted(target_dashboards(config, args)):
        errors.extend(
            validate_dashboard(dashboard_path, resource_names, defined_custom_elements)
        )

    if errors:
        print("Lovelace validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    targets = (
        ", ".join(
            sorted(display_path(path) for path in target_dashboards(config, args))
        )
        or "resources only"
    )
    print(f"OK: Lovelace validation ({targets})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
