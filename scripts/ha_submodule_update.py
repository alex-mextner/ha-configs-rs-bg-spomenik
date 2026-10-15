#!/usr/bin/env python3
"""Transactional Git installer reached by HACS Update or pinned clone setup.

Policies protect fork patches; Git hooks must pass before the pin is accepted.
Deployment rollback never reads or writes HA .storage.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import io
import json
import os
import pwd
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
from uuid import uuid4
from typing import Literal, overload


@overload
def git(
    path: Path, *args: str, binary: Literal[True], optional: Literal[False] = False
) -> bytes: ...


@overload
def git(
    path: Path, *args: str, binary: Literal[True], optional: Literal[True]
) -> bytes | None: ...


@overload
def git(
    path: Path,
    *args: str,
    binary: Literal[False] = False,
    optional: Literal[False] = False,
) -> str: ...


@overload
def git(
    path: Path, *args: str, binary: Literal[False] = False, optional: Literal[True]
) -> str | None: ...


def git(
    path: Path, *args: str, binary: bool = False, optional: bool = False
) -> str | bytes | None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    tools = path / ".tools/bin"
    if (tools / "gitleaks").is_file():
        env["PATH"] = str(tools) + os.pathsep + env.get("PATH", os.defpath)
    command = [
        "git",
        "-c",
        "user.name=Home Assistant",
        "-c",
        "user.email=homeassistant@localhost",
        "-C",
        str(path),
        *args,
    ]
    result = subprocess.run(
        command, capture_output=True, text=not binary, timeout=120, env=env
    )
    if result.returncode:
        if optional:
            return None
        detail = result.stderr.decode(errors="replace") if binary else result.stderr
        raise RuntimeError(f"Git {args[0]} failed: {detail.strip()}")
    return result.stdout if binary else result.stdout.strip()


def checked_ref(ref: str) -> str:
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/+-]*", ref)
        or ".." in ref
        or ref.endswith("/")
    ):
        raise RuntimeError("Invalid update ref")
    return ref


def version_tuple(version: str) -> tuple[int, ...]:
    match = re.match(r"v?(\d+(?:\.\d+)*)", version)
    if not match:
        raise RuntimeError(f"Unrecognized Home Assistant version: {version}")
    return tuple(int(part) for part in match[1].split("."))


def verify_deployed(
    root: Path, submodule: Path, domain: str, revision: str, persistent: str | None
) -> None:
    """Preserve local deployment patches instead of silently replacing them."""
    deployed = root / "custom_components" / domain
    component = f"custom_components/{domain}"
    if not deployed.exists():
        return
    if (
        deployed.is_symlink()
        and deployed.resolve() != (submodule / component).resolve()
    ):
        raise RuntimeError(
            "The deployed symlink does not point to its declared submodule"
        )
    archive = git(
        submodule, "archive", "--format=tar", revision, component, binary=True
    )
    tracked = set()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            relative = Path(member.name).relative_to(component)
            tracked.add(relative.as_posix())
            target = deployed / relative
            content = tar.extractfile(member)
            if content is None:
                raise RuntimeError(f"Archive has no content for {relative}")
            if not target.is_file() or target.read_bytes() != content.read():
                raise RuntimeError(
                    f"{domain}: deployed local changes in {relative}; commit patches in the source first"
                )
    for directory, subdirs, filenames in os.walk(deployed):
        subdirs[:] = [name for name in subdirs if name != "__pycache__"]
        for name in filenames:
            relative = (Path(directory) / name).relative_to(deployed)
            if name.endswith(".pyc") or relative.as_posix() in tracked:
                continue
            if persistent and relative.is_relative_to(Path(persistent)):
                continue
            raise RuntimeError(
                f"{domain}: untracked deployed file {relative}; preserve it before updating"
            )


def install(
    *,
    root: Path,
    domain: str,
    repository: str,
    ref: str,
    ha_version: str,
    pinned: bool = False,
) -> dict:
    root = Path(root).resolve()
    policies_path = root / "integration-sources.json"
    if not policies_path.is_file():
        return {"managed": False}
    policies = json.loads(policies_path.read_text())
    policy = policies.get(domain)
    if not policy:
        deployed = root / "custom_components" / domain
        if deployed.is_symlink() and deployed.resolve().is_relative_to(
            root / "submodules"
        ):
            raise RuntimeError(
                "Submodule integration is missing an explicit update policy"
            )
        return {"managed": False}
    if not re.fullmatch(r"[a-z][a-z0-9_]*", domain):
        raise RuntimeError("Invalid integration domain")
    approved = {
        str(name).lower() for name in [policy["repository"], *policy.get("aliases", [])]
    }
    if repository.lower() not in approved:
        raise RuntimeError(f"Unapproved repository for protected integration {domain}")
    checked_ref(ref)
    subpath = policy["submodule"]
    submodule = (root / subpath).resolve()
    if not submodule.is_relative_to(root / "submodules") or not submodule.is_dir():
        raise RuntimeError("Submodule path is absent or outside the configured root")
    config_path = git(
        root, "config", "-f", ".gitmodules", "--get", f"submodule.{subpath}.path"
    )
    url = git(root, "config", "-f", ".gitmodules", "--get", f"submodule.{subpath}.url")
    if config_path != subpath or not url or url.startswith("-"):
        raise RuntimeError("Submodule does not match .gitmodules")
    with (root / ".ha-submodule-updates.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Another submodule update is running; retry after it finishes"
            ) from exc
        if git(submodule, "status", "--porcelain", "--untracked-files=no"):
            raise RuntimeError(
                f"{domain}: submodule is dirty; preserve or commit local patches first"
            )
        if git(root, "diff", "--cached", "--name-only", "--", subpath):
            raise RuntimeError(
                "Submodule pointer has staged work; refusing to overwrite it"
            )
        before = git(submodule, "rev-parse", "HEAD")
        if pinned and before != git(root, "rev-parse", f"HEAD:{subpath}"):
            raise RuntimeError(
                f"{domain}: checkout differs from the recorded pin; commit or restore it before syncing"
            )
        selected = (
            before if pinned else checked_ref(policy.get("protected_branch", ref))
        )
        is_branch = (
            selected == policy.get("branch", "main") or "protected_branch" in policy
        )
        fetch_ref = ("refs/heads/" if is_branch else "refs/tags/") + selected
        if pinned:
            target = before
        else:
            git(submodule, "fetch", "--no-tags", url, fetch_ref)
            target = git(submodule, "rev-parse", "FETCH_HEAD^{commit}")
        hacs_text = git(submodule, "show", target + ":hacs.json", optional=True)
        metadata = json.loads(hacs_text) if hacs_text else {}
        minimum = metadata.get("homeassistant")
        if minimum and version_tuple(ha_version) < version_tuple(str(minimum)):
            raise RuntimeError(
                f"{domain} requires Home Assistant {minimum}; running {ha_version}"
            )
        verify_deployed(
            root, submodule, domain, before, metadata.get("persistent_directory")
        )
        component_path = f"custom_components/{domain}"
        archive = git(
            submodule, "archive", "--format=tar", target, component_path, binary=True
        )
        with tempfile.TemporaryDirectory(prefix=".ha-update-", dir=root) as temporary:
            staging = Path(temporary)
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(staging, filter="data")
            source = staging / component_path
            manifest = json.loads((source / "manifest.json").read_text())
            if manifest.get("domain") != domain or not manifest.get("version"):
                raise RuntimeError("Target manifest domain/version is invalid")
            if (
                not pinned
                and policy.get("protected_branch")
                and re.match(r"^v?[0-9]", ref)
                and str(manifest["version"]).lstrip("v") != ref.lstrip("v")
            ):
                raise RuntimeError(
                    "Protected fork does not contain the requested release; update the fork first"
                )
            for file in source.rglob("*"):
                if file.is_symlink():
                    raise RuntimeError(
                        "Integration archive contains unsupported symbolic links"
                    )
                if file.is_file() and file.suffix == ".py":
                    compile(file.read_bytes(), str(file), "exec")
            for required in policy.get("required_files", []):
                if not (source / required).is_file():
                    raise RuntimeError(f"Protected fork patch is missing: {required}")
            deployed = root / component_path
            persistent = metadata.get("persistent_directory")
            if persistent:
                relative = Path(persistent)
                if relative.is_absolute() or ".." in relative.parts:
                    raise RuntimeError("Invalid persistent directory")
                previous_data = deployed / relative
                if previous_data.is_dir():
                    shutil.copytree(
                        previous_data,
                        source / relative,
                        dirs_exist_ok=True,
                        symlinks=True,
                    )
            stamp = (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + "-"
                + domain
                + "-"
                + uuid4().hex[:8]
            )
            backup = root / "backups/integration-updates" / stamp
            backup.mkdir(parents=True)
            had_deployment = deployed.exists() or deployed.is_symlink()
            tracked_link = deployed.is_symlink() and git(
                root, "ls-files", "-s", "--", component_path
            ).startswith("120000 ")
            if deployed.exists():
                shutil.copytree(
                    deployed,
                    backup / "deployed",
                    symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            (backup / "transaction.json").write_text(
                json.dumps(
                    {
                        "domain": domain,
                        "before": before,
                        "target": target,
                        "requested_ref": ref,
                        "selected_ref": selected,
                    },
                    indent=2,
                )
            )
            original = backup / "original"
            replaced = False
            try:
                git(submodule, "checkout", "--detach", target)
                if not tracked_link:
                    if had_deployment:
                        os.replace(deployed, original)
                    deployed.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, deployed)
                    replaced = True
                previous_pin = git(root, "rev-parse", f"HEAD:{subpath}")
                if previous_pin != target:
                    git(
                        root,
                        "commit",
                        "--only",
                        "-m",
                        f"Update {domain} to {manifest['version']} via HACS ({target[:12]})",
                        "--",
                        subpath,
                    )
            except Exception:
                git(submodule, "checkout", "--detach", before)
                if replaced:
                    os.replace(deployed, backup / "failed-deployment")
                if original.exists() or original.is_symlink():
                    os.replace(original, deployed)
                raise
            return {
                "managed": True,
                "domain": domain,
                "commit": target,
                "version": str(manifest["version"]),
                "selected_ref": selected,
                "requested_is_branch": pinned
                or ref
                in {policy.get("branch", "main"), policy.get("protected_branch")},
                "backup": str(backup),
            }


def run_as_owner(root: Path) -> None:
    """Git and hooks run as the checkout owner with an accessible home in containers."""
    owner = root.stat()
    if os.geteuid() == 0 and owner.st_uid != 0:
        os.setgroups([])
        os.setgid(owner.st_gid)
        os.setuid(owner.st_uid)
    if os.geteuid() != owner.st_uid:
        raise RuntimeError("Run integration setup as the checkout owner")
    try:
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError:
        home = root.resolve()
    if not home.is_dir() or not os.access(home, os.W_OK | os.X_OK):
        home = root.resolve()
    os.environ["HOME"] = str(home)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--ha-version", required=True)
    parser.add_argument(
        "--pinned",
        action="store_true",
        help="Deploy the currently checked-out commit without fetching",
    )
    args = parser.parse_args()

    def interrupted(_signum, _frame):
        raise RuntimeError("Update interrupted; rollback requested")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        run_as_owner(args.root)
        print(
            json.dumps(
                install(
                    root=args.root,
                    domain=args.domain,
                    repository=args.repository,
                    ref=args.ref,
                    ha_version=args.ha_version,
                    pinned=args.pinned,
                )
            )
        )
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
