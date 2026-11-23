# Copyright (c) 2026 Alex Mextner
"""Exact native HA context provenance preserves all service return/error behavior."""

import builtins
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

from hass_client.client import HomeAssistantClient
from music_assistant.helpers import ha_recovery_intent as intent


class NativeHassOriginTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the pinned client's real service method with only its wire mocked."""

    async def test_success_returns_same_object_and_logs_only_safe_exact_identity(
        self,
    ) -> None:
        result = {
            "context": {"id": "context-one", "user_id": "private-user"},
            "secret": "private-result",
        }
        client = types.SimpleNamespace(
            connected=True, send_command=AsyncMock(return_value=result)
        )
        with patch.object(intent.LOGGER, "info") as log:
            actual = await HomeAssistantClient.call_service(
                client,
                "media_player",
                "play_media",
                {"media_content_id": "https://secret/?token=secret"},
                {"entity_id": "media_player.studio"},
            )
        self.assertIs(actual, result)
        self.assertEqual(log.call_count, 1)
        payload = json.loads(log.call_args.args[1])
        self.assertEqual(
            payload,
            {
                "context_id": "context-one",
                "domain": "media_player",
                "service": "play_media",
                "entity_ids": ["media_player.studio"],
            },
        )
        self.assertNotIn("secret", str(log.call_args))
        self.assertNotIn("private", str(log.call_args))

    async def test_service_failure_propagates_unchanged_without_origin(self) -> None:
        error = RuntimeError("fixture-service-error")
        client = types.SimpleNamespace(
            connected=True, send_command=AsyncMock(side_effect=error)
        )
        with (
            patch.object(intent.LOGGER, "info") as log,
            self.assertRaises(RuntimeError) as caught,
        ):
            await HomeAssistantClient.call_service(
                client,
                "media_player",
                "media_stop",
                target={"entity_id": "media_player.studio"},
            )
        self.assertIs(caught.exception, error)
        log.assert_not_called()

    async def test_helper_failure_cannot_change_a_successful_service_result(
        self,
    ) -> None:
        result = {"context": {"id": "one"}}
        client = types.SimpleNamespace(
            connected=True, send_command=AsyncMock(return_value=result)
        )
        with patch.object(
            intent, "log_hass_origin", side_effect=RuntimeError("fixture")
        ):
            self.assertIs(
                await HomeAssistantClient.call_service(
                    client,
                    "media_player",
                    "media_stop",
                    target={"entity_id": "media_player.studio"},
                ),
                result,
            )

    async def test_missing_helper_import_cannot_change_a_successful_result(
        self,
    ) -> None:
        original = builtins.__import__
        result = {"context": {"id": "one"}}
        client = types.SimpleNamespace(
            connected=True, send_command=AsyncMock(return_value=result)
        )

        def missing(name: str, *args: object, **kwargs: object) -> object:
            if name == "music_assistant.helpers.ha_recovery_intent":
                raise ImportError(name)
            return original(name, *args, **kwargs)

        with patch.object(builtins, "__import__", missing):
            self.assertIs(
                await HomeAssistantClient.call_service(
                    client,
                    "media_player",
                    "media_stop",
                    target={"entity_id": "media_player.studio"},
                ),
                result,
            )

    def test_malformed_context_target_and_logger_failure_are_quiet(self) -> None:
        valid = {"context": {"id": "one"}}
        with patch.object(intent.LOGGER, "info") as log:
            for result, target in [
                (None, {}),
                ({"context": {}}, {"entity_id": "media_player.studio"}),
                (valid, {"entity_id": "media_player.studio?secret"}),
                (valid, {"device_id": "device"}),
                (valid, {"entity_id": []}),
            ]:
                intent.log_hass_origin(result, "media_player", "media_stop", target)
            log.assert_not_called()
        with patch.object(intent.LOGGER, "info", side_effect=OSError("fixture")):
            intent.log_hass_origin(
                valid,
                "media_player",
                "media_stop",
                {"entity_id": "media_player.studio"},
            )
