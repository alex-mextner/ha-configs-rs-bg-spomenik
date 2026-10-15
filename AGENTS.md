# AGENTS.md — home-generative-agent

## Project summary
Home Assistant custom integration providing a generative agent. Core code lives in
`custom_components/home_generative_agent/`, tests in `tests/`.

## Repo layout
- `custom_components/home_generative_agent/` — integration code
- `tests/` — pytest suite
- `blueprints/` — HA blueprints used by the integration
- `scripts/` — helper scripts (notably `gen_manifest_requirements.py`)
- `requirements/` + `requirements_runtime_manifest.txt` — dependency management

## Integration module map
- `sentinel/` — anomaly detection engine, dynamic rules, proposals, suppression
- `snapshot/` — snapshot schema/builders/reducers used by Sentinel and explain flows
- `notify/` — mobile notification dispatch and action helpers
- `explain/` — prompt templates and LLM-backed explanation/discovery helpers

## Development environment
- Python: 3.14
- Virtualenv: `hga/` (managed by Makefile)
- Primary tools: `ruff` (format + lint), `pyright` (types), `pytest`

## Common commands (Makefile)
- `make venv` — create venv
- `make devdeps` — install dev deps
- `make testdeps` — install test deps
- `make runtimedeps` — regenerate + install runtime deps from manifest
- `make lint` — regenerate runtime deps + `ruff check` (non-mutating)
- `make format` — `ruff format`
- `make fix` — `ruff check --fix`
- `make test` — pytest (sets `PYTHONPATH` to repo root)
- `make typecheck` — `pyright`
- `make all` — devdeps + testdeps + runtimedeps + lint + test + check + typecheck

Note: `make lint` will fail if `requirements_runtime_manifest.txt` is out of date.

## Dependency workflow
Runtime dependencies are sourced from
`custom_components/home_generative_agent/manifest.json`.

- Don’t edit `requirements_runtime_manifest.txt` directly.
- After changing `manifest.json`, run `scripts/gen_manifest_requirements.py`
  (or `make runtimedeps`).

## Coding conventions
- Formatting/linting: `ruff format` + `ruff check`
- Type checking: `pyright` (standard mode)
- Prefer async I/O and Home Assistant patterns (`async_*` APIs,
  `async_add_executor_job` for blocking work).
- Update docs when behavior or config changes (README is the primary user doc).

## Testing
- Run `make test` for the full suite.
- Keep tests under `tests/` and mirror integration module structure where
  practical.

## custom_components/ management

Third-party integration sources are managed as **Git submodules** in `submodules/`.
Deployments in `custom_components/<domain>/` may be tracked symlinks to their source
or plain copies. The policy-driven updater preserves tracked symlinks and existing
plain copies, and refuses to overwrite local patches.

### Current submodules

| Submodule | Symlink | Repository | Pin |
|---|---|---|---|
| `submodules/dataplicity` | `custom_components/dataplicity` | `alex-mextner/Dataplicity` | protected branch `prod` |
| `submodules/hacs` | `custom_components/hacs` | `alex-mextner/hacs-integration` | protected branch `local/hacs-frontend` |
| `submodules/home-generative-agent` | `custom_components/home_generative_agent` | `goruck/home-generative-agent` | branch `main` |
| `submodules/hass-mcp-server` | `custom_components/mcp_server_http_transport` | `ganhammar/hass-mcp-server` | tag `v1.8.0` |
| `submodules/HA-WiFi-Sensor-Tracker` | `custom_components/wifi_sensor_tracker` | `5a2v0/HA-WiFi-Sensor-Tracker` | tag `2.2.5` |
| `submodules/xtend_tuya` | `custom_components/xtend_tuya` | `azerty9971/xtend_tuya` | tag `v4.4.7` |
| `submodules/YandexDialogs` | `custom_components/yandex_dialogs` | `AlexxIT/YandexDialogs` | tag `v1.3.2` |
| `submodules/yandex_smart_home` | `custom_components/yandex_smart_home` | `dext0r/yandex_smart_home` | tag `v1.1.2` |
| `submodules/YandexStation` | `custom_components/yandex_station` | `alex-mextner/YandexStation` | branch `master` |
| `submodules/hass-ingress-fork` | `custom_components/ingress` | `alex-mextner/hass_ingress` | protected branch `local-patches` |

