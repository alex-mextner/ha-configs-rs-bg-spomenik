#!/usr/bin/env python3
"""Attempt one measured clean-page reclaim immediately before ODROID Kodi starts."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LOGGER = logging.getLogger("ha-kodi-prestart")
MODEL = Path("/proc/device-tree/model")
DRM = Path("/sys/class/drm")
MEMINFO = Path("/proc/meminfo")
DROP_CACHES = Path("/proc/sys/vm/drop_caches")
SUPPORTED_BOARDS = {"Hardkernel ODROID-N2", "Hardkernel ODROID-N2Plus"}
# The observed GUI startup allocated four 2089-page CMA buffers (~33 MiB).
# Keep the same 96 MiB admission threshold used by the successful observation.
MIN_CMA_KIB = 96 * 1024
HDMI_TIMEOUT = 40.0
POLL_INTERVAL = 0.25
SYNC_TIMEOUT = 10
MEMINFO_VALUE_FIELDS = 2
MEMORY_KEYS = {"CmaTotal", "CmaFree", "MemAvailable", "MemFree", "Cached"}


def record(stage: str, **values: object) -> None:
    """Write safe structured diagnostics to the terminal and private run log."""
    LOGGER.info(json.dumps({"stage": stage, **values}, sort_keys=True))


def read_model() -> str:
    """Read the device-tree identity without trusting user configuration."""
    return MODEL.read_text().rstrip("\x00\n")


def read_memory() -> dict[str, int]:
    """Require a valid CMA observation before considering any cache mutation."""
    values = {}
    for line in MEMINFO.read_text().splitlines():
        name, separator, raw = line.partition(":")
        if separator and name in MEMORY_KEYS:
            fields = raw.split()
            if len(fields) != MEMINFO_VALUE_FIELDS or fields[1] != "kB":
                error_message = "invalid_cma_observation"
                raise ValueError(error_message)
            values[name] = int(fields[0])
    total, free = values.get("CmaTotal", 0), values.get("CmaFree", -1)
    if total <= 0 or not 0 <= free <= total:
        error_message = "invalid_cma_observation"
        raise ValueError(error_message)
    return values


def hdmi_connected() -> bool:
    """Use the same HDMI connector status criterion as the existing session."""
    return any(
        status.read_text().strip() == "connected"
        for status in DRM.glob("card*-HDMI-A-*/status")
    )


def wait_hdmi() -> bool:
    """Wait only for an already requested display, without powering anything."""
    deadline = time.monotonic() + HDMI_TIMEOUT
    while not hdmi_connected():
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)
    return True


def drop_clean_pages() -> None:
    """Drop only clean page-cache entries; never discard dirty data or slab."""
    DROP_CACHES.write_text("1\n")


def prepare() -> str:
    """Perform at most one recovery for this normal service start."""
    if os.geteuid() != 0:
        error_message = "root_unit_context_required"
        raise PermissionError(error_message)
    if read_model() not in SUPPORTED_BOARDS:
        return "unsupported_board"
    record("waiting_for_hdmi", timeout_seconds=HDMI_TIMEOUT)
    if not wait_hdmi():
        return "hdmi_not_ready"
    before = read_memory()
    record("hdmi_ready", memory_kib=before)
    if before["CmaFree"] >= MIN_CMA_KIB:
        return "reclaim_unneeded"
    started = time.monotonic()
    # Fixed executable from the unit's PATH (NixOS has no /usr/bin/sync), no shell or caller-provided arguments.
    subprocess.run(["sync"], check=True, timeout=SYNC_TIMEOUT)
    if not hdmi_connected():
        return "hdmi_disconnected"
    if read_memory()["CmaFree"] >= MIN_CMA_KIB:
        return "reclaim_unneeded_after_sync"
    drop_clean_pages()
    record(
        "one_clean_cache_reclaim",
        elapsed_seconds=round(time.monotonic() - started, 3),
        before_memory_kib=before,
        after_memory_kib=read_memory(),
    )
    return "reclaim_attempted"


def main() -> int:
    """Keep each invocation private, logged, bounded by the systemd command."""
    os.umask(0o077)
    fd, path = tempfile.mkstemp(prefix="ha-kodi-prestart-", suffix=".log", dir="/tmp")
    with os.fdopen(fd, "w") as log:
        handlers = [logging.StreamHandler(sys.stderr), logging.StreamHandler(log)]
        formatter = logging.Formatter("%(asctime)s %(message)s")
        for handler in handlers:
            handler.setFormatter(formatter)
            LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
        started = time.monotonic()
        record("start", log=path, pid=os.getpid())
        try:
            outcome = prepare()
            record(
                "complete",
                outcome=outcome,
                elapsed_seconds=round(time.monotonic() - started, 3),
                log=path,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            record(
                "failed",
                error_type=type(error).__name__,
                elapsed_seconds=round(time.monotonic() - started, 3),
                log=path,
            )
            return 1
        finally:
            for handler in handlers:
                LOGGER.removeHandler(handler)
                handler.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
