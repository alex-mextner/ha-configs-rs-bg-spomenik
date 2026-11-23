#!/usr/bin/env python3
# Copyright (c) 2026 Alex Mextner.
"""Guarded, MA-only frontend install/rollback. Defaults to a read-only plan."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Callable

SOURCE = "f329b7d47edc336e58079bf5093e805d959e9072"
WORKFLOW = "af303d71242cb27c383811b8a7f22efd0d605a0f"
RUN = "45203864762"
FILE_COUNT = 307
MANIFEST_SHA = "98b4cc70e658054046f51b15310099edad68d9380dd040a926d8c0e13679a239"
IMAGE_ID = "sha256:37a9a2776e838a754c9f5b38c432567389952304e7cb8f6b44b6cd28043de6de"
ROOT = Path("/home/ultra/homeassistant")
COMPOSE = ROOT / "ha.docker-compose.yaml"
RELEASE = Path("/home/ultra/.local/share/music-assistant-frontend/releases") / SOURCE
PACKAGE = "music_assistant_frontend"
TARGET = "/app/venv/lib/python3.14/site-packages/" + PACKAGE
LOCK = Path("/home/ultra/.ma-maintenance.lock")
STATE = Path("/home/ultra/.local/state/ma-frontend-170")
MA = "homeassistant-music-assistant-1"
HA = "homeassistant-homeassistant-1"
VOLUME = {
    "type": "bind",
    "source": str(RELEASE / PACKAGE),
    "target": TARGET,
    "read_only": True,
    "bind": {"create_host_path": False},
}
BLOCK = (
    "      # Stage the verified release before recreating MA; see "
    "docs/music-assistant-frontend-deployment.md.\n"
    "      - type: bind\n"
    f"        source: {RELEASE / PACKAGE}\n"
    f"        target: {TARGET}\n"
    "        read_only: true\n"
    "        bind:\n"
    "          create_host_path: false\n"
)
ANCHOR = re.compile(
    r"(?m)^      - (?:/home/ultra|\$\{HOME\})/homeassistant/docker/music-assistant/"
    r"person_likes:/app/venv/lib/python3\.14/site-packages/"
    r"music_assistant/providers/person_likes:ro\n"
)
MA_STATE_CODE = """
import asyncio, json
from pathlib import Path
from music_assistant_client import MusicAssistantClient
async def main():
    entries=json.loads(Path('/config/.storage/core.config_entries').read_text())['data']['entries']
    entries=[e for e in entries if e['domain']=='music_assistant']
    if len(entries)!=1: raise RuntimeError('Expected one MA integration')
    client = MusicAssistantClient(
        'http://172.20.0.1:8095', None, token=entries[0]['data']['token']
    )
    async with client:
        players=await client.send_command('players/all')
        queues=await client.send_command('player_queues/all')
    def item(value):
        return None if not value else value.get('queue_item_id')
    result={
        'players': sorted([
            {'player_id':p['player_id'],'available':p['available']} for p in players
        ],key=lambda p:p['player_id']),
        'queues': sorted([{
            'queue_id':q['queue_id'],'state':q['state'],'items':q['items'],
            'current_index':q.get('current_index'),'elapsed_time':q.get('elapsed_time'),
            'current_item':item(q.get('current_item')),'next_item':item(q.get('next_item'))
        } for q in queues],key=lambda q:q['queue_id'])
    }
    print(json.dumps(result))
asyncio.run(main())
"""
HASH_CODE = """
import hashlib,json
from pathlib import Path
root=Path(__TARGET__)
result={}
for path in sorted(root.rglob('*')):
    if path.is_symlink(): raise RuntimeError('Unexpected frontend symlink')
    if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc':
        result[str(path.relative_to(root))]=hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps(result))
