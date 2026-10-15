"""Exercise the wrapper and API client at their process/network boundaries."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

WRAPPER = Path(__file__).resolve().parents[2] / "scripts/apply-fork-update.sh"


def test_wrapper_passes_exact_entity_and_release_to_public_api_cli(tmp_path):
    recorder = tmp_path / "python-recorder"
    recorder.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    recorder.chmod(0o755)
    result = subprocess.run(
        ["bash", str(WRAPPER), "update.demo_update", "--version", "v2.0.0"],
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "HA_PYTHON": str(recorder)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        str(WRAPPER.with_name("ha_updates.py")),
        "install",
        "--entity",
        "update.demo_update",
        "--version",
        "v2.0.0",
    ]


@pytest.mark.asyncio
async def test_cli_calls_public_install_with_exact_release():
    spec = importlib.util.spec_from_file_location(
        "ha_updates_test", WRAPPER.with_name("ha_updates.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    response = MagicMock(status=200)
    response.text = AsyncMock(return_value="[]")
    client = MagicMock()
    client.post.return_value.__aenter__.return_value = response
    session = MagicMock()
    session.__aenter__.return_value = client
    args = argparse.Namespace(
        action="install",
        entity="update.demo_update",
        version="v2.0.0",
        url="http://ha.invalid",
        env_file=Path("/does-not-exist"),
    )
    with (
        patch.dict(os.environ, {"HA_TOKEN": "test-token"}),
        patch.object(module.aiohttp, "ClientSession", return_value=session),
    ):
        result = await module.run(args)
    assert result == {"accepted": True, "entity_id": "update.demo_update"}
    client.post.assert_called_once_with(
        "http://ha.invalid/api/services/update/install",
        json={"entity_id": "update.demo_update", "version": "v2.0.0"},
        headers={"Authorization": "Bearer test-token"},
    )


def test_wrapper_refuses_obsolete_submodule_fork_branch_arguments():
    result = subprocess.run(
        ["bash", str(WRAPPER), "YandexStation", "owner/fork", "master"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "update.* entity" in result.stderr
