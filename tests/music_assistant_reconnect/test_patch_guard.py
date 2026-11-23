# Copyright (c) 2026 Alex Ultra.
"""
Checksum, migration, and interruption checks using untouched image sources.

MA_CLIENT_ORIGINAL and MA_PROVIDER_ORIGINAL name fixture copies made before the
startup wrapper runs. Fixtures are never vendored or fetched by these tests.
"""

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import apply_guard as guard


class PatchGuardTests(unittest.TestCase):
    """Validate exact sources and atomic failure behavior."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = Path(os.environ["MA_CLIENT_ORIGINAL"]).read_bytes()
        cls.provider = Path(os.environ["MA_PROVIDER_ORIGINAL"]).read_bytes()
        if hashlib.sha256(cls.client).hexdigest() != guard.ORIGINAL_SHA256:
            message = "Client fixture is not the pinned original image source"
            raise RuntimeError(message)
        if hashlib.sha256(cls.provider).hexdigest() != guard.PROVIDER_ORIGINAL_SHA256:
            message = "Provider fixture is not the pinned original image source"
            raise RuntimeError(message)

    def test_original_client_migrates_to_reviewed_output(self) -> None:
        result = guard.prepare_client(self.client)
        self.assertEqual(hashlib.sha256(result).hexdigest(), guard.PATCHED_SHA256)

    def test_previous_error_guard_migrates_to_same_output(self) -> None:
        previous = self.client.replace(guard.ORIGINAL, guard.REPLACEMENT)
        self.assertEqual(hashlib.sha256(previous).hexdigest(), guard.ERROR_GUARD_SHA256)
        self.assertEqual(
            guard.prepare_client(previous), guard.prepare_client(self.client)
        )

    def test_original_provider_migrates_to_reviewed_output(self) -> None:
        self.assertEqual(
            hashlib.sha256(guard.prepare_provider(self.provider)).hexdigest(),
            guard.PROVIDER_PATCHED_SHA256,
        )

    def test_second_startup_is_idempotent(self) -> None:
        client = guard.prepare_client(self.client)
        provider = guard.prepare_provider(self.provider)
        self.assertEqual(guard.prepare_client(client), client)
        self.assertEqual(guard.prepare_provider(provider), provider)

    def test_unknown_sources_are_refused(self) -> None:
        for transform, original in (
            (guard.prepare_client, self.client),
            (guard.prepare_provider, self.provider),
        ):
            with (
                self.subTest(transform=transform.__name__),
                self.assertRaisesRegex(RuntimeError, "Unexpected"),
            ):
                transform(original + b"\n")

    def test_invalid_provider_does_not_partially_patch_client(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ma-reconnect-preflight-") as root:
            client = Path(root) / "client.py"
            provider = Path(root) / "provider.py"
            client.write_bytes(self.client)
            provider.write_bytes(self.provider + b"\n")
            result = subprocess.run(  # noqa: S603 -- fixed interpreter and reviewed local patcher
                [sys.executable, guard.__file__, str(client), str(provider)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "Unexpected MA Home Assistant source", result.stdout + result.stderr
            )
            self.assertEqual(client.read_bytes(), self.client)

    def test_failed_atomic_replace_preserves_original_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ma-reconnect-atomic-") as root:
            client = Path(root) / "client.py"
            client.write_bytes(self.client)
            with (
                patch.object(
                    guard.Path, "replace", side_effect=OSError("simulated interruption")
                ),
                self.assertRaises(OSError),
            ):
                guard.apply(client)
            self.assertEqual(client.read_bytes(), self.client)
            self.assertEqual(list(Path(root).iterdir()), [client])

    def test_atomic_write_preserves_permissions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ma-reconnect-mode-") as root:
            client = Path(root) / "client.py"
            client.write_bytes(self.client)
            client.chmod(0o640)
            guard.apply(client)
            self.assertEqual(client.stat().st_mode & 0o777, 0o640)
            self.assertEqual(
                hashlib.sha256(client.read_bytes()).hexdigest(), guard.PATCHED_SHA256
            )


if __name__ == "__main__":
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="ma-hass-guard-regression-", suffix=".log", delete=False
    ) as log:

        class Tee:
            """Save standalone test output while displaying progress."""

            def write(self, text) -> None:
                sys.stderr.write(text)
                log.write(text)

            def flush(self) -> None:
                sys.stderr.flush()
                log.flush()

        output = Tee()
        output.write(f"{datetime.now(UTC).isoformat()} Log {log.name}\n")
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(PatchGuardTests)
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
        output.write(
            f"{datetime.now(UTC).isoformat()} EXIT {int(not result.wasSuccessful())}\n"
        )
        output.flush()
        sys.exit(not result.wasSuccessful())