""".replace("__TARGET__", repr(TARGET))


def require(condition: object, message: str) -> None:
    """Stop before a mutation when a required invariant fails."""
    if not condition:
        raise RuntimeError(message)


def digest(data: bytes) -> str:
    """Return a SHA-256 digest without logging the input."""
    return hashlib.sha256(data).hexdigest()


def run(*args: str, data: bytes | None = None, timeout: int = 45) -> bytes:
    """Run an internal command while keeping sensitive output private."""
    result = subprocess.run(  # noqa: S603 -- fixed internal argv, without a shell
        args, input=data, capture_output=True, timeout=timeout, check=False
    )
    require(result.returncode == 0, f"{args[0]} failed with exit {result.returncode}")
    return result.stdout


def json_run(*args: str, data: bytes | None = None) -> object:
    """Decode private command output in-process."""
    return json.loads(run(*args, data=data))


def compose_args() -> list[str]:
    """Pin the existing project and configuration directory."""
    return [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT),
        "-p",
        "homeassistant",
    ]


def effective(text: bytes) -> dict:
    """Normalize Compose in memory without printing environment values."""
    return json.loads(
        run(*compose_args(), "-f", "-", "config", "--format", "json", data=text)
    )


def patch_compose(text: str, action: str) -> str:
    """Insert or remove only the exact pinned MA volume block."""
    matches = list(
        re.finditer(
            r"(?ms)^  music-assistant:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)",
            text,
        )
    )
    require(len(matches) == 1, "Expected exactly one MA service block")
    service = matches[0]
    body = service.group("body")
    if action == "rollback":
        require(
            body.count(BLOCK) == 1, "Exact pinned volume block is absent or changed"
        )
        changed = body.replace(BLOCK, "", 1)
    else:
        require(
            TARGET not in body, "Frontend already has a volume or conflicting setting"
        )
        anchors = list(ANCHOR.finditer(body))
        require(len(anchors) == 1, "Expected exactly one unchanged provider volume")
        at = anchors[0].end()
        changed = body[:at] + BLOCK + body[at:]
    return text[: service.start("body")] + changed + text[service.end("body") :]


def verify_delta(before: dict, after: dict, action: str) -> None:
    """Reject any effective change outside the single frontend volume."""
    smaller, larger = (after, before) if action == "rollback" else (before, after)
    reduced = copy.deepcopy(larger)
    volumes = reduced["services"]["music-assistant"]["volumes"]
    require(volumes.count(VOLUME) == 1, "Effective frontend volume does not match pin")
    volumes.remove(VOLUME)
    require(reduced == smaller, "Compose changes exceed the single frontend volume")


def inspect(name: str) -> dict:
    """Read one Docker object without exporting its credentials."""
    return json.loads(run("docker", "inspect", name))[0]


def mounts(raw: dict) -> list[dict]:
    """Select only the mount identity fields needed for comparison."""
    return sorted(
        [
            {
                "type": m["Type"],
                "source": m["Source"],
                "target": m["Destination"],
                "read_only": not m["RW"],
            }
            for m in raw["Mounts"]
        ],
        key=lambda m: m["target"],
    )


def runtime(raw: dict) -> dict:
    """Select safe runtime identity fields for the private receipt."""
    return {
        "id": raw["Id"],
        "image_id": raw["Image"],
        "entrypoint": raw["Config"]["Entrypoint"],
        "command": raw["Config"]["Cmd"],
        "mounts": mounts(raw),
        "started_at": raw["State"]["StartedAt"],
        "running": raw["State"]["Running"],
    }


def verify_running_config(config: dict, raw: dict) -> None:
    """Match the proposed image ID and current runtime settings."""
    service = config["services"]["music-assistant"]
    image = json.loads(run("docker", "image", "inspect", service["image"]))[0]
    require(
        raw["Image"] == IMAGE_ID == image["Id"], "Proposed and running image IDs differ"
    )
    labels = raw["Config"]["Labels"]
    require(
        labels.get("com.docker.compose.project") == "homeassistant", "Wrong project"
    )
    require(
        labels.get("com.docker.compose.project.config_files") == str(COMPOSE),
        "Unexpected Compose configuration files",
    )
    entrypoint = service.get("entrypoint", image["Config"]["Entrypoint"])
    command = service.get("command", image["Config"]["Cmd"])
    require(entrypoint == raw["Config"]["Entrypoint"], "Running entrypoint differs")
    require(command == raw["Config"]["Cmd"], "Running command differs")
    expected_env = dict(value.split("=", 1) for value in image["Config"].get("Env", []))
    expected_env.update(service.get("environment", {}))
    actual_env = dict(value.split("=", 1) for value in raw["Config"].get("Env", []))
    require(expected_env == actual_env, "Running environment differs from Compose")
    expected = sorted(
        [
            {
                key: m.get(key, False)
                for key in ("type", "source", "target", "read_only")
            }
            for m in service["volumes"]
        ],
        key=lambda m: m["target"],
    )
    require(expected == mounts(raw), "Live Compose and running mounts differ")
    require(raw["State"]["Running"], "MA is not running")


def snapshot() -> dict:
    """Read queues and players through the installed HA integration credential."""
    ha = inspect(HA)
    require(ha["State"]["Running"], "HA is not running")
    state = json_run(
        "docker", "exec", "-i", HA, "python3", "-", data=MA_STATE_CODE.encode()
    )
    state["ha_started_at"] = ha["State"]["StartedAt"]
    return state


def require_idle(state: dict) -> None:
    """Defer maintenance for every non-idle queue, including paused queues."""
    require(bool(state["queues"]), "MA queue inventory is empty")
    require(all(q["state"] == "idle" for q in state["queues"]), "A queue is not idle")


def require_same_state(before: dict, after: dict) -> None:
    """Reject HA restarts, queue changes, and player inventory drift."""
    require_idle(after)
    require(before["ha_started_at"] == after["ha_started_at"], "HA restarted")
    require(
        before["queues"] == after["queues"],
        "Queue contents, indexes, or positions changed",
    )
    require(
        before["players"] == after["players"], "Player roster or availability changed"
    )


def frontend_hashes() -> dict:
    """Hash the current package without reading runtime databases."""
    return json_run("docker", "exec", "-i", MA, "python3", "-", data=HASH_CODE.encode())


def release_hashes() -> dict:
    """Check the pinned manifest and every immutable release file."""
    require(
        RELEASE.is_dir() and not RELEASE.is_symlink(), "Release is missing or a symlink"
    )
    manifest_bytes = (RELEASE / "manifest.json").read_bytes()
    require(digest(manifest_bytes) == MANIFEST_SHA, "Pinned manifest digest differs")
    manifest = json.loads(manifest_bytes)
    expected = {
        "source_sha": SOURCE,
        "workflow_sha": WORKFLOW,
        "workflow_run_id": RUN,
        "base_frontend_version": "2.17.297",
    }
    require(
        all(manifest.get(k) == v for k, v in expected.items()),
        "Artifact provenance differs",
    )
    rows = manifest["files"]
    require(len(rows) == FILE_COUNT, "Unexpected frontend file count")
    files = {row["path"]: row for row in rows}
    require(len(files) == FILE_COUNT, "Duplicate manifest paths")
    actual = set()
    for path in RELEASE.rglob("*"):
        require(not path.is_symlink(), "Release contains a symlink")
        if path.is_file():
            actual.add(str(path.relative_to(RELEASE)))
    require(
        actual == set(files) | {"manifest.json", "SHA256SUMS"},
        "Release file set differs",
    )
    for name, row in files.items():
        require(
            name.startswith(PACKAGE + "/") and ".." not in Path(name).parts,
            "Invalid manifest path",
        )
        content = (RELEASE / name).read_bytes()
        require(
            len(content) == row["size"] and digest(content) == row["sha256"],
            "Release file differs",
        )
    sums = {}
    for line in (RELEASE / "SHA256SUMS").read_text().splitlines():
        value, name = line.split("  ", 1)
        require(name not in sums, "Duplicate checksum path")
        sums[name] = value
    require(set(sums) == set(files) | {"manifest.json"}, "Checksum file set differs")
    for name, value in sums.items():
        require(digest((RELEASE / name).read_bytes()) == value, "Checksum differs")
    return {
        name.removeprefix(PACKAGE + "/"): row["sha256"] for name, row in files.items()
    }


def served_hashes(expected: dict) -> dict:
    """Verify index, worker, manifest, and entry assets served over local HTTP."""

    def fetch(path: str) -> bytes:
        connection = HTTPConnection("127.0.0.1", 8095, timeout=10)
        try:
            connection.request(
                "GET",
                "/" + path,
                headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache"},
            )
            response = connection.getresponse()
            require(response.status == HTTPStatus.OK, "Static asset request failed")
            return response.read(16_000_000)
        finally:
            connection.close()

    index = fetch("")
    require(digest(index) == expected["index.html"], "Served index hash differs")
    paths = {"sw.js", "manifest.webmanifest"}
    paths.update(
        re.findall(
            r"""(?:src|href)=["'](?:\.?/)?(assets/[^"']+\.(?:js|css))["']""",
            index.decode(),
        )
    )
    require(any(p.endswith(".js") for p in paths), "No entry JavaScript found")
    result = {"index.html": digest(index)}
    for path in sorted(paths):
        require(
            path in expected and ".." not in Path(path).parts, "Unexpected index asset"
        )
        result[path] = digest(fetch(path))
        require(result[path] == expected[path], "Served asset hash differs")
    return result


class MaintenanceLock:
    """Own one exclusive maintenance lock and release only that exact record."""

    def __init__(self, path: Path = LOCK) -> None:
        """Create a unique owner record without acquiring a lock."""
        self.path = path
        self.owner = f"owner=chatgpt-ma-frontend pid={os.getpid()} id={uuid.uuid4()}\n"
        self.acquired = False

    def __enter__(self) -> Self:
        """Create the lock exclusively; existing owners always win."""
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(self.owner)
            handle.flush()
            os.fsync(handle.fileno())
        self.acquired = True
        return self

    def check(self) -> None:
        """Verify this operation still owns the lock."""
        require(
            self.acquired and self.path.read_text() == self.owner,
            "Maintenance lock ownership changed",
        )

    def __exit__(self, *_: object) -> None:
        """Preserve an intervening owner's lock during cleanup."""
        if self.acquired and self.path.exists() and self.path.read_text() == self.owner:
            self.path.unlink()


