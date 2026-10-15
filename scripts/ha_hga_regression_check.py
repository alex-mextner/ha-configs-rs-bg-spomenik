#!/usr/bin/env python3
# Copyright 2026 Project contributors
# JUnit is created by local pytest in this invocation, never downloaded.
# ruff: noqa: S314, FBT001, TRY301
"""
Run copied HGA tests, comparing unchanged failures with the actual Git base.

The maintained submodule suite and all local non-HGA suites must pass separately.
This is not a replacement for ``make test``: that command still exposes every
failure. Baseline tolerance applies only to unchanged copied tests AND an unchanged,
clean source pin. Reports retain every failure; changed inventories fail closed.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("submodules/home-generative-agent")
COPIED = Path("tests/custom_components/home_generative_agent")
GUARDED = [
    str(SOURCE),
    str(COPIED),
    "custom_components/home_generative_agent",
    "pytest.ini",
    "conftest.py",
    "tests/conftest.py",
    "tests/custom_components/conftest.py",
    "pyproject.toml",
    "setup.cfg",
]


def require_unchanged_source(
    base_pin: str, candidate_pin: str, loaded_pin: str, dirty: bool
) -> None:
    """Refuse baseline tolerance when tests or implementation changed."""
    if not base_pin or not (base_pin == candidate_pin == loaded_pin) or dirty:
        message = "HGA source/tests/config changed; all copied HGA tests must pass"
        raise ValueError(message)


def read_results(path: Path) -> dict[tuple[str, str], str]:
    """Parse inventory, rejecting errors, duplicates and empty reports."""
    results = {}
    for case in ET.parse(path).iter("testcase"):
        key = (case.get("classname", ""), case.get("name", ""))
        if not key[1] or key in results or case.find("error") is not None:
            message = (
                "JUnit has a collection/runtime error, duplicate or missing test ID"
            )
            raise ValueError(message)
        results[key] = next(
            (kind for kind in ("failure", "skipped") if case.find(kind) is not None),
            "passed",
        )
    if not results:
        message = "JUnit contains no test results"
        raise ValueError(message)
    return results


def compare_results(
    base: dict[tuple[str, str], str], current: dict[tuple[str, str], str]
) -> None:
    """Require identical test identities, failures and skips."""
    if not base or base != current:
        message = "Copied HGA test inventory/outcomes differ from base; refusing merge"
        raise ValueError(message)


def git_bytes(*args: str, cwd: Path = ROOT) -> bytes:
    """Read repository state without mutating branches, checkouts or credentials."""
    git = shutil.which("git")
    if git is None:
        message = "Git is required for baseline verification"
        raise OSError(message)
    return subprocess.check_output([git, *args], cwd=cwd, timeout=60)  # noqa: S603


def run_suite(
    tests_root: Path, label: str, reports: Path
) -> tuple[int, dict[tuple[str, str], str]]:
    """Run the actual suite in the same interpreter/source directory for both trees."""
    report = reports / f"hga-{label}.xml"
    log = reports / f"hga-{label}.log"
    env = dict(os.environ, PYTHONPATH=str(ROOT / SOURCE))
    env.pop("PYTEST_ADDOPTS", None)
    with log.open("w") as output:
        result = subprocess.run(  # noqa: S603 - interpreter and repo paths, no shell
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--import-mode=importlib",
                "--rootdir",
                str(tests_root),
                str(tests_root / COPIED),
                "--junitxml",
                str(report),
            ],
            cwd=ROOT / SOURCE,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=600,
            check=False,
        )
    sys.stdout.write(
        f"Copied HGA {label}: pytest exit {result.returncode}; full log {log}\n"
    )
    if result.returncode not in (0, 1):
        message = f"Copied HGA {label} did not complete normally (see {log})"
        raise ValueError(message)
    results = read_results(report)
    if result.returncode == 0 and any(state == "failure" for state in results.values()):
        message = "Pytest exit status disagrees with JUnit failures"
        raise ValueError(message)
    return result.returncode, results


def main() -> int:
    """Run copied tests with strict actual-base comparison."""
    try:
        reports = ROOT / ".cache/test-results"
        reports.mkdir(parents=True, exist_ok=True)
        current_exit, current = run_suite(ROOT, "current", reports)
        if current_exit == 0:
            sys.stdout.write(f"Copied HGA: {len(current)} results; pytest passed\n")
            return 0
        base = git_bytes("merge-base", "origin/main", "HEAD").decode().strip()
        base_pin = git_bytes("rev-parse", f"{base}:{SOURCE}").decode().strip()
        candidate_pin = git_bytes("rev-parse", f"HEAD:{SOURCE}").decode().strip()
        loaded_pin = git_bytes("rev-parse", "HEAD", cwd=ROOT / SOURCE).decode().strip()
        changed = git_bytes("diff", "--name-only", base, "--", *GUARDED).strip()
        untracked = git_bytes(
            "ls-files", "--others", "--exclude-standard", "--", *GUARDED
        ).strip()
        source_dirty = git_bytes(
            "status", "--porcelain", "--untracked-files=normal", cwd=ROOT / SOURCE
        ).strip()
        require_unchanged_source(
            base_pin,
            candidate_pin,
            loaded_pin,
            bool(changed or untracked or source_dirty),
        )
        paths = (
            git_bytes(
                "ls-tree", "-r", "--name-only", base, "--", str(COPIED), *GUARDED[3:]
            )
            .decode()
            .splitlines()
        )
        archive = git_bytes("archive", base, "--", *paths)
        with tempfile.TemporaryDirectory(prefix="hga-base-") as directory:
            base_root = Path(directory)
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                bundle.extractall(base_root, filter="data")
            base_exit, baseline = run_suite(base_root, "base", reports)
        if base_exit != current_exit:
            message = "Base and candidate pytest exit statuses differ"
            raise ValueError(message)
        compare_results(baseline, current)
        # Surface every pre-existing failure instead of representing it as passed.
        failures = [
            "::".join(key) for key, state in current.items() if state == "failure"
        ]
        sys.stdout.write(
            f"NO NEW HGA REGRESSIONS: {len(current)} identical outcomes; base {base}; "
            f"{len(failures)} existing failures remain:\n"
        )
        sys.stdout.write("\n".join(failures) + "\n")
    except (
        ValueError,
        OSError,
        KeyError,
        ET.ParseError,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as exc:
        sys.stderr.write(f"HGA regression verification FAILED: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
