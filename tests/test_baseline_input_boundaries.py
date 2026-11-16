"""Native regressions for HTTP, XML, and registry backup boundaries."""

from __future__ import annotations

import io
import json
import ssl
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock
from xml.etree.ElementTree import ParseError

import pytest

from custom_components import rutracker_transmission as rutracker
from scripts import dataplicity_watchdog, ha_health_monitor, transmission_errors

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

CLIENTS = (rutracker, dataplicity_watchdog, transmission_errors)
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Fixture series</title>
    <link href="https://example.invalid/forum/viewtopic.php?t=101"/>
    <id>https://example.invalid/forum/viewtopic.php?t=101</id>
    <category label="Fixture forum"/>
    <updated>2026-11-15T12:00:00Z</updated>
  </entry>
</feed>"""


def _response(
    url: str,
    status: int = 200,
    body: bytes = b"ok",
    headers: dict[str, str] | None = None,
) -> urllib.response.addinfourl:
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    response = urllib.response.addinfourl(io.BytesIO(body), message, url, status)
    response.msg = "fixture"
    return response


@pytest.fixture(params=CLIENTS, ids=("rutracker", "dataplicity", "transmission"))
def client_module(request) -> ModuleType:
    return request.param


def test_only_http_handlers_preserve_proxy_and_tls(client_module, monkeypatch) -> None:
    monkeypatch.setenv("http_proxy", "http://proxy.example.invalid:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.example.invalid:3128")
    opener = client_module._http_opener()
    handler_types = {type(handler) for handler in opener.handlers}
    assert urllib.request.ProxyHandler in handler_types
    assert urllib.request.HTTPHandler in handler_types
    assert urllib.request.HTTPSHandler in handler_types
    assert urllib.request.HTTPRedirectHandler in handler_types
    assert urllib.request.HTTPErrorProcessor in handler_types
    assert not handler_types.intersection(
        {
            urllib.request.FileHandler,
            urllib.request.FTPHandler,
            urllib.request.DataHandler,
        },
    )
    proxy = next(
        h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)
    )
    assert proxy.proxies["http"] == "http://proxy.example.invalid:3128"
    assert proxy.proxies["https"] == "http://proxy.example.invalid:3128"
    https = next(
        h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)
    )
    if https._context is not None:
        assert https._context.verify_mode == ssl.CERT_REQUIRED
        assert https._context.check_hostname


def test_unsupported_scheme_never_dispatches(client_module, monkeypatch) -> None:
    transport = Mock(side_effect=AssertionError("Unexpected network dispatch"))
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", transport)
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", transport)
    with pytest.raises(urllib.error.URLError, match="Only HTTP and HTTPS"):
        client_module._http_opener().open(
            "unsupported://example.invalid/item", timeout=1
        )
    transport.assert_not_called()


@pytest.mark.parametrize(
    "destination", ["http://example.invalid/done", "https://example.invalid/done"]
)
def test_http_redirect_retains_standard_post_conversion(
    client_module,
    monkeypatch,
    destination: str,
) -> None:
    observed = []

    def transport(_handler, request) -> urllib.response.addinfourl:
        observed.append(
            (
                request.full_url,
                request.get_method(),
                request.data,
                request.timeout,
                request.get_header("X-fixture"),
            )
        )
        if request.full_url.endswith("/start"):
            return _response(request.full_url, 302, headers={"Location": destination})
        return _response(request.full_url, body=b"completed")

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", transport)
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", transport)
    request = urllib.request.Request(
        "http://example.invalid/start",
        data=b"payload",
        headers={"X-Fixture": "kept"},
    )
    with client_module._http_opener().open(request, timeout=7.5) as response:
        assert response.read() == b"completed"
    assert observed == [
        ("http://example.invalid/start", "POST", b"payload", 7.5, "kept"),
        (destination, "GET", None, 7.5, "kept"),
    ]


def test_redirect_to_other_protocol_cannot_dispatch(client_module, monkeypatch) -> None:
    observed = []

    def transport(_handler, request) -> urllib.response.addinfourl:
        observed.append(request.full_url)
        return _response(
            request.full_url, 302, headers={"Location": "ftp://example.invalid/item"}
        )

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", transport)
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", transport)
    with pytest.raises(urllib.error.URLError):
        client_module._http_opener().open("https://example.invalid/start", timeout=1)
    assert observed == ["https://example.invalid/start"]


def test_http_failures_remain_http_errors(client_module, monkeypatch) -> None:
    def transport(_handler, request) -> urllib.response.addinfourl:
        return _response(request.full_url, 503)

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", transport)
    with pytest.raises(urllib.error.HTTPError) as caught:
        client_module._http_opener().open("http://example.invalid/item", timeout=3)
    assert caught.value.code == 503


def test_transmission_session_retry_keeps_json_and_deadline(monkeypatch) -> None:
    observed = []

    def transport(_handler, request) -> urllib.response.addinfourl:
        observed.append(
            (
                request.get_method(),
                request.timeout,
                request.get_header("X-transmission-session-id"),
                json.loads(request.data),
            )
        )
        if len(observed) == 1:
            return _response(
                request.full_url,
                409,
                headers={"X-Transmission-Session-Id": "fixture-session"},
            )
        return _response(request.full_url, body=b'{"result":"success","arguments":{}}')

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", transport)
    monkeypatch.setattr(transmission_errors, "RPC_URL", "http://example.invalid/rpc")
    response = transmission_errors.rpc("torrent-get", {"fields": ["id"]})
    assert response["result"] == "success"
    assert len(observed) == 2
    assert observed[0][:3] == ("POST", 8, None)
    assert observed[1][:3] == ("POST", 8, "fixture-session")
    assert (
        observed[0][3]
        == observed[1][3]
        == {"method": "torrent-get", "arguments": {"fields": ["id"]}}
    )


def test_rutracker_byte_and_text_clients_keep_headers_and_timeouts(monkeypatch) -> None:
    observed = []

    def transport(_handler, request) -> urllib.response.addinfourl:
        observed.append((request.timeout, request.get_header("User-agent")))
        return _response(
            request.full_url,
            body=b"fixture",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", transport)
    assert (
        rutracker._http_get_bytes("http://example.invalid/item", timeout=4)
        == b"fixture"
    )
    assert (
        rutracker._http_get_text_urllib({}, "http://example.invalid/item") == "fixture"
    )
    assert observed == [(4, "Transmission/4.1.1"), (25, rutracker.FALLBACK_USER_AGENT)]


def test_watchdog_probe_and_service_keep_contract(monkeypatch) -> None:
    observed = []

    def transport(_handler, request) -> urllib.response.addinfourl:
        observed.append(
            (
                request.full_url,
                request.timeout,
                request.get_method(),
                request.data,
                request.get_header("Authorization"),
            )
        )
        return _response(request.full_url, body=b"Home Assistant")

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", transport)
    monkeypatch.setattr(dataplicity_watchdog, "HA", "http://example.invalid")
    monkeypatch.setattr(dataplicity_watchdog, "ha_token", lambda: "fixture")
    ok, code, elapsed = dataplicity_watchdog.probe("http://example.invalid")
    dataplicity_watchdog.ha("demo/action", {"enabled": True})
    assert ok
    assert code == "200"
    assert elapsed >= 0
    assert observed[0] == (
        "http://example.invalid/manifest.json",
        20,
        "GET",
        None,
        None,
    )
    assert observed[1][:3] == (
        "http://example.invalid/api/services/demo/action",
        30,
        "POST",
    )
    assert json.loads(observed[1][3]) == {"enabled": True}
    assert observed[1][4] == "Bearer fixture"


async def test_atom_feed_preserves_normal_candidate_fields(monkeypatch) -> None:
    monkeypatch.setattr(rutracker, "_http_get_text", AsyncMock(return_value=ATOM))
    result = await rutracker._search_feed(
        Mock(),
        {rutracker.CONF_FEED_URL: "https://example.invalid/{forum_id}"},
        "Fixture",
        ["fixture"],
        42,
    )
    assert len(result) == 1
    item = result[0]
    assert (item.topic_id, item.title, item.source, item.forum_id) == (
        "101",
        "Fixture series",
        "feed",
        42,
    )
    assert item.updated == "2026-11-15T12:00:00Z"


async def test_malformed_atom_remains_a_parse_error(monkeypatch) -> None:
    monkeypatch.setattr(rutracker, "_http_get_text", AsyncMock(return_value="<feed"))
    with pytest.raises(ParseError):
        await rutracker._search_feed(
            Mock(),
            {rutracker.CONF_FEED_URL: "https://example.invalid/{forum_id}"},
            "Fixture",
            ["fixture"],
            42,
        )


async def test_malformed_feed_degrades_to_notes_and_preserves_selection(
    hass, monkeypatch
) -> None:
    hass.data[rutracker.DOMAIN] = {"selected_result_id": "keep", "last_response": {}}
    monkeypatch.setattr(rutracker, "_http_get_text", AsyncMock(return_value="<feed"))
    context = rutracker.SearchContext(
        hass=hass,
        session=Mock(),
        cfg={rutracker.CONF_FEED_URL: "https://example.invalid/{forum_id}"},
        query="Fixture",
        tokens=["fixture"],
        forums=[42],
        max_results=8,
        cookie_header="",
        notes=[],
        candidates=[],
        seen=set(),
    )
    searched_feed = await rutracker._collect_search_candidates(context)
    assert searched_feed
    assert context.candidates == []
    assert any(note.startswith("feed forum 42:") for note in context.notes)
    assert hass.data[rutracker.DOMAIN]["last_response"]["selected_result_id"] == "keep"


def test_registry_backup_is_complete_and_private(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ha_health_monitor.tempfile, "tempdir", str(tmp_path))
    source = tmp_path / "registry.json"
    source.write_bytes(b'{"data":{"entities":[]}}')
    backup = ha_health_monitor._backup_registry(source)
    assert backup != source
    assert backup.read_bytes() == source.read_bytes()
    assert backup.stat().st_mode & 0o777 == 0o600


def test_incomplete_registry_backup_does_not_replace_source(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ha_health_monitor.tempfile, "tempdir", str(tmp_path))
    source = tmp_path / "registry.json"
    source.write_bytes(b"original")
    monkeypatch.setattr(
        ha_health_monitor.shutil,
        "copyfileobj",
        Mock(side_effect=OSError("fixture copy failure")),
    )
    with pytest.raises(OSError, match="fixture copy failure"):
        ha_health_monitor._backup_registry(source)
    assert source.read_bytes() == b"original"
    assert list(tmp_path.glob("core.entity_registry.backup.*")) == []


def test_registry_rewrite_requires_successful_backup(monkeypatch) -> None:
    monitor = object.__new__(ha_health_monitor.HAMonitor)
    registry = {
        "data": {"entities": [{"entity_id": "tts.fixture", "config_entry_id": None}]}
    }
    monkeypatch.setattr(monitor, "get_entity_registry", lambda: registry)
    monkeypatch.setattr(
        ha_health_monitor,
        "_backup_registry",
        Mock(side_effect=OSError("fixture backup failure")),
    )
    writer = Mock(side_effect=AssertionError("Registry rewrite reached"))
    monkeypatch.setattr(ha_health_monitor.Path, "open", writer)
    with pytest.raises(OSError, match="fixture backup failure"):
        monitor.fix_orphaned_entity("tts.fixture")
    writer.assert_not_called()
    assert registry["data"]["entities"] == [
        {"entity_id": "tts.fixture", "config_entry_id": None}
    ]
