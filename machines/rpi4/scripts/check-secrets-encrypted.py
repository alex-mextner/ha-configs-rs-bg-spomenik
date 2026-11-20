#!/usr/bin/env python3
"""
Fail unless every value in a sops YAML file is a complete sops ciphertext.

Usage: check-secrets-encrypted.py secrets/secrets.sops.yaml

A value passes only if it matches the whole sops envelope
ENC[AES256_GCM,data:...,iv:...,tag:...,type:...]. The file must carry the
'sops' metadata block with sops' own fields only, duplicate keys (which a YAML
parser would silently collapse, hiding a plaintext value) are rejected, and
comments must be sops-encrypted comments.
"""

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

USAGE_ARGC = 2

B64 = r"[A-Za-z0-9+/=]"
ENVELOPE = re.compile(
    rf"ENC\[AES256_GCM,data:{B64}*,iv:{B64}+,tag:{B64}+,"
    r"type:(str|int|float|bytes|bool|comment)\]"
)
# A '#' is allowed only when it starts a complete encrypted sops comment.
PLAIN_COMMENT = re.compile(
    rf"#(?!ENC\[AES256_GCM,data:{B64}*,iv:{B64}+,tag:{B64}+,type:comment\]$)"
)

# Fields sops itself writes into the 'sops' block for this repo (age recipients only:
# kms/pgp/vault backends are rejected, their nested content cannot be validated here).
# Anything else, for example a misplaced secret under 'sops:', is rejected.
SOPS_METADATA_KEYS = {
    "age",
    "lastmodified",
    "mac",
    "version",
    "shamir_threshold",
    "unencrypted_suffix",
    "encrypted_suffix",
    "unencrypted_regex",
    "encrypted_regex",
    "unencrypted_comment_regex",
    "encrypted_comment_regex",
    "mac_only_encrypted",
}
AGE_ENTRY_KEYS = {"recipient", "enc"}


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that raises on duplicate mapping keys instead of dropping one."""

    # The positional `deep` flag is PyYAML's own signature (FBT002).
    def construct_mapping(
        self,
        node: yaml.MappingNode,
        deep: bool = False,  # noqa: FBT001, FBT002
    ) -> dict[Any, Any]:
        """Construct the mapping, refusing a key that appears twice."""
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def plaintext_paths(node: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """Yield the path of every leaf that is not a complete sops ciphertext."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from plaintext_paths(value, (*path, str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from plaintext_paths(value, (*path, str(index)))
    elif not (isinstance(node, str) and ENVELOPE.fullmatch(node)):
        yield path


def plain_comment_lines(text: str) -> list[int]:
    """Line numbers carrying a comment that is not a sops-encrypted comment."""
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if PLAIN_COMMENT.search(line)
    ]


def metadata_problems(meta: dict[str, Any]) -> list[str]:
    """Unexpected fields in the 'sops' block, including inside age recipients."""
    problems = sorted(set(meta) - SOPS_METADATA_KEYS)
    for entry in meta.get("age") or []:
        if not (isinstance(entry, dict) and set(entry) <= AGE_ENTRY_KEYS):
            problems.append("age[]")
    return problems


def problems_in(text: str) -> list[str]:
    """Return a list of human-readable problems; empty means the file is fine."""
    comments = plain_comment_lines(text)
    if comments:
        return [f"plaintext comment at lines: {comments}"]
    try:
        # UniqueKeyLoader is a SafeLoader that only adds the duplicate-key check.
        document = yaml.load(text, Loader=UniqueKeyLoader)  # noqa: S506
    except yaml.YAMLError as error:
        return [f"invalid YAML: {error}"]
    if not isinstance(document, dict) or not isinstance(document.get("sops"), dict):
        return ["not a sops file (no 'sops' metadata)"]
    unknown = metadata_problems(document["sops"])
    if unknown:
        return [f"unexpected fields inside 'sops': {unknown}"]
    data = {key: value for key, value in document.items() if key != "sops"}
    if not data:
        return ["no secrets inside"]
    bad = [".".join(path) for path in plaintext_paths(data)]
    return [f"unencrypted values at: {', '.join(bad)}"] if bad else []


def main() -> int:
    """Check the file named on the command line; exit 1 on any problem, 2 on usage."""
    if len(sys.argv) != USAGE_ARGC:
        sys.stderr.write(f"{__doc__}\n")
        return 2
    name = sys.argv[1]
    problems = problems_in(Path(name).read_text(encoding="utf-8"))
    for problem in problems:
        sys.stderr.write(f"{name}: {problem}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