### Rules
- **Do not copy third-party code directly** into this repo. Always add it as a submodule.
- `dataplicity` is a **patched fork** — maintained patches live in the protected `prod` branch.
  Never replace it with vanilla upstream without checking the patch delta.
- `home_generative_agent` is tracked on upstream `goruck/home-generative-agent`.
  If local patches are needed, either fork upstream or patch here and push to the fork.
- `ingress` (`hass-ingress-fork`) is a **local patched fork** of `lovelylain/hass_ingress`.
  Patches: removed donate button, removed header/title, added loading spinner CSS.
  Never replace it with vanilla upstream without re-applying patches.
- **HACS special case:** upstream `.gitignore` excludes `hacs_frontend`. The protected
  HACS fork pin must already contain `custom_components/hacs/hacs_frontend/` and the
  Git update adapter. Preserve both when maintaining the fork; clone setup validates
  their required files and never replaces the fork with upstream.
- Prefer HACS Update or public `update.install` for integration updates so Git pins,
  deployments, and HACS metadata stay in sync. For Git-only maintenance preserve
  patches, commit the source/pointer deliberately, then reconcile the exact release
  through HA; see the update guide below.
- **Verify the exact `<domain>` directory name inside the submodule** before creating the symlink. The repo name (e.g. `hass_ingress`) often differs from the actual integration domain (e.g. `ingress`). Always run `ls submodules/<repo>/custom_components/` to confirm the exact folder name, then symlink `custom_components/<domain>` to `../submodules/<repo>/custom_components/<domain>`.
- After cloning, use `scripts/ha_setup_integrations.py --initialize --ha-version <running-version>`
  to initialize missing sources at their recorded pins and validate/deploy them safely.
  Add `--install-hooks` if no existing hook manager is active.

### Git-managed HACS updates and version reconciliation

Use HACS Update or the public `update.install` service for managed integrations.
The patched HACS installer runs `scripts/ha_submodule_update.py`, preserves protected
forks, deploys exact refs, and commits the parent Git pointer. HACS's normal
post-install flow maintains its own installed-version state. Managed errors never
fall back to ZIP installation.

**Never edit `.storage/hacs.repositories` or `.storage/hacs.data` or force-kill HA to
retain a cache edit.** After a Git-only update, reconcile through the same HACS UI/API
release installation. `update.clear_skipped` only clears skip flags.

Clone setup: `python3 scripts/ha_setup_integrations.py --initialize --ha-version <running-version> --install-hooks`.
Existing hook managers are preserved; the command reports how to wire the tracked
post-checkout/post-merge hooks if one is active. Offline sync refuses unrecorded pins,
dirty sources, and local deployment patches. It preserves tracked symlinks, existing
plain copies, and the bundled HACS frontend.

See [Git-managed integration updates](docs/git-managed-integration-updates.md) for
policy, setup, API commands, branch limitations, backups, and deployment prerequisites.

## Notes
- The integration is a Home Assistant service integration (`manifest.json`).
- Prefer the Makefile workflow for setup/tasks (`make devdeps`, `make testdeps`,
  `make runtimedeps`) over legacy helper scripts.

## Host requirements (MUST stay true)

The PC is a headless-ish media/HA box; these settings are load-bearing — never disable them.

- **GDM autologin MUST be enabled** for user `ultra` (`/etc/gdm3/custom.conf`,
  `[daemon]` → `AutomaticLoginEnable=true`, `AutomaticLogin=ultra`). Without it, any
  session restart (gnome-shell leak, reboot, gdm restart) leaves the TV on the login screen
  and Kodi / host actions (`ha-host-actions`) cannot open anything on the display.
  It was found silently commented out on 2026-09-24 (file changed 2026-05-29) — check it
  after any gdm3 upgrade or display-manager tinkering.
- **The PC must never sleep while Kodi runs.** Currently guaranteed globally: GNOME
  `sleep-inactive-ac-type='nothing'` + `/etc/systemd/logind.conf.d/lid-switch.conf`
  (`IdleAction=ignore`, power/suspend keys ignored). Screen blanking during playback is
  inhibited by the `media-inhibit` user service. Do not re-enable automatic suspend.
