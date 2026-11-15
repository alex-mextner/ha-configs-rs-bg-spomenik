# Fireplace and YouTube on the living-room projector

The existing `script.living_room_fireplace` remains the voice entry point: «включи камин на проекторе». It uses the existing paired projector/Kodi power action and native `media_player.home` entity. Commands are queued, with a maximum of three pending/running executions.

## Existing power integration prerequisite

This package extends the installed living-room power integration. Before any power action, it requires `media_player.projector.power_intent_revision`, `binary_sensor.projector_hdmi_connected`, and `sensor.kodi_service_state`. The existing `script.source_to_pc` must control the paired projector/Kodi route; the projector also exposes `last_power_intent`, `power_sync_target`, and `power_sync_error`. The older TV-only configuration in a fresh checkout does not supply this contract. The fireplace script aborts before calling that legacy route when the capability is absent, and its HA regression covers this boundary. Deployment must verify the producing integration in the active installation and preserve it.

The manifest and HA endpoints target the existing bridge at `172.20.0.1:8787`. A different bridge bind requires updating the HA URLs, resolver and adapter together; this feature does not silently retarget another host.

## Source selection and evidence

The controller selects from `scripts/host/kodi_fireplaces.json`. It excludes the last successfully played source and any currently owned source, shuffles eligible YouTube videos, tries at most two, and then tries only explicitly eligible licensed local alternatives. An entry with `selectable: false` is retained for attribution or old ownership recognition and is never selected. At this candidate checkpoint all local entries are disabled; failure of both YouTube attempts reports an unconfirmed/failed launch instead of opening a rejected clip. Resolver failures never update selection history. Successful history is written atomically only after Kodi reports the expected media, repeat-one, and an advancing playback position. Restarting the bridge or stopping playback preserves that history.

A source is an independent video, not another resolution of the same upload. The YouTube catalog contains Fireplace 4K (`2rkB6CNfqJ4`), Evenfall Studio (`W1NI3lfdMug`), and Fire of Life (`kBlOvm_kiCk`), published as crackling fireplaces without music. Availability is checked when requested. They are streamed through public YouTube watch URLs and are not redistributed as local assets.

AISIS supplies a `request_id`; direct HA/UI and Yandex script calls generate one. The controller publishes `starting` before extraction and keeps status reads responsive. A `playing` receipt includes that request ID, `launch_started_at`, fresh `observed_at`, `source_id`, `source_kind`, actual native title, `owned`, `file_matches`, `repeat`, and `progressing`. HA and AISIS cross-check the title against native Kodi state. Polling an old video cannot confirm a new request.

An uncertain Player.Open or stop outcome produces `unknown`, not a definitive failure or an automatic duplicate Open. Definitive source failure produces `error` with a fixed code. An identified but stalled owned source is stopped and confirmed released before another candidate opens. Preparation has a 20-second subprocess deadline; a complete host launch has a 50-second budget plus bounded cleanup. The HA request timeout is 60 seconds. The cold projector/Kodi readiness wait remains 45 seconds; voice may report pending before physical power and playback finish.

## Maintained playback components

| Component | Version / revision | Purpose | License |
| --- | --- | --- | --- |
| Kodi | Debian `2:21.2+dfsg-4`, ARM64 | Native player | GPL-2.0-or-later |
| InputStream Adaptive | `21.5.25-Omega`, `ddc857b58094ea8542416b792a9abe6176320065` | DASH video and audio | GPL-2.0-or-later |
| Kodi addon SDK | `21.2-Omega`, `d1a1d48c3cb3722d39264ffdd8132f755ffecd27` | Native package ABI | GPL-2.0-or-later |
| SendToKodi | `0.10.3` | Managed extractors and pure DASH builder | MIT, copyright Tristan Teufel |
| yt-dlp | managed `2026.10.26` at initial deployment | Public YouTube extraction | Unlicense; bundled components retain their licenses |
| Deno | managed `v2.9.8` at initial deployment | YouTube JavaScript support | MIT |
| SmartTube for Kodi | `0.5.3`, `97520cdb1c37ad78a3c3346b867bfed68c12de2a` | Native public search, HD opening, and fireplace preparation | GPL-2.0-only |

