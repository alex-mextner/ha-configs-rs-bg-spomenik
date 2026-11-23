# Copyright (c) 2026 Alex Ultra.
"""
Pinned HA reconnect backports for the deployed Music Assistant 2.10.4.

Full input/output hashes reject an unexpected package before startup. Both the
original image and the previous ERROR-frame-only guard can migrate atomically.
No downloads, credentials, or runtime Music Assistant data are used.
"""

import hashlib
import logging
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

LOGGER = logging.getLogger(__name__)
EXPECTED_ARGUMENTS = 3

ORIGINAL_SHA256 = "0b464086da1215a14250e72c144a7a57acceaa38f863337a939d4588cf04fd55"
ERROR_GUARD_SHA256 = "dc7af40887c9c48c16a5e425b1a637734718614870a26066d8c0f5248d0df0f5"
RECONNECT_GUARD_SHA256 = (
    "9cfde978bd240782b78f79ca8ee5fc8b7614e9820298cac5a942f80c597c79ca"
)
PATCHED_SHA256 = "997ab3638eb045f9ace6284db436ba2c79f67842275612fbe424756497e53c14"
PROVIDER_ORIGINAL_SHA256 = (
    "600bd557f3549e0c412341f85d51eba96ec1e7d0412b4ae85ae28ec3f13abe1b"
)
PROVIDER_PATCHED_SHA256 = (
    "bfea362c3d13c82bc9ced0786c09952ffbdd2f75fd31ae054157c0c94589a6aa"
)

# ERROR-frame fix: python-hass-client ab1bd37043a0447b526654112b9d8594b12757de.
ORIGINAL = (
    b"                    if msg.data.code == aiohttp.WSCloseCode.MESSAGE_TOO_BIG:\n"
)
REPLACEMENT = (
    b"                    # heartbeat timeouts and dropped connections"
    b" arrive as ERROR frames\n                    # too, carrying an e"
    b"xception that has no close code\n                    if (\n       "
    b"                 isinstance(msg.data, aiohttp.WebSocketError)\n  "
    b"                      and msg.data.code == aiohttp.WSCloseCode.M"
    b"ESSAGE_TOO_BIG\n                    ):\n"
)
# WSMessageTypeError derives from TypeError, not aiohttp.ClientError. Only this
# transport-specific subtype is reconnectable; unrelated TypeError stays visible.
HANDSHAKE_ORIGINAL = b"""            client_exceptions.ClientError,
        ) as err:
"""
HANDSHAKE_REPLACEMENT = b"""            client_exceptions.ClientError,
            client_exceptions.WSMessageTypeError,
        ) as err:
"""
SERVICE_ORIGINAL = b'        return await self.send_command("call_service", **params)\n'
SERVICE_REPLACEMENT = (
    b'        result = await self.send_command("call_service", **params)\n'
    b"""        try:
            from music_assistant.helpers.ha_recovery_intent import log_hass_origin

            log_hass_origin(result, domain, service, target)
        except Exception:
            # Private provenance must never change a service result.
            pass
        return result
"""
)

# Cleanup mirrors music-assistant/server PR 7844, with cancellation cleanup and
# explicit credential-error translation so invalid tokens never enter retry loops.
PROVIDER_CHANGES = (
    (
        """from hass_client.exceptions import BaseHassClientError
""",
        """from hass_client.exceptions import (
    AuthenticationFailed as HassAuthenticationFailed,
    BaseHassClientError,
)
""",
    ),
    (
        """from music_assistant_models.errors import (
""",
        """from music_assistant_models.errors import (
    AuthenticationFailed,
""",
    ),
    (
        """        try:
            await self.hass.connect()
        except BaseHassClientError as err:
            await self._cleanup_failed_init()
            err_msg = str(err) or err.__class__.__name__
            raise SetupFailedError(err_msg) from err
        self._listen_task = self.mass.create_task(self._hass_listener())
""",
        """        try:
            await self.hass.connect()
        except asyncio.CancelledError:
            await self._cleanup_failed_init()
            raise
        except HassAuthenticationFailed as err:
            await self._cleanup_failed_init()
            err_msg = str(err) or err.__class__.__name__
            raise AuthenticationFailed(err_msg) from err
        except BaseHassClientError as err:
            await self._cleanup_failed_init()
            err_msg = str(err) or err.__class__.__name__
            raise SetupFailedError(err_msg) from err
        except Exception:
            await self._cleanup_failed_init()
            raise
        self._listen_task = self.mass.create_task(self._hass_listener())
""",
    ),
)


