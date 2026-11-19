#!/usr/bin/python3 -B
# ruff: noqa: T201, S603  # a CLI prints; it runs only its own modules/*/run
"""
Converge a Debian/Armbian machine to machine.toml, one idempotent module at a time.

Modelled on ubuntu-tweaks: every module lives in modules/<name>/ with a module.toml
(description, order, enabled, requires) and an executable `run` that answers three
verbs with the same logic:

    run check     read-only; exit 0 satisfied, 3 drift
    run plan      read-only; prints what `install` would change; exit 0 or 3
    run install   as root: make it true; non-zero exit = failure

The module gets its `[modules.<name>]` table as JSON in DT_OPTIONS, the whole
machine.toml as JSON in DT_CONFIG and the config directory in DT_DIR. The engine
never stops at the first failure: a failed module only blocks the modules that
`requires` it. After `install` the module is checked again, so "converged" is
observed, not assumed.

Exit codes: 0 converged, 2 usage / bad config / not the host machine.toml names,
3 drift (preview, check), 6 not root (everything but `list`), 7 failed or blocked,
130 / 143 stopped by SIGINT / SIGTERM (see Stops).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
DRIFT = 3
USAGE = 2
NO_ROOT = 6
FAILED = 7


class StopSignalError(Exception):
    """SIGINT or SIGTERM arrived while a module ran: the run stops after it."""

    def __init__(self, signum: int) -> None:
        """Keep the signal number; the message is its name."""
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


class Stops:
    """
    SIGINT and SIGTERM during a run: passed on to the running module.

    A module ends once its current command has finished (the traps in
    lib/common.sh), so apt or dpkg is never left running on its own; the
    dotfiles module also stops the installer it runs in a session of its own.
    The run then stops before the next module (StopSignalError).
    """

    def __init__(self) -> None:
        """No signal yet, no module running."""
        self.signum = 0
        self.module: subprocess.Popen | None = None

    def catch(self) -> None:
        """Handle both signals from now on."""
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, self.handle)

    def handle(self, signum: int, _frame: object) -> None:
        """Remember the first signal, pass every one on to the running module."""
        self.signum = self.signum or signum
        if self.module is not None:
            self.module.send_signal(signum)

    def check(self) -> None:
        """Stop the run once a signal has arrived."""
        if self.signum:
            raise StopSignalError(self.signum)


STOPS = Stops()


@dataclass(frozen=True)
class Module:
    """One modules/<name>/ directory plus its options from machine.toml."""

    name: str
    order: int
    description: str
    requires: tuple[str, ...]
    enabled: bool
    options: dict

    @property
    def run(self) -> Path:
        """The module's executable."""
        return DIR / "modules" / self.name / "run"


