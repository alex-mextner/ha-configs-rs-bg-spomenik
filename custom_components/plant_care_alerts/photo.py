# Copyright (c) 2026 Alex Ultra
# SPDX-License-Identifier: MIT
"""Persist uploaded plant photos without modifying existing user images."""

from __future__ import annotations

import os
import re
import uuid
import warnings
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.components.file_upload import process_uploaded_file
from PIL import Image, ImageOps, UnidentifiedImageError

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_MAX_BYTES = 20 * 1024 * 1024
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_OUTPUT_DIMENSION = 1024
_MAX_DIMENSION = 10000
_MAX_PIXELS = 40_000_000
_UPLOAD_ID = re.compile(r"[0-9a-f]{32}\Z")


def _check_output_size(size: int) -> None:
    """Reject oversize output inside the caller's exclusive-file cleanup scope."""
    if size > _MAX_OUTPUT_BYTES:
        message = "Processed image is too large"
        raise ValueError(message)


def _check_upload_id(file_id: str) -> None:
    if not _UPLOAD_ID.fullmatch(file_id):
        message = "Invalid upload ID"
        raise ValueError(message)


def persist_photo(hass: HomeAssistant, file_id: str) -> str:
    """Consume a HA upload, validate it and create a new exclusive image file."""
    _check_upload_id(file_id)
    with process_uploaded_file(hass, file_id) as source:
        if source.stat().st_size > _MAX_BYTES:
            message = "Image is too large"
            raise ValueError(message)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(source) as original:
                    if original.format not in {"PNG", "JPEG"}:
                        message = "Only PNG and JPEG images are supported"
                        raise ValueError(message)
                    width, height = original.size
                    if (
                        width < 1
                        or height < 1
                        or width > _MAX_DIMENSION
                        or height > _MAX_DIMENSION
                        or width * height > _MAX_PIXELS
                    ):
                        message = "Image dimensions are too large"
                        raise ValueError(message)
                    image = ImageOps.exif_transpose(original)
                    alpha = "A" in image.getbands() or "transparency" in image.info
                    image = image.convert("RGBA" if alpha else "RGB")
                    image.thumbnail(
                        (_MAX_OUTPUT_DIMENSION, _MAX_OUTPUT_DIMENSION),
                        Image.Resampling.LANCZOS,
                    )
        except (
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
        ) as err:
            message = "Invalid image"
            raise ValueError(message) from err

        # Open each directory without following symlinks, so another process cannot
        # redirect writes outside the managed directory during directory creation.
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(Path(hass.config.config_dir), flags)
        try:
            for component in ("www", "plant_care", "managed"):
                with suppress(FileExistsError):
                    os.mkdir(component, dir_fd=fd)
                try:
                    next_fd = os.open(component, flags, dir_fd=fd)
                except OSError as err:
                    message = "Unsafe photo directory"
                    raise ValueError(message) from err
                os.close(fd)
                fd = next_fd
            extension = "png" if alpha else "jpg"
            name = f"{uuid.uuid4().hex}.{extension}"
            output = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=fd,
            )
            try:
                with os.fdopen(output, "wb") as target:
                    image.save(target, format="PNG" if alpha else "JPEG")
                    _check_output_size(target.tell())
            except BaseException:
                os.unlink(name, dir_fd=fd)
                raise
        finally:
            os.close(fd)
    return f"/local/plant_care/managed/{name}"
