#!/usr/bin/env python3
"""Deploy recorded integration pins after cloning or from Git checkout/merge hooks.

Missing submodules require explicit --initialize. Existing checkouts and deployments
must match their pins; local patches and hook managers are never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ha_submodule_update import git, install, run_as_owner


def setup(
    root: Path, target: str | None, ha_version: str, initialize: bool
) -> list[dict]:
    policies = json.loads((root / "integration-sources.json").read_text())
    if not isinstance(policies, dict) or not policies:
        raise RuntimeError("Integration policies must be a nonempty mapping")
    selected = []
    for domain, policy in policies.items():
        if not isinstance(policy, dict):
            raise RuntimeError(f"Invalid policy for {domain}")
        subpath = policy["submodule"]
        if target is None or target in (domain, subpath, Path(subpath).name):
            selected.append((domain, policy))
    if not selected:
        raise RuntimeError(f"Unknown integration target: {target}")
    results = []
    for domain, policy in selected:
        subpath = policy["submodule"]
        checkout = (root / subpath).resolve()
        if not checkout.is_relative_to(root / "submodules"):
            raise RuntimeError("Submodule path is outside the configured root")
        if not (checkout / ".git").exists():
            if not initialize:
                raise RuntimeError(
                    f"{domain}: submodule is missing; rerun with --initialize"
                )
            git(root, "submodule", "update", "--init", "--", subpath)
        results.append(
            install(
                root=root,
                domain=domain,
                repository=policy["repository"],
                ref="HEAD",
                ha_version=ha_version,
                pinned=True,
            )
        )
    return results


def install_secret_scanner(root: Path, source: Path) -> Path:
    """Provision the host scanner for the existing container commit gate."""
    source = source.resolve()
    try:
        probe = subprocess.run(
            [str(source), "version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("A working host gitleaks binary is required") from exc
    target = root / ".tools/bin/gitleaks"
    target.parent.mkdir(parents=True, exist_ok=True)
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    if source != target.resolve():
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
            staged = Path(temporary.name)
        try:
            shutil.copy2(source, staged)
            staged.chmod(0o755)
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
    (target.parent / "gitleaks.json").write_text(
        json.dumps({"version": probe.stdout.strip(), "sha256": checksum}, indent=2)
    )
    return target


def install_hooks(root: Path) -> None:
    """Respect an existing global or local hook manager instead of shadowing it."""
    active = git(root, "config", "--get", "core.hooksPath", optional=True)
    if active == ".githooks":
        return
    raw = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
    existing = raw.exists() and any(
        p.is_file() and os.access(p, os.X_OK) and not p.name.endswith(".sample")
        for p in raw.iterdir()
    )
    if active or existing:
        raise RuntimeError(
            "Existing Git hooks detected; add .githooks/post-checkout and "
            "post-merge to that manager without replacing it"
        )
    git(root, "config", "--local", "core.hooksPath", ".githooks")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--target",
        help="Domain, submodule name, or policy submodule path; default: all",
    )
    parser.add_argument(
        "--ha-version",
        default=os.environ.get("HA_VERSION"),
        help="Running HA version; defaults to HA_VERSION or checkout .HA_VERSION",
    )
    parser.add_argument(
        "--initialize",
        action="store_true",
        help="Fetch missing submodules at recorded pins, never remote branch tips",
    )
    parser.add_argument(
        "--install-hooks",
        action="store_true",
        help="Enable tracked hooks only if no other hook manager is configured",
    )
    parser.add_argument(
        "--gitleaks",
        type=Path,
        help="Provision this host scanner for container commit hooks",
    )
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        run_as_owner(root)
        version_file = root / ".HA_VERSION"
        version = args.ha_version or (
            version_file.read_text().strip() if version_file.is_file() else None
        )
        if not version:
            raise RuntimeError(
                "Supply --ha-version or HA_VERSION (or a checkout .HA_VERSION) "
                "for compatibility checks"
            )
        if args.gitleaks:
            install_secret_scanner(root, args.gitleaks)
        if args.install_hooks:
            install_hooks(root)
        print(json.dumps(setup(root, args.target, version, args.initialize), indent=2))
        return 0
    except Exception as exc:
        print(f"Integration setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
