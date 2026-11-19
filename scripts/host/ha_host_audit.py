"""Private bounded audit records for the standalone host bridge."""
# Installed beside the standalone executable; the copied bundle is not a package.

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TextIO

_AUDIT = logging.getLogger("ha-host-actions.audit")
_AUDIT.addHandler(logging.NullHandler())


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self) -> TextIO:
        # Initial and rotated files are private without altering the daemon's
        # umask or the permissions of files created by unrelated host routes.
        fd = os.open(
            self.baseFilename,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)


def _start_audit() -> tuple[Path, float]:
    # A private unique run directory avoids following somebody else's /tmp
    # symlinks. Rotation caps this daemon's disk use at three 256 KiB files.
    directory = Path(
        tempfile.mkdtemp(prefix=f"ha-host-actions-{os.getpid()}-", dir="/tmp")
    )
    directory.chmod(0o700)
    path = directory / "ha-host-actions.jsonl"
    with path.open("x", encoding="utf-8"):
        pass
    path.chmod(0o600)
    file_handler = _PrivateRotatingFileHandler(
        path, maxBytes=256 * 1024, backupCount=2, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    terminal_handler = logging.StreamHandler(sys.stderr)
    terminal_handler.setFormatter(logging.Formatter("%(message)s"))
    _AUDIT.handlers = [file_handler, terminal_handler]
    _AUDIT.setLevel(logging.INFO)
    _AUDIT.propagate = False
    started = time.monotonic()
    _audit("startup", outcome="started", log=str(path))
    return path, started


def _audit(stage: str, **metadata: Any) -> None:
    # Callers supply only fixed labels, status codes, durations and the log path.
    # Never include a request body/query, text/character, token, heading or an
    # exception message (which could contain user-provided data).
    record = {"at": datetime.now().astimezone().isoformat(), "stage": stage, **metadata}
    _AUDIT.info(json.dumps(record, ensure_ascii=True, sort_keys=True))
