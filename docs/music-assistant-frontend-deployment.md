# Music Assistant frontend deployment

The base Compose file pins a verified static frontend artifact for the stopped-group playback-target fix. Stage the complete artifact before recreating `music-assistant`; `create_host_path: false` makes a missing release fail before Docker can create an empty directory over the frontend package.

Runtime acceptance and deployment receipts belong to [issue #170](https://github.com/alex-mextner/ha-configs-rs-bg-spomenik/issues/170). The procedure below requires the approved source revision, passing CI, and a matching artifact.

## Pinned artifact

| Item | Value |
| --- | --- |
| Frontend source | [1f7cd828c8717fc20dec9f03c65c5229ec053638](https://github.com/alex-mextner/music-assistant-frontend/commit/1f7cd828c8717fc20dec9f03c65c5229ec053638) |
| Installed frontend base | 2.17.297, source `e779b9d359e47cd210c2801f16a0ce6263571903` |
| Server compatibility | Music Assistant 2.10.4, Python 3.14 package directory |
| CI build | [Run 45265242388](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45265242388) |
| Download | [Artifact 14861823225](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45265242388/artifacts/14861823225) |
| Archive SHA-256 | `d83694753be121726767b353a5cef42275ba1038e82fb6351d1ec61f3b265eaf` |
| Manifest SHA-256 | `0df2edb45e6a07ea75ff7112919fbad351da80f95ce63471b57e462c7cf8f0f0` |
| Payload | `music_assistant_frontend/` (307 verified files), `manifest.json`, and `SHA256SUMS` |
| Runtime mount | `/app/venv/lib/python3.14/site-packages/music_assistant_frontend`, read-only |

The build passed 2,989 tests across 284 files, including all 47 transfer interaction tests, playback guards for player cards, queue menus and API commands, and the existing group membership and playback-confirmation suites. Native TypeScript checks, lint, the frontend build, Python package verification, and the unchanged-source check also passed. The [initial regression run](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45200112538) failed six target/transfer checks against the unmodified installed-base code; the [follow-up regression run](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45212691624) exposed 16 failures in the other playback entry points and member-availability guard. A [control regression run](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45229030713) failed six checks covering Keep-playing controls, the captured non-idle route, and queue clear/delete during a pending transfer. The [ownership and dispatch regression run](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45249351388) failed 14 checks against the previous candidate, covering changed member ownership, playback starting during the handoff, event-listener dispatch, and component reuse. The [last review regression run](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45263752671) failed eight assertions against the preceding candidate for repeated membership changes, own-group-leader ownership and accurate post-transfer error reporting. The final source passes all 84 focused checks, including those regressions.

Retain the verified release locally before the Actions artifact expires. Its manifest records the source commit, workflow commit and run, actual tool versions, and the size and SHA-256 digest of every frontend file.

## Deliver the reviewed script

Deploy from a clean, reviewed checkout. Copy only `scripts/ma_frontend_deploy.py` to the Odroid as a private immutable file beneath `/home/ultra/.local/state/ma-frontend-170/scripts/<reviewed-ha-commit>/`. Record its Git commit, absolute destination and SHA-256 in the private delivery receipt, and verify the copied bytes before execution. The placeholder below is that recorded commit; it is not a command to update the live checkout.

The normal PR ship operation merges the repository and performs local checkout cleanup; it does not install the remote configuration. **Do not pull the deployment change into the dirty live checkout or replace its Compose file.** The guarded script inserts the one reviewed volume into the existing live file. This preserves the live image and reconnect-wrapper settings that differ from the canonical baseline.

```bash
cd /home/ultra/homeassistant
ma_frontend_script='/home/ultra/.local/state/ma-frontend-170/scripts/<reviewed-ha-commit>/ma_frontend_deploy.py'
sha256sum "$ma_frontend_script"
python3 "$ma_frontend_script" plan
```

The script's default `plan` reads live state and writes private receipts and logs. It validates the image ID, entrypoint, command, environment, mounts and **complete Compose service configuration hash** against the current container. Pending changes to other MA settings stop the operation. It also rejects original frontend files modified in the container layer, so the recorded rollback hashes describe the unchanged image. Python cache files do not invalidate that check.

The current deployment uses only `/home/ultra/homeassistant/ha.docker-compose.yaml` in project `homeassistant`. The script deliberately preserves that verified invocation. It does not call `scripts/ha_compose.sh`, whose normal `up` also manages the DNS service. If the container's configuration-file label later names additional files, the script stops before changing anything. Prepare and review a narrow update matching that actual full configuration before using this procedure on that deployment; do not remove the label/hash guards or guess an overlay list.

## Stage and preserve recovery material

1. Verify the archive against the pinned digest and safely extract it. Check every manifest entry and `SHA256SUMS`, including the exact source and CI run. Require the complete 307-file package, including `__init__.py`, `py.typed`, `index.html`, `sw.js`, `manifest.webmanifest` and `assets/`.

2. Stage the payload beneath:
   `/home/ultra/.local/share/music-assistant-frontend/releases/1f7cd828c8717fc20dec9f03c65c5229ec053638/`.
   Keep `manifest.json` and `SHA256SUMS` beside its `music_assistant_frontend/` child. Make staged files read-only and verify them again on the Odroid. Every release file and directory must belong to the deploying user and reject group/other writes; the script checks the entire tree without changing permissions.

3. Keep an additional, verified recovery copy **off the Odroid** in the existing private PC task directory:
   `/run/media/ultra/backups/home-ultra/work/ma-player-switch-20261123/ci-delivery-evidence/odroid-recovery-1f7cd828c871/`.
   Before installation it must contain the complete archive, manifest/checksums, artifact verification receipt, exact reviewed script and its provenance, and the fresh plan's `receipt.json` plus `compose.before.yaml`. Check the copies' sizes and SHA-256 digests against their originals; keep directories private and files mode `0600`. The Compose backup may contain private configuration and must never enter Git.

4. The two Odroid backup manifests include the release and receipt directories for future provisioning. Their scheduled backup service has **not** been verified as installed on this host. The explicit off-host recovery copy above is the recovery material for this deployment; adding manifest paths alone does not establish a working backup job.

5. Run a fresh plan, preserve its private receipt and exact preimage off-host, then install with that preimage SHA. Each mutating operation exclusively owns `/home/ultra/.ma-maintenance.lock`; an existing owner stops it. Every running-MA operation requires a fresh queue/player snapshot, unchanged HA start time, and all queues and players idle immediately before recreation. Paused playback also defers maintenance. A player using an external source can be playing while its MA queue reports idle, so both states are required.

The script changes only the pinned volume, validates the entire effective configuration delta, and recreates only `music-assistant` using the unchanged local image with `--no-deps --no-build --pull never`. It never edits runtime databases, recovery scripts, or player state.

## Run installation and rollback as durable user services

After the reviewed script, artifact, fresh plan and recovery copies are verified, launch every mutating operation as a detached user systemd service. The Odroid has an active user manager with lingering enabled. Use the same user and working directory as the plan. A lost SSH connection or Ctrl-C in a log monitor must not signal the guard or Compose.

For installation, replace the SHA placeholder with the value from the fresh plan:

```bash
ma_frontend_preimage_sha='FRESH_PLAN_SHA256'
ma_frontend_unit="ma-frontend-170-$(date -u +%Y%m%dT%H%M%SZ)-$$"
ma_frontend_operation_log="$(mktemp /home/ultra/.local/state/ma-frontend-170/operation-XXXXXX.log)"
chmod 0600 "$ma_frontend_operation_log"
systemd-run --user --unit="$ma_frontend_unit" --service-type=exec --remain-after-exit \
  -p Restart=no -p UMask=0077 -p WorkingDirectory=/home/ultra/homeassistant \
  -p "StandardOutput=append:$ma_frontend_operation_log" \
  -p "StandardError=append:$ma_frontend_operation_log" \
  /usr/bin/python3 "$ma_frontend_script" install \
  --expected-compose-sha "$ma_frontend_preimage_sha"
```

Do not add `--scope`, `--pty`, `--pipe` or `--wait`, automatic restarts or a runtime kill deadline. A successful launch only means the service started. Read its actual result and the receipt named in the private operation log:

```bash
systemctl --user show "$ma_frontend_unit" \
  --property=ActiveState,SubState,Result,ExecMainCode,ExecMainStatus
```

Keep the unit and result until the receipt, Compose preimage and diagnostics have been copied off-host. Do not stop the unit while Compose or verification is running. Interrupting a separate `tail -f` monitor is safe; stopping the service is not a substitute for inspecting its state.

An existing maintenance lock is never stolen automatically. If an interrupted operation leaves one behind, retain the exact lock record and operation evidence. Establish that its named owner, guard process, Compose child and associated service are all gone before considering manual removal of that exact stale record. Never remove an unknown lock or one with a live owner, including another maintenance task's lock. Inspect the recorded phase and actual container/Compose state, then obtain a fresh guarded plan; removing a stale lock alone does not establish that retrying installation is safe.

## Receipts and verification

Receipts and exact Compose preimages remain under `/home/ultra/.local/state/ma-frontend-170/`; automatic timestamped logs remain private under `/tmp`. The detached service also keeps a private operation log. Compose stdout and stderr are saved together in the attempt directory as `compose-up.log` (mode `0600`), whose path is recorded before Compose starts; other command/configuration outputs are not written to that log. Receipt writes are atomic. The `patch_started` phase is saved before the exact file replacement, and `up_started` before invoking Compose. If a pre-up check fails, the script restores its exact preimage only while it still owns the lock and the file still equals its own patch. A concurrent edit is preserved and recorded as an undo refusal.

After Compose has started, the script does not guess whether recreation completed or automatically restore configuration. It records the last phase and leaves recovery to a fresh guarded plan. Compose is not killed by an arbitrary timeout midway through container recreation; the owned process and log remain available for monitoring. Runtime verification still has a bounded 90-second retry window.

A successful installation requires the full service hash, image, wrapper and mounts to match; the mounted package and served HTML/entry assets must match their expected hashes. It also requires unchanged HA start time, exact player roster/availability and idle playback, and preserved ordered queue-item IDs, counts, current indexes and elapsed positions. Collection uses bounded pages from `player_queues/items` and rereads projected headers/player state to reject drift. Checks before recreation compare the entire snapshot. Only after an actual recreation are derived current/next-item objects and diagnostic `active_source` excluded from equality: the pinned server recomputes them at startup. The full before/after snapshots remain in the receipt. Queue loss, reordering, changed resume position, unavailable-player loss or new playback still fail verification. Copy the resulting installation receipt off-host and verify the copy immediately, including after a failed operation that needs recovery.

Reload the MA frontend and check the original interaction through the server-hosted UI: replace the idle group's leader with the intended speaker, then press Play. Record the observed selected player and actual playback in issue #170. Server asset verification does not establish which frontend origin a separate phone application uses.

The mount leaves the image's distribution metadata in place. MA may still report frontend version 2.17.297; served asset hashes establish which build the server delivers.

## Rollback

Use the private installation receipt and obtain a fresh plan and Compose preimage. Keep that plan/preimage off-host before applying rollback.

```bash
python3 "$ma_frontend_script" plan --plan-action rollback --install-receipt <installation-receipt.json>
```

Start rollback with a new unit name and private log using the durable-service launcher above, replacing its guard arguments with `rollback --expected-compose-sha FRESH_PLAN_SHA256 --install-receipt /absolute/installation-receipt.json`. Keep its actual unit result and copy the resulting receipt off-host.

Each rollback acquires a fresh owned maintenance lock and removes only this exact volume entry, preserving later independent edits. It does not require the damaged overlay files to be intact.

| Observed MA state | Guarded rollback |
| --- | --- |
| Original container still running, including a later restart of the same container | Require exact recorded post-patch Compose bytes, unchanged container configuration and original frontend hashes; remove the unapplied volume without another restart. |
| Original container stopped before the overlay was applied | Require the same original identity/configuration and trusted receipt; remove the volume and run the scoped Compose command to restore MA. |
| Overlay container running | Require a fresh idle queue snapshot and full configuration match; remove the volume and recreate only MA. |
| Overlay container proven stopped | Require the trusted receipt, fixed image, exact volume and matching configuration; remove the volume and restore MA. |

Stopped recovery requires Docker to report `Running=false`, `Restarting=false`, PID zero and an exited/created state, both at preflight and immediately before Compose. A running container with an unreachable API is refused. A restarting container is not considered stopped. An active restart loop therefore needs a separately coordinated maintenance stop and a new Docker-state check before this guarded rollback can run; the script does not recover such a loop unattended. No automatic stop or playback command is used to create eligibility.

When MA is stopped there is no fresh queue baseline. The receipt says so explicitly; the old installation snapshot is not presented as current state. Recovery verifies fresh HA uptime, API availability, nonempty player/idle queue inventory, original frontend hashes and served assets. It does not claim exact queue preservation that could not be observed beforehand.

The canonical Compose file also contains this release pin. After a runtime rollback, remove that same pin through the normal reviewed repository workflow **before any later configuration synchronization**, so a future checkout update cannot reintroduce it. Keep the artifact and receipts for diagnosis. Never restore queues by editing runtime databases or overwrite the live Compose file with a backup.

## Later upgrades

The Python package path is tied to the server image. Before changing the image or adding Compose overlays, review the frontend compatibility, active configuration-file list and deployment procedure together. Update the artifact pin or deliberately remove it through the reviewed workflow. A normal recreation with the current base file retains the mount.
