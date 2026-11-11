# Copyright (c) 2026 Alex Ultra
# SPDX-License-Identifier: MIT
"""Persist uploaded plant photos without modifying existing user images."""

from __future__ import annotations

import os
from pathlib import Path
import re
import uuid
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
from homeassistant.components.file_upload import process_uploaded_file

_MAX_BYTES = 20 * 1024 * 1024
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_OUTPUT_DIMENSION = 1024
_MAX_DIMENSION = 10000
_MAX_PIXELS = 40_000_000
_UPLOAD_ID = re.compile(r"[0-9a-f]{32}\Z")


def persist_photo(hass, file_id: str) -> str:
    """Consume a HA upload, validate it and create a new exclusive image file."""
    if not _UPLOAD_ID.fullmatch(file_id):
        raise ValueError("Invalid upload ID")
    with process_uploaded_file(hass, file_id) as source:
        if source.stat().st_size > _MAX_BYTES:
            raise ValueError("Image is too large")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(source) as original:
                    if original.format not in {"PNG", "JPEG"}:
                        raise ValueError("Only PNG and JPEG images are supported")
                    width, height = original.size
                    if (width < 1 or height < 1 or width > _MAX_DIMENSION
                            or height > _MAX_DIMENSION or width * height > _MAX_PIXELS):
                        raise ValueError("Image dimensions are too large")
                    image = ImageOps.exif_transpose(original)
                    alpha = "A" in image.getbands() or "transparency" in image.info
                    image = image.convert("RGBA" if alpha else "RGB")
                    image.thumbnail(
                        (_MAX_OUTPUT_DIMENSION, _MAX_OUTPUT_DIMENSION),
                        Image.Resampling.LANCZOS,
                    )
        except (UnidentifiedImageError, Image.DecompressionBombError,
                Image.DecompressionBombWarning, OSError) as err:
            raise ValueError("Invalid image") from err

        # Open each directory without following symlinks, so another process cannot
        # redirect writes outside the managed directory during directory creation.
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(Path(hass.config.config_dir), flags)
        try:
            for component in ("www", "plant_care", "managed"):
                try:
                    os.mkdir(component, dir_fd=fd)
                except FileExistsError:
                    pass
                try:
                    next_fd = os.open(component, flags, dir_fd=fd)
                except OSError as err:
                    raise ValueError("Unsafe photo directory") from err
                os.close(fd)
                fd = next_fd
            extension = "png" if alpha else "jpg"
            name = f"{uuid.uuid4().hex}.{extension}"
            output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o644, dir_fd=fd)
            try:
                with os.fdopen(output, "wb") as target:
                    image.save(target, format="PNG" if alpha else "JPEG")
                    if target.tell() > _MAX_OUTPUT_BYTES:
                        raise ValueError("Processed image is too large")
            except BaseException:
                os.unlink(name, dir_fd=fd)
                raise
        finally:
            os.close(fd)
    return f"/local/plant_care/managed/{name}"