def replace_exact(path: Path, original: bytes, changed: bytes) -> None:
    """Replace only the verified preimage while detecting concurrent writers."""
    require(not path.is_symlink(), "Compose is a symlink")
    before = path.stat()
    require(before.st_uid == os.getuid(), "Compose owner differs from the operator")

    def signature(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mtime_ns,
            value.st_size,
            value.st_mode,
            value.st_uid,
            value.st_gid,
        )

    require(path.read_bytes() == original, "Compose changed since preflight")
    fd, temporary = tempfile.mkstemp(prefix=".ma-frontend-170-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(changed)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).chmod(before.st_mode & 0o777)
        os.chown(temporary, before.st_uid, before.st_gid)
        require(
            signature(path.stat()) == signature(before)
            and path.read_bytes() == original,
            "Compose changed before write",
        )
        Path(temporary).replace(path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def save_json(path: Path, value: dict) -> None:
    """Persist a private operational receipt."""
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def frontend_plan(
    operation: str,
    args: argparse.Namespace,
    original: bytes,
    configurations: tuple[dict, dict],
    raw: dict,
) -> tuple[dict, dict | None, bool]:
    """Validate the installed image and choose a safe frontend backout path."""
    config, proposed = configurations
    if operation != "rollback":
        verify_running_config(config, raw)
        return release_hashes(), frontend_hashes(), False

    require(
        args.install_receipt is not None, "Rollback requires an installation receipt"
    )
    prior = json.loads(args.install_receipt.read_text())
    require(
        prior["source_sha"] == SOURCE and prior["action"] == "install",
        "Wrong installation receipt",
    )
    require(
        prior["status"] in {"compose_updated", "recreated", "verified"},
        "Installation was not applied",
    )
    require(
        prior["before"]["image_id"] == raw["Image"] == IMAGE_ID,
        "Original image differs",
    )
    target = prior["original_frontend_hashes"]
    require(
        isinstance(target, dict) and "index.html" in target, "Original hashes missing"
    )
    require(
        all(
            isinstance(name, str)
            and not Path(name).is_absolute()
            and ".." not in Path(name).parts
            and isinstance(value, str)
            and re.fullmatch(r"[a-f0-9]{64}", value)
            for name, value in target.items()
        ),
        "Original hash map is invalid",
    )
    configuration_only = (
        prior["status"] == "compose_updated" and raw["Id"] == prior["before"]["id"]
    )
    if configuration_only:
        require(
            digest(original) == prior["compose_proposed_sha256"],
            "Partial-install Compose differs from its receipt",
        )
        require(runtime(raw) == prior["before"], "Original container changed")
        verify_running_config(proposed, raw)
        require(frontend_hashes() == target, "Original frontend changed")
    else:
        verify_running_config(config, raw)
    # A bad or missing overlay is a reason to back out, not a rollback prerequisite.
    return target, None, configuration_only


def preflight(
    args: argparse.Namespace, directory: Path
) -> tuple[dict, bytes, bytes, dict]:
    """Prepare an exact one-volume change and fresh runtime snapshot."""
    original = COMPOSE.read_bytes()
    if args.action != "plan":
        require(
            digest(original) == args.expected_compose_sha,
            "Live Compose preimage differs",
        )
    operation = args.plan_action if args.action == "plan" else args.action
    changed = patch_compose(original.decode(), operation).encode()
    config, proposed = effective(original), effective(changed)
    verify_delta(config, proposed, operation)
    raw = inspect(MA)
    target_hashes, hashes, configuration_only = frontend_plan(
        operation, args, original, (config, proposed), raw
    )
    state = snapshot()
    require_idle(state)
    receipt = {
        "action": operation,
        "mode": args.action,
        "source_sha": SOURCE,
        "script_sha256": digest(Path(__file__).read_bytes()),
        "compose_preimage_sha256": digest(original),
        "compose_proposed_sha256": digest(changed),
        "effective_proposed_sha256": digest(
            json.dumps(proposed, sort_keys=True).encode()
        ),
        "before": runtime(raw),
        "state_before": state,
        "original_frontend_hashes": hashes,
        "configuration_only": configuration_only,
        "status": "preflight",
    }
    backup = directory / "compose.before.yaml"
    backup.write_bytes(original)
    backup.chmod(0o600)
    return receipt, original, changed, target_hashes


def verify_after(receipt: dict, expected: dict, config: dict) -> dict:
    """Verify preserved runtime state and actual frontend bytes."""
    current = inspect(MA)
    verify_running_config(config, current)
    before, after = receipt["before"], runtime(current)
    require(after["entrypoint"] == before["entrypoint"], "Entrypoint changed")
    require(after["command"] == before["command"], "Command changed")
    state = snapshot()
    require_same_state(receipt["state_before"], state)
    require(frontend_hashes() == expected, "Mounted frontend differs")
    return {
        "after": after,
        "state_after": state,
        "served_hashes": served_hashes(expected),
    }


def apply_change(
    args: argparse.Namespace, directory: Path, log: Callable[[str], None]
) -> dict:
    """Apply one scoped change under the shared maintenance lock."""
    with MaintenanceLock() as lock:
        receipt, original, changed, expected = preflight(args, directory)
        path = directory / "receipt.json"
        save_json(path, receipt)
        lock.check()
        require_same_state(receipt["state_before"], snapshot())
        require(
            runtime(inspect(MA)) == receipt["before"], "MA changed during preflight"
        )
        replace_exact(COMPOSE, original, changed)
        receipt["status"] = "compose_updated"
        save_json(path, receipt)
        lock.check()
        require(COMPOSE.read_bytes() == changed, "Compose changed after patch")
        current_config = effective(COMPOSE.read_bytes())
        require(
            digest(json.dumps(current_config, sort_keys=True).encode())
            == receipt["effective_proposed_sha256"],
            "Effective configuration changed",
        )
        require_same_state(receipt["state_before"], snapshot())
        require(
            runtime(inspect(MA)) == receipt["before"], "MA changed before recreation"
        )
        # Re-resolve the local image immediately before --pull never recreates MA.
        final_config = effective(changed)
        require(
            digest(json.dumps(final_config, sort_keys=True).encode())
            == receipt["effective_proposed_sha256"],
            "Effective configuration changed before recreation",
        )
        image = final_config["services"]["music-assistant"]["image"]
        require(
            json.loads(run("docker", "image", "inspect", image))[0]["Id"] == IMAGE_ID,
            "Image changed",
        )
        if not receipt.get("configuration_only", False):
            lock.check()
            require(
                COMPOSE.read_bytes() == changed,
                "Compose changed immediately before recreation",
            )
            log("Recreating only music-assistant with its unchanged image.")
            run(
                *compose_args(),
                "-f",
                str(COMPOSE),
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "music-assistant",
                timeout=90,
            )
            receipt["status"] = "recreated"
            save_json(path, receipt)
        else:
            log(
                "Removing only the unapplied volume; "
                "original MA container stays running."
            )
        deadline = time.monotonic() + 90
        while True:
            lock.check()
            try:
                receipt.update(verify_after(receipt, expected, current_config))
                break
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                if time.monotonic() >= deadline:
                    raise
                log(f"Waiting for MA verification: {exc}")
                time.sleep(3)
        require(COMPOSE.read_bytes() == changed, "Compose changed during verification")
        receipt["status"] = "verified"
        save_json(path, receipt)
        return receipt


def main() -> int:
    """Run the guarded CLI with private automatic operational logging."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["plan", "install", "rollback"], nargs="?", default="plan"
    )
    parser.add_argument("--expected-compose-sha")
    parser.add_argument(
        "--plan-action", choices=["install", "rollback"], default="install"
    )
    parser.add_argument("--install-receipt", type=Path)
    args = parser.parse_args()
    if args.action != "plan" and not re.fullmatch(
        r"[a-f0-9]{64}", args.expected_compose_sha or ""
    ):
        parser.error(
            "install/rollback require --expected-compose-sha from a fresh preflight"
        )
    os.umask(0o077)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    directory = STATE / stamp
    directory.mkdir(mode=0o700, parents=True)
    fd, log_path = tempfile.mkstemp(
        prefix="ma-frontend-170-" + stamp + "-", suffix=".log"
    )
    with os.fdopen(fd, "w") as handle:

        def log(message: str) -> None:
            line = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + message
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            handle.write(line + "\n")
            handle.flush()

        log(
            f"Action={args.action}; receipt={directory / 'receipt.json'}; "
            f"log={log_path}"
        )
        try:
            if args.action == "plan":
                require(
                    not LOCK.exists(), "Another maintenance operation owns the lock"
                )
                receipt, _, _, _ = preflight(args, directory)
                save_json(directory / "receipt.json", receipt)
                log("Plan verified; no Compose or runtime changes.")
            else:
                receipt = apply_change(args, directory, log)
                log(
                    "MA deployment verified; "
                    "browser interaction acceptance remains separate."
                )
            log("Compose preimage SHA-256: " + receipt["compose_preimage_sha256"])
        except (
            RuntimeError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.TimeoutExpired,
        ) as exc:
            log(f"Stopped: {type(exc).__name__}: {exc}")
            log(
                "Inspect the receipt before retrying; "
                "rollback requires a fresh lock and idle queues."
            )
            return 1
        else:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
