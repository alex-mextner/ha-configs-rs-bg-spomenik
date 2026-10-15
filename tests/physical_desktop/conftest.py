"""Permit the explicit local transport tests under HA's global socket guard."""

import pytest


@pytest.fixture(autouse=True)
def local_transport_sockets(request):
    if request.config.pluginmanager.hasplugin("homeassistant"):
        request.getfixturevalue("socket_enabled")
