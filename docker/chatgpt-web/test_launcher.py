"""No real browser, credentials, or website calls."""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest


def load_launcher():
    spec = importlib.util.spec_from_file_location('chatgpt_launcher', Path(__file__).with_name('launcher.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_is_default_and_needs_no_shared_library(monkeypatch):
    bridge = SimpleNamespace(main=AsyncMock())
    monkeypatch.setitem(sys.modules, 'bridge', bridge)
    monkeypatch.delenv('CHATGPT_WEB_BROWSER_MODE', raising=False)
    load_launcher().main()
    bridge.main.assert_awaited_once_with()


@pytest.mark.parametrize('mode', ['manual', 'managed'])
def test_explicit_mode_uses_shared_adapter(monkeypatch, mode):
    bridge = SimpleNamespace(main=AsyncMock())
    run = AsyncMock()
    monkeypatch.setitem(sys.modules, 'bridge', bridge)
    monkeypatch.setitem(sys.modules, 'agenttools_chatgpt.ha_bridge', SimpleNamespace(run=run))
    monkeypatch.setenv('CHATGPT_WEB_BROWSER_MODE', mode)
    load_launcher().main()
    run.assert_awaited_once_with(bridge)
    bridge.main.assert_not_called()


def test_invalid_mode_is_rejected_not_silently_downgraded(monkeypatch):
    monkeypatch.setitem(sys.modules, 'bridge', SimpleNamespace(main=AsyncMock()))
    monkeypatch.setenv('CHATGPT_WEB_BROWSER_MODE', 'stealth')
    with pytest.raises(SystemExit):
        load_launcher().main()
