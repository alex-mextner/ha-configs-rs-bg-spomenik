# Copyright (c) 2026 Alex Mextner
"""Checksum-pinned private intent hook at authenticated HTTP and WS dispatch."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

from apply_guard import fail, replace_once, write_verified

ORIGINALS = {
    "controller.py": "f5d327916a49ba0d31a8c492638b92b9767bfa9b973d173a48f947c99a8c1226",
    "websocket_client.py": (
        "9223d1a4ab03f332e312046fc00d2aab2e15307ab483a8039d50ca65f1359d00"
    ),
}
PATCHED = {
    "controller.py": "6a6ee8d2a4c5e0c5fbb3e80c1006c9053a86c53637a70703c80d8c66231e803c",
    "websocket_client.py": (
        "16d60a56330a8dd36a30abd0d8512e69adb42b7ae49b04047d8e4e96d560a96f"
    ),
}
HELPER_SHA256 = "e7d7567d6427477864e4cfb51f0a5d81a1a1c8482c28ca58d725f5ed91ddfd2e"


def transform(source: bytes, name: str) -> bytes:
    """Insert telemetry after parsed input and before handler side effects."""
    source = replace_once(
        source,
        b"import asyncio\n",
        b"import asyncio\nfrom music_assistant.helpers.ha_recovery_intent "
        b"import begin_intent, finish_intent\n",
    )
    command = b"command_msg.command" if name == "controller.py" else b"msg.command"
    message_id = (
        b"command_msg.message_id" if name == "controller.py" else b"msg.message_id"
    )
    source = replace_once(
        source,
        b"            result: Any = handler.target(**args)\n",
        b"            recovery_intent = begin_intent(self.mass, "
        + command
        + b", args, "
        + message_id
        + b")\n"
        b"            result: Any = handler.target(**args)\n",
    )
    after = (
        b"            # Determine the UI locale for this request "
        b"from the HTTP headers and warm it up\n"
        if name == "controller.py"
        else b"            await self._send_message("
        b"SuccessResultMessage(msg.message_id, result))\n"
    )
    return replace_once(
        source, after, b"            finish_intent(recovery_intent)\n" + after
    )


def prepare(source: bytes, name: str) -> bytes:
    """Reject every input except the exact pinned original or reviewed output."""
    digest = hashlib.sha256(source).hexdigest()
    if digest == PATCHED.get(name):
        return source
    if digest != ORIGINALS.get(name):
        fail("Unexpected Music Assistant webserver source checksum")
    output = transform(source, name)
    if hashlib.sha256(output).hexdigest() != PATCHED.get(name):
        fail("Intent guard output checksum mismatch")
    return output


def main() -> None:
    """Validate every file before any write, then atomically install the hook."""
    root = Path(sys.argv[1])
    helper = Path(__file__).with_name("ha_recovery_intent.py").read_bytes()
    if hashlib.sha256(helper).hexdigest() != HELPER_SHA256:
        fail("Intent helper checksum mismatch")
    sources = {
        name: prepare((root / "controllers/webserver" / name).read_bytes(), name)
        for name in ORIGINALS
    }
    target = root / "helpers/ha_recovery_intent.py"
    if (
        target.exists()
        and hashlib.sha256(target.read_bytes()).hexdigest() != HELPER_SHA256
    ):
        fail("Unexpected existing intent helper")
    if target.exists():
        write_verified(target, helper)
    else:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=".ha-intent-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o644)
            stream.write(helper)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    for name, source in sources.items():
        write_verified(root / "controllers/webserver" / name, source)


if __name__ == "__main__":
    main()
