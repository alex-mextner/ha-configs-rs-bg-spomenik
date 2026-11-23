# Copyright (c) 2026 Alex Mextner.
"""Reject damaged or incorrectly sourced frontend releases before installation."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import ma_frontend_deploy as deploy

INDEX = "music_assistant_frontend/frontend/index.html"
INIT = "music_assistant_frontend/__init__.py"
ABC_SHA = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class ReleaseArtifactTests(unittest.TestCase):
    """Exercise the actual file verifier against a small independently built release."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.release = self.root / "release"
        (self.release / INDEX).parent.mkdir(parents=True)
        (self.release / INDEX).write_bytes(b"abc")
        (self.release / INIT).write_bytes(b"")
        self.manifest: dict[str, Any] = {
            "source_sha": "1" * 40,
            "workflow_sha": "2" * 40,
            "workflow_run_id": "123",
            "base_frontend_version": "2.17.297",
            "files": [
                {"path": INDEX, "size": 3, "sha256": ABC_SHA},
                {"path": INIT, "size": 0, "sha256": EMPTY_SHA},
            ],
        }
        constants = patch.multiple(
            deploy,
            RELEASE=self.release,
            SOURCE="1" * 40,
            WORKFLOW="2" * 40,
            RUN="123",
            FILE_COUNT=2,
            MANIFEST_SHA="",
        )
        constants.start()
        self.addCleanup(constants.stop)
        self.write_metadata()

    def write_metadata(self) -> None:
        """Write independently hashed metadata without any deployment helper."""
        manifest_bytes = json.dumps(self.manifest, sort_keys=True).encode()
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        (self.release / "manifest.json").write_bytes(manifest_bytes)
        checksums = [
            f"{row['sha256']}  {row['path']}" for row in self.manifest["files"]
        ]
        checksums.append(f"{manifest_sha}  manifest.json")
        (self.release / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
        deploy.MANIFEST_SHA = manifest_sha

    def test_valid_release_returns_verified_package_relative_hashes(self) -> None:
        self.assertEqual(
            deploy.release_hashes(),
            {"frontend/index.html": ABC_SHA, "__init__.py": EMPTY_SHA},
        )

    def test_same_size_payload_change_is_rejected(self) -> None:
        (self.release / INDEX).write_bytes(b"abd")
        with self.assertRaisesRegex(RuntimeError, "Release file differs"):
            deploy.release_hashes()

    def test_manifest_size_mismatch_is_rejected_even_with_correct_hash(self) -> None:
        self.manifest["files"][0]["size"] = 4
        self.write_metadata()
        with self.assertRaisesRegex(RuntimeError, "Release file differs"):
            deploy.release_hashes()

    def test_missing_payload_is_rejected(self) -> None:
        (self.release / INDEX).unlink()
        with self.assertRaisesRegex(RuntimeError, "file set differs"):
            deploy.release_hashes()

    def test_extra_payload_is_rejected(self) -> None:
        (self.release / "music_assistant_frontend/unreviewed.js").write_bytes(b"extra")
        with self.assertRaisesRegex(RuntimeError, "file set differs"):
            deploy.release_hashes()

    def test_release_directory_symlink_is_rejected(self) -> None:
        original = self.root / "original"
        self.release.rename(original)
        self.release.symlink_to(original, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            deploy.release_hashes()

    def test_payload_symlink_is_rejected_even_when_target_hash_matches(self) -> None:
        outside = self.root / "outside"
        outside.write_bytes(b"abc")
        (self.release / INDEX).unlink()
        (self.release / INDEX).symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            deploy.release_hashes()

    def test_directory_symlink_is_rejected_even_when_contents_match(self) -> None:
        original = self.root / "frontend"
        directory = (self.release / INDEX).parent
        directory.rename(original)
        directory.symlink_to(original, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            deploy.release_hashes()

    def test_changed_manifest_is_rejected_before_trusting_its_contents(self) -> None:
        path = self.release / "manifest.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(RuntimeError, "manifest digest"):
            deploy.release_hashes()

    def test_other_build_provenance_is_rejected_even_with_a_pinned_manifest(
        self,
    ) -> None:
        original = copy.deepcopy(self.manifest)
        for field in (
            "source_sha",
            "workflow_sha",
            "workflow_run_id",
            "base_frontend_version",
        ):
            with self.subTest(field=field):
                self.manifest = copy.deepcopy(original)
                self.manifest[field] = "another-build"
                self.write_metadata()
                with self.assertRaisesRegex(RuntimeError, "provenance"):
                    deploy.release_hashes()

    def test_incomplete_manifest_is_rejected_even_with_consistent_checksums(
        self,
    ) -> None:
        self.manifest["files"].pop()
        (self.release / INIT).unlink()
        self.write_metadata()
        with self.assertRaisesRegex(RuntimeError, "file count"):
            deploy.release_hashes()

    def test_duplicate_manifest_paths_are_rejected(self) -> None:
        self.manifest["files"][1] = copy.deepcopy(self.manifest["files"][0])
        self.write_metadata()
        with self.assertRaisesRegex(RuntimeError, "Duplicate manifest paths"):
            deploy.release_hashes()

    def test_manifest_cannot_include_a_file_outside_the_frontend_package(self) -> None:
        other = self.release / "outside.txt"
        (self.release / INDEX).rename(other)
        self.manifest["files"][0]["path"] = "outside.txt"
        self.write_metadata()
        with self.assertRaisesRegex(RuntimeError, "Invalid manifest path"):
            deploy.release_hashes()

    def test_noncanonical_manifest_path_is_rejected_even_if_it_resolves_to_a_file(
        self,
    ) -> None:
        self.manifest["files"][0]["path"] = (
            "music_assistant_frontend/frontend/../frontend/index.html"
        )
        self.write_metadata()
        with self.assertRaises(RuntimeError):
            deploy.release_hashes()

    def test_checksum_digest_mismatch_is_rejected(self) -> None:
        path = self.release / "SHA256SUMS"
        path.write_text(path.read_text().replace(ABC_SHA, "0" * 64))
        with self.assertRaisesRegex(RuntimeError, "Checksum differs"):
            deploy.release_hashes()

    def test_missing_checksum_is_rejected(self) -> None:
        path = self.release / "SHA256SUMS"
        path.write_text("\n".join(path.read_text().splitlines()[1:]) + "\n")
        with self.assertRaisesRegex(RuntimeError, "Checksum file set"):
            deploy.release_hashes()

    def test_duplicate_checksum_path_is_rejected(self) -> None:
        path = self.release / "SHA256SUMS"
        content = path.read_text()
        path.write_text(content + content.splitlines()[0] + "\n")
        with self.assertRaisesRegex(RuntimeError, "Duplicate checksum path"):
            deploy.release_hashes()