- **Kodi runs on demand, not forever.** `script.vkliuchi_serialy` (source → pc) and
  projector power-on start Kodi via `ha-host-actions`; `packages/kodi_lifecycle.yaml`
  quits it after 30 min with nothing playing. The Kodi entity for this PC is
  `media_player.home` (Kodi integration); `media_player.kodi_home*` are Music Assistant
  players and do not reflect video playback — never use them for Kodi state.
- **gnome-shell leak guard** (root): `gnome-shell-leak-guard.timer` runs
  `/usr/local/sbin/gnome-shell-leak-guard` every 15 min, logs RSS to
  `/var/log/gnome-shell-rss.log`, and restarts gdm when gnome-shell > 3 GB and Kodi is not
  playing (relies on autologin). Normal RSS after start is ~300 MB.
- **Log limits:** journald capped at 500 MB (`/etc/systemd/journald.conf.d/size.conf`);
  `/tmp/ha_*.log` + `/tmp/wakeword_noise_watchdog.log` rotated at 5 MB
  (`/etc/logrotate.d/ha-host-logs`; `/tmp` is tmpfs = RAM); per-run
  `/tmp/ha_fail_safe/2*.log` older than 2 days deleted daily (`/etc/cron.d/ha-fail-safe-logs`).
- **LAN IP 192.168.0.39 must stay fixed** (wlo1, MAC `14:85:7f:cd:c2:4c`): it is hardcoded in
  `configuration.yaml` `internal_url` and `go2rtc.yaml` WebRTC candidates. Pin it with a DHCP
  reservation on the Ubee router (192.168.0.1). 192.168.0.11 was this PC's address back when
  it ran Windows (Kodi "Телек" entry, removed 2026-09-24); the Windows guide-window service
  (`win_guide_*` secrets, `win_host`) pointed there too and is dead now.
- **Ollama models live only in `~/.ollama`** (mounted into `homeassistant-ollama-1` as
  uid 1000). Do not create other model dirs (a stale 14 GB `~/ollama-data` duplicate from a
  manual root `docker run` was removed on 2026-09-24); pull models via
  `docker exec homeassistant-ollama-1 ollama pull ...`.

## Router (Ubee EVW32C, 192.168.0.1) — polling budget

The Ubee web server hangs when it is polled often (it stopped answering at 30 s + 1 min
polling). Router data reaches HA only through `router-cli` (`~/dev/router-cli`, command
`router`) and its local inventory DB:

- `router-inventory-update.timer` (systemd --user): `router inventory update` once an hour;
- the "Обновить" button in the network-devices card / `button.network_devices_update`
  → `rest_command.network_update` → bridge `POST /network/update` (serialized; a poll younger
  than 60 s is reused);
- `router-scan.timer` every 6 h probes the devices' web UIs (not the router).