def load(machine_file: Path) -> tuple[dict, list[Module]]:
    """Read machine.toml and every module.toml, sorted by order then name."""
    try:
        config = tomllib.loads(machine_file.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        print(f"debian-tweak: cannot read {machine_file}: {error}", file=sys.stderr)
        sys.exit(USAGE)
    tables = config.get("modules", {})
    known = {p.parent.name for p in (DIR / "modules").glob("*/module.toml")}
    unknown = sorted(set(tables) - known)
    if unknown:
        print(
            f"debian-tweak: unknown module(s) in {machine_file}: {', '.join(unknown)}",
            file=sys.stderr,
        )
        sys.exit(USAGE)
    modules = []
    for name in sorted(known):
        meta = tomllib.loads((DIR / "modules" / name / "module.toml").read_text())
        options = dict(tables.get(name, {}))
        enabled = bool(options.pop("enabled", meta.get("enabled", True)))
        modules.append(
            Module(
                name,
                int(meta["order"]),
                meta["description"],
                tuple(meta.get("requires", ())),
                enabled,
                options,
            )
        )
    return config, sorted(modules, key=lambda m: (m.order, m.name))


def call(module: Module, verb: str, config: dict, *, quiet: bool) -> int:
    """Run one verb of one module; return its exit code (STOPS: signals)."""
    env = dict(
        os.environ,
        DT_OPTIONS=json.dumps(module.options),
        DT_CONFIG=json.dumps(config),
        DT_DIR=str(DIR),
    )
    out = subprocess.DEVNULL if quiet else None
    STOPS.check()
    with subprocess.Popen(
        [str(module.run), verb], env=env, stdout=out, stderr=out
    ) as proc:
        STOPS.module = proc
        if STOPS.signum:  # arrived while the module was starting
            proc.send_signal(STOPS.signum)
        try:
            code = proc.wait()
        finally:
            STOPS.module = None
    STOPS.check()
    return code


def classify(code: int) -> str:
    """Map a check/plan exit code to a result."""
    if code == 0:
        return "ok"
    return "drift" if code == DRIFT else "failed"


def converge(module: Module, config: dict, *, command: str, commit: bool) -> str:
    """Check, preview or apply one module; return ok/changed/drift/failed."""
    if command == "check":
        result = classify(call(module, "check", config, quiet=True))
        print(f"[{result}] {module.name}")
        return result
    if not commit:
        print(f"== {module.name}: {module.description}")
        return classify(call(module, "plan", config, quiet=False))
    if call(module, "check", config, quiet=True) == 0:
        print(f"[ok] {module.name}")
        return "ok"
    print(f"== {module.name}: {module.description}")
    result = "failed"
    if call(module, "install", config, quiet=False) == 0:
        rechecked = call(module, "check", config, quiet=True) == 0
        result = "changed" if rechecked else "failed"
    print(f"[{result}] {module.name}")
    return result


def summarize(results: dict[str, str], *, preview: bool) -> int:
    """Print the counts and return the run's exit code."""
    states = ("ok", "changed", "drift", "failed", "blocked")
    counts = {s: sum(1 for v in results.values() if v == s) for s in states}
    print(", ".join(f"{n} {s}" for s, n in counts.items() if n) or "nothing selected")
    if counts["failed"] or counts["blocked"]:
        return FAILED
    if counts["drift"]:
        if preview:
            print("preview only: run `sudo ./install.sh --commit` to apply")
        return DRIFT
    return 0


def parse_args() -> argparse.Namespace:
    """Command line of install.sh."""
    parser = argparse.ArgumentParser(
        prog="install.sh", description="Converge this machine to machine.toml."
    )
    parser.add_argument(
        "command", nargs="?", default="apply", choices=["apply", "check", "list"]
    )
    parser.add_argument(
        "--commit", action="store_true", help="apply the plan (default: preview only)"
    )
    parser.add_argument("--machine", type=Path, default=DIR / "machine.toml")
    parser.add_argument("--only", default="", help="comma-separated module names")
    parser.add_argument(
        "--force-host",
        metavar="HOSTNAME",
        help="run although `hostname` differs from [modules.system] hostname; "
        "HOSTNAME must be this box's current `hostname`",
    )
    args = parser.parse_args()
    if args.commit and args.command != "apply":
        parser.error("--commit only goes with apply")
    return args


def list_modules(modules: list[Module]) -> None:
    """Print every module with its order, on/off state and description."""
    for m in modules:
        state = "on " if m.enabled else "off"
        print(f"{m.order:>3} {m.name:<14} {state} {m.description}")


def host_mismatch(
    config: dict, machine_file: Path, force_host: str | None
) -> str | None:
    """
    Explain why machine_file must not converge this box, or return None.

    The declared name is [modules.system] hostname (read even with the module
    off; required): a machine.toml copied to another box must not converge
    that box. --force-host overrides it only when it names this box, so a
    wrong ssh target still stops (a fresh board, or a rename, is the intended
    case).
    """
    declared = config.get("modules", {}).get("system", {}).get("hostname")
    actual = socket.gethostname()
    if not isinstance(declared, str) or not declared:
        return (
            f"debian-tweak: {machine_file} declares no [modules.system] hostname: "
            f"set it to the name of the box it is for (this is {actual}), also "
            "with the system module off"
        )
    names = (actual, actual.split(".", 1)[0])
    if declared in names or force_host in names:
        return None
    return (
        f"debian-tweak: {machine_file} is for {declared}, this is {actual}: pass "
        f"the right --machine, or --force-host {actual} if this box is to "
        f"become {declared} (a fresh board, or a rename)"
    )


def run_selected(
    modules: list[Module], config: dict, only: set[str], args: argparse.Namespace
) -> dict[str, str]:
    """Converge the enabled (and --only selected) modules in order; return results."""
    results: dict[str, str] = {}
    for m in modules:
        if not m.enabled or (only and m.name not in only):
            continue
        blocked_by = [r for r in m.requires if results.get(r) in ("failed", "blocked")]
        if blocked_by:
            results[m.name] = "blocked"
            print(f"[blocked] {m.name}: needs {', '.join(blocked_by)}")
            continue
        results[m.name] = converge(m, config, command=args.command, commit=args.commit)
    return results


def main() -> int:
    """Entry point: list, check, preview or apply the selected modules."""
    # keep our headers in order with the modules' own output
    sys.stdout.reconfigure(line_buffering=True)
    args = parse_args()
    config, modules = load(args.machine)
    only = {n for n in args.only.split(",") if n}
    missing = only - {m.name for m in modules}
    if missing:
        print(
            f"debian-tweak: --only: unknown module(s) {', '.join(sorted(missing))}",
            file=sys.stderr,
        )
        return USAGE
    if args.command == "list":
        list_modules(modules)
        return 0
    mismatch = host_mismatch(config, args.machine, args.force_host)
    if mismatch:
        print(mismatch, file=sys.stderr)
        return USAGE
    # Without root, /etc/sudoers.d/* cannot be read and would show up as false drift.
    if os.geteuid() != 0:
        print(
            "debian-tweak: needs root (every command but list): sudo ./install.sh ...",
            file=sys.stderr,
        )
        return NO_ROOT
    STOPS.catch()
    try:
        results = run_selected(modules, config, only, args)
    except StopSignalError as stop:
        print(f"debian-tweak: stopped on {stop}", file=sys.stderr)
        return 128 + stop.signum
    preview = args.command == "apply" and not args.commit
    return summarize(results, preview=preview)


if __name__ == "__main__":
    sys.exit(main())