Upstream: [Kodi](https://github.com/xbmc/xbmc), [InputStream Adaptive](https://github.com/xbmc/inputstream.adaptive), [SendToKodi](https://github.com/firsttris/plugin.video.sendtokodi), [yt-dlp](https://github.com/yt-dlp/yt-dlp), [Deno](https://github.com/denoland/deno), [SmartTube](https://github.com/primez-x/plugin.video.smarttube).

Open SmartTube in Kodi's video addons to search YouTube without a new API key. Its routes are `plugin://plugin.video.smarttube/search/?q=<query>` and `plugin://plugin.video.smarttube/play/?video_id=<id>`. Its maintained adaptive service handles selection. Native browsing is independent of the fireplace's curated source pool.

## Prepared playback and cancellation

`ha_kodi_youtube_resolve.py` runs outside Kodi in a separately owned process group. It imports the unchanged, checksum-pinned SmartTube 0.5.3 metadata and DASH modules from their verified source buffers. The raw provider response must identify the requested video before parsing. The helper requires H264 video at 720/1080p plus non-DRC AAC and creates a private MPD. Each provider HTTP read has an eight-second timeout within the complete 20-second preparation budget. Timeout cleanup terminates and reaps the whole helper process group. The fireplace preparation path does not invoke yt-dlp or Deno. Kodi's tiny `plugin.video.hayoutube` adapter performs no extraction; it checks the current attempt generation immediately before resolving the already prepared stream.

InputStream Adaptive requires HTTP delivery of manifests. The existing host bridge serves only `GET /kodi/youtube/manifest?request_id=<attempt UUID>`, with its existing `X-HA-Host-Token` authentication. It verifies the current generation, fixed private path, size, and SHA256. There are no arbitrary filenames, upstream proxying, redirects, or new listeners. The host token appears only in ISA manifest headers; ordinary allowlisted YouTube headers appear only in media stream headers. The adapter rejects redirect/xlink manifests and media origins outside HTTPS Googlevideo.

The public voice request ID and private per-source attempt UUID have different purposes. Cancelling an attempt changes `~/.config/ha-host-actions/youtube/current-request.json`. The helper refuses late publication, the adapter refuses stale resolution, and the HTTP bridge refuses a stale MPD. The private descriptor and MPD live under `youtube/requests/<attempt UUID>/`; obsolete owned requests are bounded and cleaned without deleting the current generation, durable active/pending attempts, or unknown directories. Missing or unreadable selection history disables deletion, and protection is checked again immediately before removal.

Durable selection state is `~/.config/ha-host-actions/fireplace-selection.json`. It keeps the active, verified source separate from a pending preparing/prepared/opening candidate. The preparing record is durable before its generation becomes current. A crash before Open preserves the previous source and restores its manifest generation; a crash after Open requires fresh matching playback and progression before promotion when a valid repeat lease exists; an unleased pending candidate is fenced and stopped by exact identity. A definitively failed preparation also restores the previous generation. Explicit stop records durable cancellation and an intent sequence in the generation file; bridge close/restart does not invent a user stop. A newer start supersedes an older stop still waiting for the controller lock. Cancellation or an uncertain Open never revives an older attempt. Progress evidence must come from the same source, attempt and media identity, and final success publication rechecks stop intent after the durable history write. The existing `fireplace-owner.json` keeps prior repeat ownership for safe restoration. Selection state contains source IDs, timestamps, titles, and file hashes, not signed provider URLs. Provider URLs occur only in private, temporary playback files. Deleting selection history defeats the no-adjacent-repeat guarantee; repair an unreadable file explicitly rather than silently resetting it.

YouTube URLs may expire, disappear, or be restricted. A short live check proves startup and progression; it does not promise uninterrupted playback for the full advertised 10/12-hour upload. Each new command prepares fresh URLs, and source availability is evaluated afresh. A pair of quality-approved distinct local fallbacks is still pending; the stored unapproved clips do not satisfy that acceptance criterion. Do not redownload YouTube videos as an assumed freely licensed fallback.

## Stored local assets and selection status

All files reside in `/home/ultra/KodiLibrary/Ambience`. They are currently excluded from new selection. Fastily is rejected for picture/audio quality; Hotel is a short, sideways, obstructed shot. Fantastic has a clear picture, but sound quality has not been accepted. Keeping the existing files preserves attribution and current playback; it is not approval to select them. Exact bytes, source URLs, authors, and licenses are in the catalog; verify SHA256 before deployment.

| File | Attribution | License | SHA256 |
| --- | --- | --- | --- |
| `fireplace-fastily-1080p.webm` | FASTILY, [Fireplace 1 2021-02-14](https://commons.wikimedia.org/wiki/File:Fireplace_1_2021-02-14.webm) | CC BY-SA 4.0 | `248cc597abdf1e53e3d55ab998e1aadfff3c96f7c48ca2ce30362b74d5959267` |
| `fireplace-fantastic-1080p.webm` | free video library, [Fantastic fireplace](https://commons.wikimedia.org/wiki/File:Fantastic-fireplace-fire-chimney-hearth-_background_-_texture_-_motion_graphics_-_free_video_library.webm) | CC BY 3.0, using the explicit Commons license | `0d8094ccd902b986c16d453d7167664536b51978607a2c737e76f0220ce6bea5` |
| `fireplace-hotel-1080p.webm` | BrillLyle, [ABOedit2015-Video](https://commons.wikimedia.org/wiki/File:ABOedit2015-Video.webm) | CC0 1.0 | `484a6e8e11de5aa71fcd343a26602a833db330e57039366013d29d96bbe76466` |

The downloaded bytes are preserved without further editing or transcoding. The existing Fastily file is Wikimedia's published 1080p transcode; the two additions are the original uploaded files. Retain these attributions when copying the fallback assets. The two additional clips depict different scenes; cropped or alternate-resolution copies of one scene do not count as variety.

## Validation and deployment

`.github/workflows/kodi-inputstream-portable.yml` builds the pinned upstream native addon on GitHub's ARM64 runner, runs upstream tests, and verifies every packaged ELF against the observed Odroid runtime and Kodi API ranges. Artifacts include the ZIP, relative checksum, licenses, provenance, target baseline, ELF verification, and automatic private logs. Install only a successful matching artifact; system package/loader overrides are unnecessary.

`.github/workflows/kodi-fireplace.yml` runs the host regression suite and executes the actual fireplace YAML through Home Assistant 2026.9.3. Its contract fixture downloads immutable, checksum-verified SmartTube 0.5.3 modules and exercises the real provider parser and DASH builder through the resolver, authenticated reader, and Kodi adapter. A separate contract retains coverage of the installed SendToKodi 0.10.3 builder and selector. `.github/workflows/ha-regressions.yml` runs the unchanged `make test-regressions` gate; the local shipping command verifies successful native receipts for the maintained, fireplace and Ruff workflows and every required job, rejecting a dirty worktree/index or a changed commit. All three workflows run on pushed branches and applicable pull requests; only exact-commit push receipts satisfy the shipping helper. Unit/HA tests and builds run only in genuine CI or managed Runpod. Local PCs and household devices are used for editing, read-only diagnostics, deployment, and bounded live playback checks.

Production has unrelated local commits and active HA changes. Deploy only these fireplace files, addon files, and new media assets after backing up their exact prior contents. Patch the existing bridge's fireplace/manifest handlers without replacing unrelated live functions. Reload the changed HA rest command, REST sensor, and script through supported services. Read projector/Kodi state before live checks; preserve human playback and restore only devices/volume changed by the check.
