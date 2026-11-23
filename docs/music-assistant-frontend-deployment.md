# Music Assistant frontend deployment

The base Compose file pins a verified static frontend artifact for the stopped-group playback-target fix. Stage the complete artifact before recreating `music-assistant`; `create_host_path: false` makes a missing release fail before Docker can create an empty directory over the frontend package.

Runtime acceptance and deployment receipts belong to [issue #170](https://github.com/alex-mextner/ha-configs-rs-bg-spomenik/issues/170). The procedure below requires the approved source revision, passing CI, and a matching artifact.

## Pinned artifact

| Item | Value |
| --- | --- |
| Frontend source | [a520cdc4d6856a0084aa7feee05508c3650b433b](https://github.com/alex-mextner/music-assistant-frontend/commit/a520cdc4d6856a0084aa7feee05508c3650b433b) |
| Installed frontend base | 2.17.297, source `e779b9d359e47cd210c2801f16a0ce6263571903` |
| Server compatibility | Music Assistant 2.10.4, Python 3.14 package directory |
| CI build | [Run 45230718817](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45230718817) |
| Download | [Artifact 14846219123](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45230718817/artifacts/14846219123) |
| Archive SHA-256 | `47587cfdbc3b4a02d8582dccff333a2cf053ea98428c9e2a5e053f9e0683d937` |
| Manifest SHA-256 | `4b810ce3dddbac0cb27eefcd3085f18f28c9feae45d9c42b8020b4c339758e27` |
| Payload | `music_assistant_frontend/` (307 verified files), `manifest.json`, and `SHA256SUMS` |
| Runtime mount | `/app/venv/lib/python3.14/site-packages/music_assistant_frontend`, read-only |

The build passed 2,968 tests across 284 files, including all 26 transfer interaction tests, playback guards for player cards, queue menus and API commands, and the existing group membership and playback-confirmation suites. Native TypeScript checks, lint, the frontend build, Python package verification, and the unchanged-source check also passed. The [initial regression run](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45200112538) failed six target/transfer checks against the unmodified installed-base code; the [follow-up regression run](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45212691624) exposed 16 failures in the other playback entry points and member-availability guard. The [latest regression run](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45229030713) failed six checks covering Keep-playing controls, the captured non-idle route, and queue clear/delete during a pending transfer.

Retain the verified release locally before the Actions artifact expires. Its manifest records the source commit, workflow commit and run, actual tool versions, and the size and SHA-256 digest of every frontend file.

## Deliver the reviewed script

Deploy from a clean, reviewed checkout. Copy only `scripts/ma_frontend_deploy.py` to the Odroid as a private immutable file beneath `/home/ultra/.local/state/ma-frontend-170/scripts/<reviewed-ha-commit>/`. Record its Git commit, absolute destination and SHA-256 in the private delivery receipt, and verify the copied bytes before execution. The placeholder below is that recorded commit; it is not a command to update the live checkout.

The normal PR ship operation merges the repository and performs local checkout cleanup; it does not install the remote configuration. **Do not pull the deployment change into the dirty live checkout or replace its Compose file.** The guarded script inserts the one reviewed volume into the existing live file. This preserves the live image and reconnect-wrapper settings that differ from the canonical baseline.

```bash
ma_frontend_script=/home/ultra/.local/state/ma-frontend-170/scripts/<reviewed-ha-commit>/ma_frontend_deploy.py
sha256sum "$ma_frontend_script"
python3 "$ma_frontend_script" plan
python3 "$ma_frontend_script" install --expected-compose-sha <fresh-plan-sha256>
```

The script's default `plan` reads live state and writes private receipts and logs. It validates the image ID, entrypoint, command, environment, mounts and **complete Compose service configuration hash** against the current container. Pending changes to other MA settings stop the operation. It also rejects original frontend files modified in the container layer, so the recorded rollback hashes describe the unchanged image. Python cache files do not invalidate that check.

The current deployment uses only `/home/ultra/homeassistant/ha.docker-compose.yaml` in project `homeassistant`. The script deliberately preserves that verified invocation. It does not call `scripts/ha_compose.sh`, whose normal `up` also manages the DNS service. If the container's configuration-file label later names additional files, the script stops before changing anything. Prepare and review a narrow update matching that actual full configuration before using this procedure on that deployment; do not remove the label/hash guards or guess an overlay list.

## Stage and preserve recovery material

1. Verify the archive against the pinned digest and safely extract it. Check every manifest entry and `SHA256SUMS`, including the exact source and CI run. Require the complete 307-file package, including `__init__.py`, `py.typed`, `index.html`, `sw.js`, `manifest.webmanifest` and `assets/`.

2. Stage the payload beneath:
   `/home/ultra/.local/share/music-assistant-frontend/releases/a520cdc4d6856a0084aa7feee05508c3650b433b/`.
   Keep `manifest.json` and `SHA256SUMS` beside its `music_assistant_frontend/` child. Make staged files read-only and verify them again on the Odroid.

3. Keep an additional, verified recovery copy **off the Odroid** in the existing private PC task directory:
   `/run/media/ultra/backups/home-ultra/work/ma-player-switch-20261123/ci-delivery-evidence/odroid-recovery-a520cdc4d685/`.
   Before installation it must contain the complete archive, manifest/checksums, artifact verification receipt, exact reviewed script and its provenance, and the fresh plan's `receipt.json` plus `compose.before.yaml`. Check the copies' sizes and SHA-256 digests against their originals; keep directories private and files mode `0600`. The Compose backup may contain private configuration and must never enter Git.

4. The two Odroid backup manifests include the release and receipt directories for future provisioning. Their scheduled backup service has **not** been verified as installed on this host. The explicit off-host recovery copy above is the recovery material for this deployment; adding manifest paths alone does not establish a working backup job.

5. Run a fresh plan, preserve its private receipt and exact preimage off-host, then install with that preimage SHA. Each mutating operation exclusively owns `/home/ultra/.ma-maintenance.lock`; an existing owner stops it. Every running-MA operation requires a fresh queue snapshot, unchanged HA start time, and all queues idle immediately before recreation. Paused queues defer maintenance.

The script changes only the pinned volume, validates the entire effective configuration delta, and recreates only `music-assistant` using the unchanged local image with `--no-deps --no-build --pull never`. It never edits runtime databases, recovery scripts, or player state.

## Receipts and verification

Receipts and exact Compose preimages remain under `/home/ultra/.local/state/ma-frontend-170/`; automatic timestamped logs remain private under `/tmp`. Receipt writes are atomic. The `patch_started` phase is saved before the exact file replacement, and `up_started` before invoking Compose. If a pre-up check fails, the script restores its exact preimage only while it still owns the lock and the file still equals its own patch. A concurrent edit is preserved and recorded as an undo refusal.

After Compose has started, the script does not guess whether recreation completed or automatically restore configuration. It records the last phase and leaves recovery to a fresh guarded plan. Compose is not killed by an arbitrary timeout midway through container recreation; the owned process and log remain available for monitoring. Runtime verification still has a bounded 90-second retry window.

A successful installation requires the full service hash, image, wrapper and mounts to match; the mounted package and served HTML/entry assets must match their expected hashes. It also requires unchanged HA start time and the fresh queue/player snapshot, including current/next items, indexes and positions. Copy the resulting installation receipt off-host and verify the copy immediately, including after a failed operation that needs recovery.

Reload the MA frontend and check the original interaction through the server-hosted UI: replace the idle group's leader with the intended speaker, then press Play. Record the observed selected player and actual playback in issue #170. Server asset verification does not establish which frontend origin a separate phone application uses.

The mount leaves the image's distribution metadata in place. MA may still report frontend version 2.17.297; served asset hashes establish which build the server delivers.

## Rollback

Use the private installation receipt and obtain a fresh plan and Compose preimage. Keep that plan/preimage off-host before applying rollback.

```bash
python3 "$ma_frontend_script" plan --plan-action rollback --install-receipt <installation-receipt.json>
python3 "$ma_frontend_script" rollback --expected-compose-sha <fresh-plan-sha256> --install-receipt <installation-receipt.json>
```

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
