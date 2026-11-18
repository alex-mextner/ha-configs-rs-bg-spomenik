"""Exercise the imported upload security boundaries and publication race."""

import asyncio
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from PIL import Image

from custom_components import plant_care_alerts as integration
from custom_components.plant_care_alerts import photo

UPLOAD_ID = "a" * 32


class PhotoFilesystemTests(unittest.TestCase):
    """Reject unsafe inputs and leave no partial managed output."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.config.mkdir()
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=str(self.config)))
        self.source = self.root / "upload.png"
        Image.new("RGB", (2, 2)).save(self.source, format="PNG")

    def test_symlinked_managed_directories_are_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        for number, parts in enumerate(
            [("www",), ("www", "plant_care"), ("www", "plant_care", "managed")]
        ):
            with self.subTest(parts=parts):
                config = self.root / f"config-{number}"
                config.mkdir()
                parent = config
                for part in parts[:-1]:
                    parent /= part
                    parent.mkdir()
                (parent / parts[-1]).symlink_to(outside, target_is_directory=True)
                hass = SimpleNamespace(config=SimpleNamespace(config_dir=str(config)))
                with (
                    patch.object(
                        photo,
                        "process_uploaded_file",
                        return_value=nullcontext(self.source),
                    ),
                    self.assertRaisesRegex(ValueError, "Unsafe photo directory"),
                ):
                    photo.persist_photo(hass, UPLOAD_ID)
                self.assertEqual(list(outside.iterdir()), [])

    def test_non_png_or_jpeg_upload_cannot_create_a_managed_image(self) -> None:
        source = self.root / "upload.gif"
        Image.new("RGB", (2, 2)).save(source, format="GIF")
        with (
            patch.object(
                photo, "process_uploaded_file", return_value=nullcontext(source)
            ),
            self.assertRaisesRegex(ValueError, "Only PNG and JPEG"),
        ):
            photo.persist_photo(self.hass, UPLOAD_ID)
        self.assertFalse((self.config / "www").exists())

    def test_oversize_processed_output_is_removed_before_error_returns(self) -> None:
        with (
            patch.object(
                photo, "process_uploaded_file", return_value=nullcontext(self.source)
            ),
            patch.object(photo, "_MAX_OUTPUT_BYTES", 1),
            self.assertRaisesRegex(ValueError, "Processed image is too large"),
        ):
            photo.persist_photo(self.hass, UPLOAD_ID)
        managed = self.config / "www" / "plant_care" / "managed"
        self.assertTrue(managed.is_dir())
        self.assertEqual(list(managed.iterdir()), [])


class PhotoServiceTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the registered callback without storage or device side effects."""

    async def asyncSetUp(self) -> None:
        self.entry = SimpleNamespace(
            data={}, options={"image_path": "/local/original.jpg", "enable_image": True}
        )
        self.plant = object()
        self.hass = SimpleNamespace(
            data={"adaptive_plant": {"plant-entry": self.plant}},
            async_add_executor_job=AsyncMock(),
            config_entries=SimpleNamespace(async_update_entry=Mock()),
        )
        self.manager = SimpleNamespace(
            details={"plant-entry": {"picture": "/local/original.jpg"}}, entities={}
        )
        with patch.object(integration, "async_register_admin_service") as register:
            integration._register_photo_service(self.hass, self.manager)
        self.assertEqual(
            register.call_args.args[:3], (self.hass, integration.DOMAIN, "set_photo")
        )
        self.callback = register.call_args.args[3]

    async def test_call_without_user_id_is_denied_before_processing(self) -> None:
        call = SimpleNamespace(
            context=Context(), data={"entry_id": "plant-entry", "file_id": UPLOAD_ID}
        )
        with self.assertRaises(Unauthorized):
            await self.callback(call)
        self.hass.async_add_executor_job.assert_not_called()
        self.hass.config_entries.async_update_entry.assert_not_called()

    async def test_photo_changed_while_upload_is_processing_is_preserved(self) -> None:
        started, release = asyncio.Event(), asyncio.Event()

        async def persist(*_args: object) -> str:
            started.set()
            await release.wait()
            return "/local/plant_care/managed/new-upload.jpg"

        self.hass.async_add_executor_job.side_effect = persist
        call = SimpleNamespace(
            context=Context(user_id="administrator"),
            data={"entry_id": "plant-entry", "file_id": UPLOAD_ID},
        )
        with patch.object(
            integration, "async_get_config_entry", return_value=self.entry
        ):
            task = asyncio.create_task(self.callback(call))
            try:
                await asyncio.wait_for(started.wait(), 5)
                self.entry.options["image_path"] = "/local/new-user-choice.jpg"
                release.set()
                with self.assertRaisesRegex(HomeAssistantError, "Plant photo changed"):
                    await asyncio.wait_for(task, 5)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.entry.options["image_path"], "/local/new-user-choice.jpg")
        self.hass.config_entries.async_update_entry.assert_not_called()
        self.assertEqual(
            self.manager.details["plant-entry"]["picture"], "/local/original.jpg"
        )
