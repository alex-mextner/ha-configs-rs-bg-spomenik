#!/usr/bin/env python3
"""
Tests of check-secrets-encrypted.py, the pre-commit guard for machines/rpi4/secrets.

Run: python3 machines/rpi4/scripts/test-check-secrets-encrypted.py
"""

import importlib.util
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "checker", HERE / "check-secrets-encrypted.py"
)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)

COMMITTED = (HERE.parent / "secrets" / "secrets.sops.yaml").read_text(encoding="utf-8")
CIPHER = "ENC[AES256_GCM,data:AAAA,iv:BBBB,tag:CCCC,type:str]"
SOPS_BLOCK = """sops:
    age:
        - recipient: age1example
          enc: |
            -----BEGIN AGE ENCRYPTED FILE-----
            -----END AGE ENCRYPTED FILE-----
    lastmodified: "2026-11-03T00:00:00Z"
    mac: ENC[AES256_GCM,data:AAAA,iv:BBBB,tag:CCCC,type:str]
    version: 3.11.0
"""


def problems(text: str) -> list[str]:
    """Run the checker on a file's text."""
    return checker.problems_in(text)


class CheckSecretsEncrypted(unittest.TestCase):
    """A file passes only when every value is ciphertext and the sops block is sops'."""

    def test_committed_file_passes(self) -> None:
        """The real encrypted file of the repo is accepted."""
        self.assertEqual(problems(COMMITTED), [])

    def test_encrypted_value_passes(self) -> None:
        """A minimal well-formed file is accepted."""
        self.assertEqual(problems(f"wifi-env: {CIPHER}\n" + SOPS_BLOCK), [])

    def test_decrypted_file_without_sops_block_fails(self) -> None:
        """The output of a plain `sops -d` has no sops block."""
        self.assertTrue(problems("wifi-env: WIFI_PSK=hunter22\n"))

    def test_plaintext_value_next_to_ciphertext_fails(self) -> None:
        """A plaintext value added to an encrypted file is named."""
        found = problems(f"wifi-env: {CIPHER}\nha-password: hunter22\n" + SOPS_BLOCK)
        self.assertTrue(any("ha-password" in p for p in found), found)

    def test_duplicate_key_hiding_plaintext_fails(self) -> None:
        """A duplicate key cannot hide a plaintext value behind a ciphertext one."""
        self.assertTrue(
            problems(f"wifi-env: hunter22\nwifi-env: {CIPHER}\n" + SOPS_BLOCK)
        )

    def test_plaintext_comment_fails(self) -> None:
        """A plain comment is rejected."""
        self.assertTrue(
            problems(f"# password is hunter22\nwifi-env: {CIPHER}\n" + SOPS_BLOCK)
        )

    def test_secret_hidden_in_sops_block_fails(self) -> None:
        """A field sops never writes cannot hide inside the sops block."""
        self.assertTrue(
            problems(f"wifi-env: {CIPHER}\n" + SOPS_BLOCK + "    password: hunter22\n")
        )

    def test_empty_input_fails(self) -> None:
        """An empty file (for example a failed git show) is rejected."""
        self.assertTrue(problems(""))


if __name__ == "__main__":
    unittest.main()