def fail(message: str) -> NoReturn:
    """Reject an unverified or incomplete backport."""
    raise RuntimeError(message)


def replace_once(source: bytes, old: bytes, new: bytes) -> bytes:
    """Apply one reviewed substitution only at its expected location."""
    if source.count(old) != 1:
        fail("Expected exactly one reviewed backport location")
    return source.replace(old, new)


def prepare_client(source: bytes) -> bytes:
    """Validate and prepare either supported client input without writing."""
    current = hashlib.sha256(source).hexdigest()
    if current == PATCHED_SHA256:
        return source
    if current == ORIGINAL_SHA256:
        source = replace_once(source, ORIGINAL, REPLACEMENT)
    elif current not in {ERROR_GUARD_SHA256, RECONNECT_GUARD_SHA256}:
        fail(f"Unexpected hass-client source SHA256: {current}")
    if current != RECONNECT_GUARD_SHA256:
        source = replace_once(source, HANDSHAKE_ORIGINAL, HANDSHAKE_REPLACEMENT)
    source = replace_once(source, SERVICE_ORIGINAL, SERVICE_REPLACEMENT)
    if hashlib.sha256(source).hexdigest() != PATCHED_SHA256:
        fail("Client backport output does not match reviewed source")
    return source


def prepare_provider(source: bytes) -> bytes:
    """Validate and prepare the supported provider source without writing."""
    current = hashlib.sha256(source).hexdigest()
    if current == PROVIDER_PATCHED_SHA256:
        return source
    if current != PROVIDER_ORIGINAL_SHA256:
        fail(f"Unexpected MA Home Assistant source SHA256: {current}")
    for old, new in PROVIDER_CHANGES:
        source = replace_once(source, old.encode(), new.encode())
    if hashlib.sha256(source).hexdigest() != PROVIDER_PATCHED_SHA256:
        fail("Provider backport output does not match reviewed source")
    return source


def write_verified(path: Path, source: bytes) -> None:
    """Atomically replace a verified module while preserving its mode."""
    if path.read_bytes() == source:
        LOGGER.info(
            "Guard already present; verified %s SHA256 %s",
            path.name,
            hashlib.sha256(source).hexdigest(),
        )
        return
    # A power loss or interrupted startup must leave either complete old or new
    # source. Never truncate the installed dependency in place.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".ma-reconnect-", dir=path.parent, delete=False
        ) as temp:
            temp_path = Path(temp.name)
            os.fchmod(temp.fileno(), stat.S_IMODE(path.stat().st_mode))
            temp.write(source)
            temp.flush()
            os.fsync(temp.fileno())
        if (
            hashlib.sha256(temp_path.read_bytes()).digest()
            != hashlib.sha256(source).digest()
        ):
            fail("Temporary backport failed its checksum")
        temp_path.replace(path)
        temp_path = None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    if path.read_bytes() != source:
        fail("Written backport failed its checksum")
    LOGGER.info(
        "Applied reconnect guard to %s; SHA256 %s",
        path.name,
        hashlib.sha256(source).hexdigest(),
    )


def apply(path: Path) -> None:
    """Apply the client backport after validating its complete source."""
    write_verified(path, prepare_client(path.read_bytes()))


def apply_provider(path: Path) -> None:
    """Apply the provider backport after validating its complete source."""
    write_verified(path, prepare_provider(path.read_bytes()))


def main() -> None:
    """Validate both modules, apply atomically and record startup diagnostics."""
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="ma-hass-reconnect-", suffix=".log", delete=False
    ) as log:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[logging.StreamHandler(sys.stdout), logging.StreamHandler(log)],
        )
        LOGGER.info("Log %s", log.name)
        try:
            if len(sys.argv) != EXPECTED_ARGUMENTS:
                fail("Usage: apply_guard.py CLIENT_PATH PROVIDER_PATH")
            client, provider = (Path(value) for value in sys.argv[1:])
            # Verify both inputs before changing either package.
            client_source = prepare_client(client.read_bytes())
            provider_source = prepare_provider(provider.read_bytes())
            write_verified(client, client_source)
            write_verified(provider, provider_source)
        except Exception:
            LOGGER.exception("EXIT 1; log %s", log.name)
            raise
        LOGGER.info("EXIT 0; log %s", log.name)


if __name__ == "__main__":
    main()
