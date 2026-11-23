# Copyright (c) 2026 Alex Mextner
"""Pinned MA fallback preserves idle state and restores volume without devices."""

import asyncio
import logging
import types
import unittest
from unittest.mock import AsyncMock, patch

from music_assistant.controllers.players.announcements import AnnouncementsMixin
from music_assistant_models.enums import PlaybackState


class NativeAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    """Exercise production announcement orchestration with device calls isolated."""

    async def test_already_interrupted_idle_player_is_not_resumed_by_announcement(
        self,
    ) -> None:
        state = types.SimpleNamespace(
            playback_state=PlaybackState.IDLE,
            power_control="none",
            powered=True,
            synced_to=None,
            active_group=None,
            active_source="original-queue",
            current_media=None,
            name="fixture",
        )
        player = types.SimpleNamespace(
            player_id="studio", state=state, current_media=None
        )
        controller = types.SimpleNamespace(
            logger=logging.getLogger("announcement_fixture"),
            _prepare_for_announcement=AsyncMock(),
            _handle_play_media=AsyncMock(),
            _wait_for_playback_state=AsyncMock(),
            _restore_after_announcement=AsyncMock(),
        )
        media = types.SimpleNamespace(
            uri="http://127.0.0.1/local/cached.ogg", custom_data=None, duration=1
        )
        await AnnouncementsMixin._play_announcement(controller, player, media, 40)
        kwargs = controller._restore_after_announcement.call_args.kwargs
        self.assertFalse(kwargs["restore_playback"])
        self.assertEqual(kwargs["prev_source"], "original-queue")
        controller._handle_play_media.assert_awaited_once_with("studio", media)

    async def test_restore_volume_does_not_start_a_queue_when_restore_playback_is_false(
        self,
    ) -> None:
        player = types.SimpleNamespace(
            player_id="studio", state=types.SimpleNamespace(name="fixture")
        )
        controller = types.SimpleNamespace(
            logger=logging.getLogger("announcement_fixture"),
            mass=types.SimpleNamespace(create_task=asyncio.create_task),
            _handle_cmd_volume_set=AsyncMock(),
            _handle_cmd_resume=AsyncMock(),
            _handle_cmd_power=AsyncMock(),
        )
        with patch(
            "music_assistant.controllers.players.announcements.asyncio.sleep",
            AsyncMock(),
        ):
            await AnnouncementsMixin._restore_after_announcement(
                controller,
                player,
                prev_power=True,
                prev_volumes={"studio": 89},
                prev_muted=set(),
                prev_synced_to=None,
                prev_group=None,
                prev_source="original-queue",
                prev_media=None,
                restore_playback=False,
            )
        controller._handle_cmd_volume_set.assert_awaited_once_with("studio", 89)
        controller._handle_cmd_resume.assert_not_awaited()
        controller._handle_cmd_power.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
