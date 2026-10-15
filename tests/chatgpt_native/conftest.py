"""Local-only network access for real bridge regression tests."""

import pytest


@pytest.fixture(autouse=True)
def local_transport_sockets(request):
    if request.config.pluginmanager.hasplugin("homeassistant"):
        request.getfixturevalue("socket_enabled")
        import pytest_socket

        pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