Everything else (`sensor.network_devices_online`, `sensor.network_inventory_last_poll`, the
card's auto-refresh) reads the local DB via the bridge. Do not add integrations that poll the
router (the old `ubee` integration was removed on 2026-09-25 for this reason). router-cli logs
out of the router's admin session after each command (the session is open to the whole LAN).

## Home Assistant Docker management
HA runs in Docker under `homeassistant-homeassistant-1`. All docker commands run as
current user (no `sudo`/`run0` needed).

**Reload automations/config:** `docker restart homeassistant-homeassistant-1`

**Dataplicity tunnel note:**
After `docker restart homeassistant-homeassistant-1`, the dataplicity m2m tunnel may briefly show a
"Dataplicity Port Forwarding" page instead of HA while the agent reconnects. This is normal
and resolves automatically within ~30–60 seconds once the HA container is `healthy`.
If it persists, run `./scripts/ha_fail_safe.sh fix-dataplicity` (or simply restart HA again).

**⚠️ Restart loop prevention (2026-05-20 incident):**
The fail-safe cron job (`*/5 * * * *`) previously had a **hardcoded wrong Dataplicity URL**,
which caused it to fail every 5 minutes and restart HA in an infinite loop. This broke the
actual m2m tunnel. The script was fixed to read the URL dynamically from `.storage/core.config`.
Anti-flap guards were added: the script now refuses to restart HA if the container has been up
for less than 10 minutes. If you manually restart HA multiple times in quick succession,
Dataplicity may need **10+ minutes** to recover — do not keep restarting.

**Access HA API:**
- Token stored in `~/.env` as `HA_TOKEN`
- Base URL: `http://localhost:8123/api`
- Use: `curl -H "Authorization: Bearer $HA_TOKEN" ...`

**Other useful commands:**
- `docker exec -it homeassistant-homeassistant-1 bash` — shell inside container
- `docker logs -f homeassistant-homeassistant-1` — view logs
- `docker ps` — list all containers

**Tip:** `touch ~/.env` (if needed) as current user — never use sudo for dotfiles in ~.

**Rule:** Prefer current user for all operations. Never use `sudo` or `run0` unless strictly required. If a file in `~` requires root access, fix its permissions instead of using elevated privileges.

## Critical rules — Home Assistant config safety

**NEVER** manually edit, delete, modify, or overwrite `.storage/` files (like `core.config_entries`,
`core.entity_registry`, `assist_pipeline.pipelines`, `lovelace_dashboards`, etc.).
**NEVER** delete or modify runtime databases (e.g. `home-assistant_v2.db`, `auth.db`, `library.db`,
`*.db` files in `/home/ultra/music-assistant/`, `/home/ultra/homeassistant/`).
These files are HA's and services' internal runtime state and must only be modified through
Home Assistant APIs (REST API, Websocket API, services) or the service's own UI.

### Why runtime databases must not be touched
- `.db` files contain encrypted tokens, auth sessions, device pairings, and entity mappings.
- Deleting them (e.g. `auth.db`) breaks integrations that rely on stored tokens (e.g. `music_assistant`
  loses its HA auth token and cannot reconnect).
- SQLite WAL/shm files must stay paired with their main DB; deleting one corrupts the database.
- If a DB needs repair, use the service's own tools or HA APIs — never `rm` the file directly.

- Always prefer HA REST API, Websocket API, or service calls over manual file edits.
- Use `configuration.yaml`, `automations.yaml`, `scripts.yaml`, and other config files for persistent settings.
- If an operation requires touching `.storage/`, find the equivalent API endpoint or UI path first.
- Before any destructive operation on HA config:
  1. Always create a backup first
  2. Confirm with user before making changes
  3. Use the fail-safe scripts in `scripts/ha_fail_safe.sh` for entity registry fixes

**NEVER** delete, modify, or overwrite backups (e.g., `backups/`, `config_backup_*`, `*.tar` in repo root) without explicit user consent. Backups are the safety net for recovery.

If something breaks — restore from backup immediately.

**When restoring from backup, do not blindly overwrite files.** Always preserve changes made after the backup was taken. Use `git diff`, `tar --diff`, or manual review to merge new work with the restored state.

## Backup restoration safety

**You cannot simply restore from backup and call it done.** A backup is a snapshot in time; the live system may contain newer work that the backup does not have. Blind restoration will silently destroy that newer work.

### Pre-restore checklist (MUST do)
1. **Identify the backup date** — know exactly what point in time it represents.
2. **List all changes made since that date** — check `git log --since="<backup-date>"`, `git status`, and `git diff`.
3. **Compare backup vs current** — run `tar --diff -f backup.tar.gz` against the live tree to find files that exist now but not in the backup.
4. **Classify files**:
   - **Tracked in git** — restore from backup, then re-apply newer commits via cherry-pick or manual merge.
   - **Untracked configs** (`packages/`, `blueprints/`, `themes/`, `scripts/`, `automations.yaml`, etc.) — these may have been added after the backup. Do not overwrite them.
   - **Runtime state** (`.storage/`, `*.db`, logs) — safe to restore, but still back up current state first.
5. **Back up the current broken state BEFORE restoring** — even if it's broken, it may contain the only copy of recent work.

### Post-restore verification (MUST do)
1. `git status` — ensure no tracked files are missing or modified unexpectedly.
2. `git ls-files | xargs ls` — confirm all tracked files exist.
3. `ls packages/ blueprints/ themes/ 2>/dev/null` — confirm untracked config directories are intact.
4. `docker restart homeassistant-homeassistant-1` — restart HA and check for startup errors.
5. Check critical entities in `.storage/core.entity_registry` — ensure nothing was orphaned or lost.

### Example: what went wrong in 2026-05-18
The backup from `ha-config-20260518_030001.tar.gz` (03:00) was used to restore after a destructive git operation at ~17:00. The restore overwrote the working tree, which destroyed:
- `packages/kitchen_light_white_only.yaml` (added after 03:00)
- `packages/universal_remote.yaml` (added after 03:00)
- `submodules/hass-ingress-fork` and `custom_components/ingress` symlink (set up after 03:00)
- `ha.docker-compose.yaml` changes (made after 03:00)

These files were lost because they were not in the backup and the restore was done blindly without a diff/merge step. They were only recovered later by manual inspection and re-creation.

## Lessons learned — critical incident 2026-05-18

### What broke
An attempt to "clean up git history" caused a cascading failure:
1. `git clean -fdx` deleted HA runtime files (`.storage/`, `home-assistant_v2.db`, `automations.yaml`, `scripts.yaml`, `scenes.yaml`, `.cloud/`, `.cache/`, `.HA_VERSION`, logs, Zigbee DB).
2. `git checkout --orphan main` erased 23 commits of HA config history.
3. Submodules were placed directly in `custom_components/<domain>/` — but upstream repos nest the actual integration code inside `custom_components/<domain>/custom_components/<domain>/`, so all integrations silently failed to load.
4. HACS broke because `hacs_frontend` (pip package) was missing from the submodule.
5. Dataplicity credentials were overwritten with old values from an older backup.
6. **Config packages lost**: `packages/kitchen_light_white_only.yaml` and `packages/universal_remote.yaml` disappeared from git history because the orphan branch rewrite dropped commits that added them. They were only restored from the cron backup `ha-config-20260518_030001.tar.gz`.

### Recovery
- Restored `.storage/`, DB, configs from `ha-config-20260518_030001.tar.gz` (cron backup).
- Replaced dataplicity credentials from `/tmp/pre_bm/.storage/core.config_entries`.
- Moved submodules to `submodules/` with symlinks into `custom_components/`.
- Copied `hacs_frontend` pip package into the HACS submodule and committed inside it.

### Rules enforced from now on
- **NEVER run `git clean` in this repo.** It unconditionally destroys runtime files.
- **NEVER run `git checkout --orphan`, `git rebase`, `git reset --hard`, or any destructive history rewrite on `main`.** If orphan history is truly needed, create a new branch and keep `main` intact.
- **NEVER delete backups.** Ever. Even if they seem old or redundant. Disk space is cheap; lost configuration is expensive. This includes `backups/`, `*.tar`, `.storage.backup*`, `/tmp/ha-backup-*`, `/tmp/pre_bm/`, and any other backup artifacts.
- **Before any destructive operation:** `cp -r /home/ultra/homeassistant /tmp/ha-backup-$(date +%Y%m%d_%H%M%S)`.
- **When restoring from backup, do not blindly overwrite files.** Always preserve changes made after the backup was taken. Use `git diff`, `tar --diff`, or manual review to merge new work with the restored state.
- **For root-owned files** (e.g. Docker-created `.pyc` or `.storage` entries), use `docker run --rm -v <path>:/target alpine sh -c "rm -rf /target/<file>"` instead of `sudo`.
- **Always verify expected config files exist after any restore.** Use `git ls-files` to confirm tracked files, and compare against backup archives with `tar --diff` to spot missing untracked-but-important files (e.g., `packages/`, `blueprints/`, `themes/`).

## Git workflow

**Commit messages must be atomic** — one commit per logical change.
- Good: "fix TTS entity orphaned state" / "add health check cron"
- Bad: "fix multiple issues" / "updates"

**Always commit atomically as you work.** Do not wait for an explicit user request to commit. After each logical change (feature, fix, or config update), create a commit immediately. This keeps the history clean and makes rollbacks safe.

**NEVER commit secrets, credentials, or `.env` files.**
**NEVER commit changes to `.storage/` files directly.**

Use `git diff` to verify changes before committing.

## Secrets management

**Never commit secrets into git.** Credentials, API keys, tokens, and passwords must always live outside the repository.

### Rules
- All sensitive values go into `secrets.yaml`.
- `secrets.yaml` is already in `.gitignore` — verify this before adding any secrets.
- In YAML configs use `!secret <key_name>` instead of a literal value.
- If a secret is accidentally committed:
  1. **Rotate the secret immediately** (change password, revoke token, etc.). The value is permanently in git history.
  2. Move it to `secrets.yaml` and reference with `!secret`.
  3. Do **not** attempt destructive git history rewrites (`rebase`, `reset --hard`, `filter-branch`) on `main` — they are prohibited by this repo's safety rules.

### Example
```yaml
# GOOD — secrets.yaml
host_actions_token: YOUR_TOKEN_HERE

# GOOD — packages/*.yaml
rest_command:
  example:
    url: !secret host_network_update_url
    headers:
      X-HA-Host-Token: !secret host_actions_token
```

# HA Fail-Safe System

## Problem
Editing `.storage/core.entity_registry` directly caused HA to become unavailable. Root causes:
1. Orphaned TTS entity `tts.j_a_r_v_i_s_j_a_r_v_i_s` without `config_entry_id` caused service registration failure
2. Direct file editing bypasses HA's internal validation
3. No monitoring to detect/fix such issues automatically

## Solution: Fail-Safe System

### Scripts

#### 1. ha_fail_safe.sh
Main fail-safe management script.
```bash
./scripts/ha_fail_safe.sh [check|fix-orphaned|restart|status]
```

Examples:
```bash
# Check HA availability
./scripts/ha_fail_safe.sh check

# Check full status including TTS
./scripts/ha_fail_safe.sh status

# Auto-fix orphaned entities
./scripts/ha_fail_safe.sh fix-orphaned

# Safe restart
./scripts/ha_fail_safe.sh restart
```

#### 2. ha_health_monitor.py
Advanced monitoring with API integration.
```bash
python3 scripts/ha_health_monitor.py
```

### Cron Jobs (already configured)
```
0 3 * * * /home/ultra/backup-ha.sh >/dev/null 2>&1  # Backup
0 8,20 * * * /home/ultra/homeassistant/scripts/ha_fail_safe.sh check >> /tmp/ha_check.log 2>&1  # Health check
```

### How It Works

1. **Detection**: Every 12 hours, `ha_fail_safe.sh check` verifies HA accessibility
2. **Status Report**: `ha_fail_safe.sh status` shows:
   - HA accessibility status
   - Orphaned entities count
   - TTS pipeline configuration validity
   - Container uptime

3. **Auto-Fix**: `fix-orphaned` automatically:
   - Backs up entity registry before changes
   - Removes orphaned TTS entities (those with `orphaned_timestamp` and no `config_entry_id`)
   - Validates pipeline configuration

4. **Safe Restart**: `restart` ensures HA comes back online before declaring success

### TTS Configuration for J.A.R.V.I.S

Pipeline "Милош" uses:
- TTS Engine: `tts.j_a_r_v_i_s_j_a_r_v_i_s`
- Language: ru
- Via Fish Audio integration

Valid TTS entity must have:
- `config_entry_id`: linked to Fish Audio config entry
- `orphaned_timestamp`: null (not orphaned)

### Emergency Recovery

If HA becomes unavailable after entity registry edit:

1. Check status: `/home/ultra/homeassistant/scripts/ha_fail_safe.sh status`
2. If orphaned entities found: `/home/ultra/homeassistant/scripts/ha_fail_safe.sh fix-orphaned`
3. If still down: `/home/ultra/homeassistant/scripts/ha_fail_safe.sh restart`
4. Check logs: `tail -f /tmp/ha_fail_safe/*.log`

### Backup Location
Backups are stored at: `/tmp/ha_fail_safe/entity_registry.backup.*`

### Files Modified
- `.storage/core.entity_registry` - Entity registry (auto-backed up before changes)
- `automations.yaml` - Health check automations
- `scripts/ha_fail_safe.sh` - Main management script
- `scripts/ha_health_monitor.py` - Advanced Python monitor

## Prevention
Never edit `.storage/core.entity_registry` directly. Always use `ha_fail_safe.sh fix-orphaned` or `ha_health_monitor.py` which:
1. Create backups automatically
2. Validate changes before writing
3. Trigger HA restart if needed

## Lessons learned — YAML safety and config validation (2026-05-19)

### What broke
A YAML syntax error in `packages/voice_conversation_log.yaml` (improperly escaped newline inside a quoted string value) caused Home Assistant to fail parsing the entire configuration on restart. HA entered **recovery mode**, breaking all automations, voice pipeline, and integrations until the bad file was fixed and HA restarted again.

### Root causes
1. Directly writing complex YAML with inline string concatenation (`"...\n..." | trim`) without validation.
2. No pre-commit or pre-reload validation of YAML syntax.
3. Restarting HA immediately after editing without checking config.

### Rules enforced from now on
- **Always validate YAML before committing or reloading HA.** Run `python3 -c "import yaml; yaml.safe_load(open('FILE.yaml'))"` on every changed `.yaml` file.
- **Use `./scripts/ha_safe_reload.sh` instead of raw `docker restart`.** It validates all YAML files first and aborts if any are invalid.
- **Pre-commit hook validates YAML and Lovelace automatically.** `.githooks/pre-commit` is tracked in git and runs:
  - `scripts/ha_validate_yaml.sh` on every staged `.yaml` file, including HA tags such as `!include` and `!secret`.
  - `scripts/ha_validate_lovelace.sh` when `configuration.yaml`, `ui-*.yaml`, Lovelace `www/*.js` resources, or hook validation scripts are staged. It checks dashboard structure, panel/card shape, declared dashboard files, `/local/...` resource files, and custom-card resource matches.
- **Enable tracked hooks after clone:** `git config core.hooksPath .githooks`.
- **Never use `"\n"` inside inline YAML quoted strings.** Use YAML block scalars (`|` or `>`) or Jinja `{% set %}` blocks for multiline content.
- **Test one package at a time.** After adding a new `packages/*.yaml`, reload only that package via **Developer Tools → YAML → Check and reload** before full container restart.
- **Monitor `docker logs` for 60 seconds after any restart.** Look for `ERROR` or `recovery mode` messages.

### Safe reload script
File: `scripts/ha_safe_reload.sh` — validates YAML then restarts HA container.

### Pre-commit hook  
File: `.githooks/pre-commit` — validates staged `.yaml` files via `scripts/ha_validate_yaml.sh` and Lovelace dashboards/resources via `scripts/ha_validate_lovelace.sh` before allowing commit. Enable it with `git config core.hooksPath .githooks`.

## agent-browser CLI

`agent-browser` is installed system-wide (brew) and is the preferred tool for UI verification and frontend automation.

### Common commands
```bash
# Open HA page with API token auth (headers only work for API endpoints; frontend needs login)
agent-browser --headers '{"Authorization":"Bearer '$HA_TOKEN'"}' open http://localhost:8123

# Take screenshot for visual verification
agent-browser screenshot /tmp/ha-panel.png

# Get compact accessibility snapshot (good for AI parsing)
agent-browser snapshot -c

# Full interactive snapshot
agent-browser snapshot -i

# Click element by ref from snapshot
agent-browser click @e2

# Fill form field
agent-browser fill @e3 "text"

# Evaluate JS in page
agent-browser eval "document.title"
```

### Tips
- Frontend panels require HA username/password login; API tokens only work for REST/websocket.
- Use `--headed` for visual debugging (shows browser window).
- Use `--session-name ha` to persist cookies across commands.
- Chain commands with `&&`: `agent-browser open URL && agent-browser snapshot`.

### UI Verification Rule (Config Flow / Options Flow)
Whenever modifying `config_flow.py`, `feature_subentry_flow.py`, `strings.json`, or any translation files that affect the HA frontend UI:
1. Restart HA to load changes: `docker restart homeassistant-homeassistant-1`
2. Open the relevant settings page with `agent-browser`
3. Take a screenshot or snapshot to verify the visual result
4. Do not rely on code review alone — selectors render differently in the frontend than they appear in Python
5. Example workflow:
```bash
docker restart homeassistant-homeassistant-1
sleep 15
agent-browser --session-name ha open http://localhost:8123/config/integrations/integration/home_generative_agent
agent-browser screenshot /tmp/hga-options.png
```

## Ingress UI mode values
Valid `ui_mode` values for `hass_ingress` are: `normal`, `replace`, `toolbar`.
`panel` is **not valid** and will cause the ingress integration to fail setup, removing all sidebar items.
