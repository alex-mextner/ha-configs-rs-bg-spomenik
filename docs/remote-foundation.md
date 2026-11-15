# Remote routing dependencies

The remote integration uses the existing household media and host interfaces listed below. This publication brings those prerequisites into canonical Git history without importing the unrelated Odroid commit history.

## Source provenance

The package definitions come from the isolated Odroid source checkpoint `16b7023557f2ec4bd04ec38a25d22e273025abb0`, based on the live checkout `d2dd79e5e9f90ad104dfe77054fd7486ff473360`. Their existing behavior is preserved:

| Dependency | Required contract |
| --- | --- |
| `packages/projector.yaml` | The off sequence waits for stable HDMI feedback for 30 seconds, sends POWER, then rechecks HDMI after 3 seconds before a second POWER. Shutdown does not select TV mode. |
| `packages/media_power_pairs.yaml` | Authenticated host status reports actual Kodi process presence for paired-power feedback. |
| `packages/host_actions.yaml` | Authenticated REST definitions expose Kodi power, keyboard state and text input. |
| `packages/music_remote.yaml` | Existing backend selection and music commands support AUDIO play/pause. Explicit compatibility beep scripts remain available; automatic AUDIO transitions use canonical target speech. |

The selected host bridge additions publish `GET /kodi/status`, `POST /kodi/keyboard` and `POST /kodi/text-input`, with their existing authentication, revision checks and volatile-text rules. The bridge's media and audit helpers were extracted without changing these contracts. The source contains neither the unrelated fireplace feature nor a production-service replacement instruction.

## Host installation boundary

The observed Odroid service executes `/home/ultra/.local/bin/ha-host-actions.py`. It does not import this repository's copy automatically. The running script observed during this task had SHA256 `462d114f832ece2cfdf35de57fc77baec47b95f2490a5b3570c7fefe6997b2f8` and already registered the required three routes along with additional owner features.

A fresh installation of the published bridge must keep these three files adjacent:

- `ha-host-actions.py`, executable mode `0755`;
- `ha_host_media.py`, mode `0644`;
- `ha_host_audit.py`, mode `0644`.

The copied-bundle regression imports all three from an isolated directory and verifies the actual registered routes. No tracked installer copies the bridge by itself. The existing Odroid executable and its service are excluded from this remote deployment; a later host deployment must reconcile its additional features before replacement.

## Native Android text provider

The Android Remote extension follows the repository's third-party integration convention: a Git submodule at `submodules/ha-androidtv-remote` and a relative `custom_components/androidtv_remote` symlink. The required implementation is pinned to `2e79b2fe8804bc0885e30e552e7a908d08acabff` in the owner's private `alex-mextner/ha-androidtv-remote` repository. That implementation is already present on Odroid.

An authorized fresh checkout must initialize that exact submodule before loading the integration. The public HA repository records the dependency and tests its interface declarations. Actual native text, revision and transport contracts run inside the private repository against an explicit immutable public HA candidate SHA. Private implementation files and logs stay there; public CI does not claim it executed an unavailable private dependency.

## Deployment scope

These package definitions, host publication and Android dependency record are publication prerequisites. The production handoff separately compares each candidate runtime file with its current live preimage. Only the requested EKF routing, canonical target announcement and required package changes belong in that handoff. CI-only files and unrelated legacy lint maintenance are excluded. A fresh HA version and task-owned preimage check is required immediately before any runtime replacement.
