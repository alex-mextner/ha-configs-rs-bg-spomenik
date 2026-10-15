# Git-managed integration updates

HACS integration Update buttons and Home Assistant's `update.install` service use
`scripts/ha_submodule_update.py` for domains in `integration-sources.json`.
Other integrations retain the standard HACS installer. A managed error stops the
installation; it never falls back to extracting a ZIP through a submodule symlink.

The policy maps each domain to its submodule, canonical fork, accepted HACS aliases,
and branch. Dataplicity, ingress, and HACS have protected branches. Their updates
fetch the fork branch and require its manifest version to match a requested release.
HACS also requires its Git adapter and bundled `hacs_frontend/__init__.py` to exist in
the target commit. Update and publish those forks first; do not replace them with
vanilla upstream or copy an upstream release over the patches.

## Clone and pinned setup

Run from the clone, as its owner, using a Python interpreter with `aiohttp` for the
API CLI. The Git installer/setup uses only Python's standard library and Git.

```sh
git clone --recurse-submodules <configuration-repository> homeassistant
cd homeassistant
python3 scripts/ha_setup_integrations.py --initialize --ha-version 2026.9.3 --install-hooks
```

Use the **actual running HA version** in place of the example. `--root` selects an
explicit checkout; the default is the script's checkout, never a hardcoded production
path. `--target` accepts a domain, submodule name, or policy submodule path. Without
`--target`, all policy entries are processed sequentially. `--initialize` fetches
**missing** submodules at the parent's recorded commits, never remote branch tips.
Without that flag setup is offline. Existing checkouts that differ from their recorded
pins are refused; first preserve their changes and deliberately reconcile the checkout
with its parent pin. Setup does not force-reset branches or discard uncommitted work.

Tracked `custom_components/<domain>` symlinks stay symlinks. Existing plain deployment
copies stay plain copies. Untracked links are converted to plain copies after validation
and backup. Extra files, local code patches, dirty submodules, and staged submodule pins
stop installation before deployment. Commit intended patches into the fork and record
its pin before retrying. Persistent integration data declared in `hacs.json` is preserved.
Bundled frontend assets must already be committed in the HACS fork's pinned commit.

`--install-hooks` enables `.githooks` only when another hook manager is not active.
Existing hooks are never replaced. For a global/local hook manager, arrange for it to
call this checkout's `.githooks/post-checkout` and `.githooks/post-merge`, preserving
its existing checks. The tracked hooks call the optional global dispatcher, perform
an offline sync, and report failures. File-only checkouts do not deploy. Hooks need
`HA_VERSION` or this checkout's `.HA_VERSION`. A failed post-checkout/post-merge hook
does not undo the Git operation: resolve the reported mismatch and rerun setup.

Manual offline sync uses the same path:

```sh
HA_PYTHON=python3 scripts/sync-submodule-to-custom-components.sh yandex_station --ha-version 2026.9.3
```

The optional first positional argument selects one target; remaining setup options
are forwarded unchanged. `HA_PYTHON` selects the interpreter for both shell wrappers.

## Install through HA

Deploy the patched HACS fork and parent scripts/policy together before using Update.
The scripts must be reachable at `/config/scripts` in the HA container, with Git and
write access to the parent repository, submodules, backups, and Git metadata (including
any external worktree Git directories). Run against a full production clone, not a
worktree whose Git metadata is absent inside the container. The installer drops root
to the checkout owner's UID/GID and selects an existing writable HOME for Git/hooks.
Existing commit hooks still apply; a rejected pin commit rolls the deployment back.

Use HACS's Update button or the public API wrapper:

```sh
python3 scripts/ha_updates.py repositories
python3 scripts/ha_updates.py registry
python3 scripts/ha_updates.py states
scripts/apply-fork-update.sh update.yandex_station_update --version v3.21.0
```

Find the actual entity and compatible available release first; entity IDs vary.
`HA_TOKEN` comes from the environment or `--env-file` (default `~/.env`); `--url`
defaults to `HA_URL` or `http://127.0.0.1:8123`. `ha_updates.py install --entity
update.<entity> [--version <exact-tag>]` is the equivalent direct command. Omitting
`--version` lets HACS select its available release. HTTP acceptance alone is not proof
of a completed installation: inspect HA/HACS logs, update states, and the resulting pin.
The obsolete `<submodule> <fork> <branch>` wrapper interface is rejected.

Exact release names, including a `v` prefix, are preserved in HACS metadata. Default
branch installs compare commit SHAs at HACS's upstream SHA width. Manual non-default
branch UI installs are rejected **before** invoking Git because HACS's update metadata
tracks the default branch. A default branch request redirected to a different protected
fork branch is also rejected. Protected forks can still install a matching tagged
release through HACS. Parent CLI branch support remains available for deliberate
maintenance, but it does not reconcile HACS state; use the UI/API release path to do so.

## Transactions and recovery

One checkout-wide nonblocking lock serializes installs. The installer fetches the
exact requested tag (or approved branch), checks HA compatibility and Python syntax,
backs up the deployment and transaction metadata under `backups/integration-updates/`,
and commits only the integration's parent Git pointer. Other staged files are preserved.
On a deployment/pin-commit failure it restores the prior checkout and deployment.
Cancellation allows the HA installation/reconciliation to finish; installer subprocess
waits are bounded and permit rollback before termination.

HACS's normal post-install flow owns installed-version metadata and restart notices.
Never manually edit `.storage/hacs.repositories` or `.storage/hacs.data`, falsify
`last_commit`/`last_version`, or kill HA to preserve a cache edit. After Git-only
maintenance, use the HACS UI/public `update.install` path to install/reconcile the
same exact release. `update.clear_skipped` only clears a skip flag; it does not repair
installed-version metadata. Restart only through the normal validated HA procedure
when the installation requests it. Keep all backups; review newer work before restoring.

## Container secret-scanner prerequisite

HA runs with a different filesystem from its host. Provision the host native gitleaks binary into the shared config before UI updates:

```sh
python3 scripts/ha_setup_integrations.py --target xtend_tuya --ha-version 2026.9.3 --gitleaks "$(command -v gitleaks)"
```

The scanner is version-probed, atomically copied into ignored `.tools/bin/gitleaks`, and its version/checksum recorded alongside it. The Git installer puts this verified tools directory on PATH for the existing pre-commit secret scan; the hook itself is unchanged. A missing scanner still refuses the commit; no hook is bypassed. Re-provision after changing host architecture.

## Parent pin transitions

Checkout and merge hooks pass the previous parent revision and touch only changed
submodule pins. A legitimate old plain-copy deployment is verified against that
previous pin before replacing it with the new recorded version. Any user patch
still refuses the operation. Unrelated branch switches do not deploy or create
backups; unchanged pinned setup validates content and leaves deployment inodes
and backup history unchanged.

For a deliberate manual parent-pin transition use:

```sh
python3 scripts/ha_setup_integrations.py --previous-revision <old-parent-sha> --ha-version 2026.9.3
```

Sources must already contain the newly recorded commits for offline hooks.
An unrecorded source checkout is never force-reset. Missing clone sources require
explicit `--initialize`. Public branch names are recorded in `integration-sources.json`;
YandexDialogs and WebRTC use `master`, Yandex Smart Home uses `dev`.

HACS commit-version updates (repositories without tagged releases) resolve the approved default branch and verify its SHA prefix against the requested version before deployment. Stale commit notifications fail safely and ask for a refresh rather than installing an unrelated revision.
