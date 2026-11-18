# Copyright (c) 2026 Alex Ultra
# SPDX-License-Identifier: MIT
"""Keep service registration and ownership realistic in manager-only fixtures."""

from types import SimpleNamespace
from unittest.mock import Mock


def make_service_registry() -> SimpleNamespace:
    """Capture actual callbacks without replacing their authorization logic."""
    registered = {}

    def register(
        domain: str,
        name: str,
        handler: object,
        schema: object | None = None,
        **metadata: object,
    ) -> None:
        registered.setdefault(domain, {})[name] = SimpleNamespace(
            func=handler, schema=schema, **metadata
        )

    def remove(domain: str, name: str) -> None:
        registered.get(domain, {}).pop(name, None)

    return SimpleNamespace(
        async_register=Mock(side_effect=register),
        async_services=lambda: {
            domain: dict(services) for domain, services in registered.items()
        },
        has_service=lambda domain, name: name in registered.get(domain, {}),
        async_remove=Mock(side_effect=remove),
    )
