# Music Assistant Home Assistant reconnection

The deployed MA 2.10.4 image includes hass-client 1.3.1. Two independent
WebSocket failure paths need a managed backport:

1. A heartbeat timeout or connection reset can arrive as an ERROR frame without
   a close code. The original client dereferences `error.code` and raises
   AttributeError before reconnection can run.
2. HA can close its authentication handshake with code 1000. aiohttp raises
   `WSMessageTypeError` (TypeError, not ClientError). The original client does
   not classify it as reconnectable; MA skips cleanup, leaves
   `hass/search_control_entities` registered, and stops retrying. Later reloads
   fail because the command is already registered.

The second path was observed on 2026-11-19 at 17:05:52 UTC. On November 23 one
provider-only reload confirmed the leak. One graceful MA-only restart restored
both HA providers and the two Stations. Chromecast and AirPlay queue counts,
indexes and positions survived. HA and credentials were unchanged.

## Managed fixes

- The ERROR-frame guard backports [python-hass-client ab1bd370](https://github.com/music-assistant/python-hass-client/commit/ab1bd37043a0447b526654112b9d8594b12757de).
- The handshake guard maps only `WSMessageTypeError` to `CannotConnect`, matching
  the transport classification proposed in [python-hass-client #298](https://github.com/music-assistant/python-hass-client/pull/298).
  Unrelated TypeError exceptions remain programming errors.
- Cleanup follows [MA #7844](https://github.com/music-assistant/server/pull/7844),
  and also handles cancellation: unregister the command, disconnect, propagate.
- An actual HA `auth_invalid` response is translated to MA AuthenticationFailed,
  which its existing retry controller excludes. Network failures use MA's
  existing 120-second retry. No second retry loop is introduced.

## Reproducibility and interruption safety

Compose retains the deployed MA 2.10.4 ARM64 image and its digest. This directory
is mounted read-only. Before each startup the wrapper validates and patches the
installed modules, then execs the original MA command. It never downloads
dependencies or reads household credentials or runtime databases.

Both inputs are validated before either file changes. Each replacement uses a
sibling temporary file, fsync and atomic replacement, preserving permissions.
An interrupted replacement leaves complete old or new source. Original,
previous-guard and fully patched sources are recognized; unexpected source
aborts startup visibly.

| File state | SHA256 |
| --- | --- |
| Original hass-client client.py | 0b464086da1215a14250e72c144a7a57acceaa38f863337a939d4588cf04fd55 |
| Previous ERROR-frame-only guard | dc7af40887c9c48c16a5e425b1a637734718614870a26066d8c0f5248d0df0f5 |
| Previous client with both transport fixes | 9cfde978bd240782b78f79ca8ee5fc8b7614e9820298cac5a942f80c597c79ca |
| Client with transport fixes and exact HA provenance | 997ab3638eb045f9ace6284db436ba2c79f67842275612fbe424756497e53c14 |
| Original MA HA provider | 600bd557f3549e0c412341f85d51eba96ec1e7d0412b4ae85ae28ec3f13abe1b |
| Provider with cleanup and auth mapping | bfea362c3d13c82bc9ced0786c09952ffbdd2f75fd31ae054157c0c94589a6aa |

The original hass-client 1.3.1 wheel has SHA256
`81a5bda47b32d699423c7ac94bf1da774711c4808390d7e083585e8aa68858d0`.
Package metadata stays 1.3.1; hashes identify the managed source.

The image command remains
`/usr/local/bin/entrypoint.sh --data-dir /data --cache-dir /data/.cache`
(image Cmd is null). The wrapper finishes with `exec "$@"`. Data, plugins,
networking, resource limits and signal delivery remain as configured.

## Verification

The `Music Assistant reconnect` workflow uses the pinned image on a native
GitHub ARM64 runner. Its disposable test container has only loopback networking,
no household data and a read-only source mount. It copies untouched image
modules as temporary fixtures, runs the real wrapper, then the regressions.
CI preserves a complete sanitized execution log.

- `test_error_frames.py`: actual listener with heartbeat/reset/protocol errors
  and the distinct message-too-large exception.
- `test_handshake.py`: real local aiohttp closure before greeting and after
  authentication; real auth_invalid; unrelated TypeError; provider cleanup,
  cancellation, subsequent initialization and actual MA loader retry rules.
- `test_patch_guard.py`: original/v1 migration, idempotence, unknown-source
  refusal, both-input preflight, interrupted atomic replacement and file modes.

The handshake cases fail on the original deployed source and pass against
isolated patched copies. No household HA connection or audio is used.
Regression files live in tests/music_assistant_reconnect/. Direct runners and
the patcher write private /tmp logs.

## Deployment and rollback

Deploy only the reviewed wrapper directory. A currently wrapped installation
needs one graceful MA restart. First-time wrapper installation recreates only MA:

```sh
docker compose -f ha.docker-compose.yaml up -d --no-deps --no-build music-assistant
```

Verify configured `hass`/`hass_players` have no last_error, both running providers
are available, expected HA players are present, and queues survive. Do not
create a household HA outage to test the handshake case.

Rollback restores the previous reviewed wrapper and recreates only the pinned
MA container. Restoring the old wrapper and merely restarting cannot undo
already patched modules. Do not edit settings or databases to clear errors.

## Retiring the wrapper

Do not infer retirement from a version number. hass-client 1.4.1 contains the
ERROR-frame fix but does not prove handshake, cleanup, cancellation or auth
retry behavior. Verify all five behaviors in the exact replacement image and
run these regressions. Then remove the mount and entrypoint override together.
Review its digest, Python paths and original entrypoint in the same upgrade.

## Playback intent telemetry

The same pinned startup wrapper installs a complete-source-checked private hook
at authenticated HTTP and WebSocket command dispatch. Parsed, authorized controls
produce a minimal pending server-log record before effects; successful handlers
produce an accepted record. An offline but authorized Stop still cancels recovery.
Invalid/private/unauthenticated requests produce no authorized intent. No target
identities are broadcast to restricted users or guests.

The event bridge correlates only its own unpredictable resume message ID with a
private local claim. Speech commands never count as user playback intent. Full
original, transformed controller and helper hashes are checked before any write;
an unexpected image fails visibly instead of receiving a partial blind patch.
The native CI job exercises the actual pinned HTTP/WS handlers and announcement
restoration without accessing household devices.

The guarded `hass_client.call_service` method also records a successful HA
response's exact context ID, known media-player service and normalized entity
targets. This optional helper never changes the returned object or a service
exception. It emits no token, service arguments, user ID or raw response. The
consumer handles either log/event order, same-context nested services, and an
unrelated owner Stop conservatively. The original, ERROR-only and previous
reconnect-only client preimages all migrate to the same reviewed output. Native
tests exercise real client return identity, failure propagation, missing helper,
invalid fields and logging failure.
