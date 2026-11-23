# Music Assistant frontend deployment

The base Compose file pins a verified static frontend artifact for the stopped-group playback-target fix. Stage the complete artifact before recreating `music-assistant`; `create_host_path: false` makes a missing release fail before Docker can create an empty directory over the frontend package.

Runtime acceptance and deployment receipts belong to [issue #170](https://github.com/alex-mextner/ha-configs-rs-bg-spomenik/issues/170). The procedure below requires the approved source revision, passing CI, and a matching artifact.

## Pinned artifact

| Item | Value |
| --- | --- |
| Frontend source | [f329b7d47edc336e58079bf5093e805d959e9072](https://github.com/alex-mextner/music-assistant-frontend/commit/f329b7d47edc336e58079bf5093e805d959e9072) |
| Installed frontend base | 2.17.297, source `e779b9d359e47cd210c2801f16a0ce6263571903` |
| Server compatibility | Music Assistant 2.10.4, Python 3.14 package directory |
| CI build | [Run 45203864762](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45203864762) |
| Download | [Artifact 14832658242](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45203864762/artifacts/14832658242) |
| Archive SHA-256 | `e55cebaf0f3e2ad596788e447f18fc37c71a4179d94d696d315eb1f1405777b9` |
| Payload | `music_assistant_frontend/`, `manifest.json`, and `SHA256SUMS` |
| Runtime mount | `/app/venv/lib/python3.14/site-packages/music_assistant_frontend`, read-only |

The build passed 2,934 tests across 284 files, including all 15 transfer interaction tests and the existing group membership and playback-confirmation suites. Lint, the frontend build, Python package verification, and the unchanged-source check also passed. The [regression-only run](https://github.com/alex-mextner/music-assistant-frontend/actions/runs/45200112538) failed six relevant checks against the unmodified installed-base code.

Retain the verified release locally before the Actions artifact expires. Its manifest records the source commit, workflow commit and run, actual tool versions, and the size and SHA-256 digest of every frontend file.

## Guarded deployment script

Use the reviewed `scripts/ma_frontend_deploy.py` on the Odroid after its native review and CI gates pass. Its default `plan` operation reads live state and writes only private receipts and logs. An installation requires the exact Compose SHA-256 reported by a fresh plan; a changed file stops it. The script validates the actual running image ID against the image Compose would use, preserves the existing entrypoint, command, environment, and mounts, and refuses non-idle queues. It applies or removes the exact volume block without serializing or replacing unrelated configuration.

```bash
python3 scripts/ma_frontend_deploy.py plan
python3 scripts/ma_frontend_deploy.py install --expected-compose-sha <fresh-plan-sha256>
```

For rollback, use the installation receipt and obtain a fresh preimage:

```bash
python3 scripts/ma_frontend_deploy.py plan --plan-action rollback --install-receipt <installation-receipt.json>
python3 scripts/ma_frontend_deploy.py rollback --expected-compose-sha <fresh-plan-sha256> --install-receipt <installation-receipt.json>
```

Each mutating operation acquires its own maintenance lock. Receipts and the exact preimage backup remain under `/home/ultra/.local/state/ma-frontend-170/`; safe operational output is also written to a private timestamped `/tmp` log. A failed verification leaves its last completed phase in the receipt and does not silently restart again. Inspect that receipt and run a fresh guarded rollback when appropriate. The script never edits runtime databases or sends playback commands.

## Stage and install

1. Verify the downloaded archive digest against the pinned value. After safe extraction, check `SHA256SUMS` and every manifest entry, including the exact source SHA and CI run. Require the complete package, including `__init__.py`, `py.typed`, `index.html`, `sw.js`, `manifest.webmanifest`, and `assets/`.

2. Stage the payload beneath the immutable release directory:
   `/home/ultra/.local/share/music-assistant-frontend/releases/f329b7d47edc336e58079bf5093e805d959e9072/`.
   The Compose source points to its `music_assistant_frontend/` child. Keep `manifest.json` and `SHA256SUMS` beside that child for later verification.

3. Coordinate with the reconnect deployment through the shared `/home/ultra/.ma-maintenance.lock`. Use exclusive creation and a unique owner record; an existing or unknown owner stops this deployment. Release only the exact lock owned by this operation.

4. Capture a fresh backup of the live Compose file and the effective MA image, entrypoint, mounts, and current frontend hashes. Record the HA start time and MA queue inventory, current/next items, indexes, and positions. Require all queues to be idle immediately before recreation; paused queues also defer it. Operational scripts must log automatically to a private timestamped file under `/tmp`.

5. Apply only the added frontend volume entry to the existing live Compose file. The deployed file has image and reconnect-wrapper settings absent from the canonical baseline used for this change. **Do not replace the live file with the checkout copy.** Check that the parsed document differs only by this one volume entry and that its image, entrypoint, existing mounts, and other services remain unchanged.

6. Validate with `docker compose -f /home/ultra/homeassistant/ha.docker-compose.yaml config --quiet`. Recreate only MA with `docker compose -f /home/ultra/homeassistant/ha.docker-compose.yaml up -d --no-deps --pull never music-assistant`. Keep the existing server image digest, data mount, provider mount, and reconnect entrypoint.

7. Confirm MA API availability and the expected players. Verify the mounted frontend files and the served index/JavaScript bytes against the artifact manifest. Confirm the HA start time is unchanged and compare the queue snapshot without rewriting any runtime state.

8. Reload the MA frontend and verify the original interaction: replace the idle group's leader with the intended speaker, then press Play. The selected player and actual playback must remain on that speaker. Play must stay blocked while the transfer is pending. Record the result in issue #170.

The mount changes static files while leaving the image's distribution metadata in place. MA may still report frontend version 2.17.297; served asset hashes and observed behavior establish which build is running.

## Rollback

Before any rollback, acquire the same shared maintenance lock with a fresh unique owner record, or verify that this operation still owns its existing lock. Capture a fresh queue snapshot and HA start time, and require every queue to be idle immediately before recreation; paused queues also defer rollback. Release only this operation's exact lock after verification. These checks apply even when rollback happens later, after the original installation lock was released.

Remove only the volume entry whose source and target match this pinned release, then validate Compose and recreate only `music-assistant` with the same command. Check that any changes made after the initial backup remain intact; a wholesale backup restore is not the rollback procedure.

A rollback can remove a damaged or missing overlay without trusting that overlay's files. If installation stopped after patching Compose and before recreation, the script supports a configuration-only backout: it requires the exact recorded post-patch file, the unchanged original container identity, and the original frontend hashes, then removes the volume without restarting MA. Any mismatch stops that path for review.

The original image then supplies its frontend package. Verify the original frontend hashes, MA availability, expected players, and unchanged HA start time. Preserve the server image, reconnect wrapper, provider mounts, and data directory throughout. Keep the verified artifact and deployment receipt for diagnosis. Never restore queue state by modifying runtime databases.

## Later upgrades

The Python package path is tied to the server image. Before changing the MA image, verify its frontend package location and compatibility, then either update this release pin to a reviewed artifact or remove the overlay deliberately. A normal base-only Compose recreation retains the mount because it is part of `ha.docker-compose.yaml`.
