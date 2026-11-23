# Copyright (c) 2026 Alex Mextner
"""Verify complete-source guard hashes and idempotent HTTP/WS instrumentation."""

import hashlib
import os
import unittest
from pathlib import Path

import apply_intent_guard as guard


class IntentGuardTests(unittest.TestCase):
    """Only the exact reviewed native image can receive the intent hook."""

    def test_exact_originals_transform_once_and_patch_is_idempotent(self) -> None:
        root = Path(os.environ["MA_WEBSERVER_ORIGINAL"])
        for name, expected in guard.ORIGINALS.items():
            with self.subTest(name=name):
                source = (root / name).read_bytes()
                self.assertEqual(hashlib.sha256(source).hexdigest(), expected)
                patched = guard.prepare(source, name)
                self.assertEqual(guard.prepare(patched, name), patched)
                self.assertEqual(patched.count(b"recovery_intent = begin_intent"), 1)
                self.assertEqual(patched.count(b"finish_intent(recovery_intent)"), 1)

    def test_changed_runtime_sources_are_rejected_before_writing(self) -> None:
        for name in guard.ORIGINALS:
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                guard.prepare(b"unreviewed upstream version", name)


if __name__ == "__main__":
    unittest.main()
