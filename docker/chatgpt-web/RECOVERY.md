# Shared ChatGPT browser lifecycle and session recovery

Related issue: #9. This is an opt-in integration, not a verified production deployment.
The shared source belongs to `agent-tools/lib/agenttools_chatgpt`; no library copy is vendored
here. The current Forgejo library and pm-cli must be reconciled and reviewed before activation.

## What was actually found

The original bridge launches Chrome through Playwright, installs a clipboard hook across all
pages, always navigates its first restored tab to ChatGPT home, and waits for that navigation
before starting HTTP. A navigation timeout can therefore terminate startup. New conversations
that encounter a human check are closed, even though the user is asked to complete that check.
The monitor also has a periodic reload based on cached readiness.

These are code-level lifecycle defects. They do NOT establish the cause of the reported live
Cloudflare verification loop. Browser/network/profile rejection remains possible. No production
logs, Ray ID, browser trace, or live manual challenge completion was available for this change.
No CAPTCHA solver, fingerprint disguise, cookie export, private API replay, or proxy rotation
is part of this implementation.

## Modes and installation

The default `CHATGPT_WEB_BROWSER_MODE=legacy` keeps the existing deployment behavior. The new
launcher imports the shared library only in the explicitly selected modes below.

Mount the reviewed **agent-tools lib directory** read-only at `/opt/agent-tools/lib`. The image
already supplies optional runtime dependencies. Shared library requires Python >=3.11,
Playwright >=1.60 (CDP `no_defaults=True`), and aiohttp >=3.12. Do not publish CDP port 9222 or
remove the existing loopback-only noVNC restrictions.

Add to the existing service's configuration (merge with its current environment and volumes;
do not replace its networking, profile volume, API key, HA token, or restart policy):

```yaml
environment:
  CHATGPT_WEB_BROWSER_MODE: manual
volumes:
  - ${AGENT_TOOLS_LIB:?set the reviewed agent-tools/lib absolute path}:/opt/agent-tools/lib:ro
```

**manual** starts ordinary headed Chrome with the persistent profile, without CDP or Playwright
attachment. Complete login/checks yourself in the existing noVNC view. API requests return 503
`manual_login`, while `/health` remains available. There is no automated clicking or page reload.
To compare with managed mode, stop Chrome cleanly and change the mode, retaining the same
profile and network. Do not run two Chrome processes against one profile.

**managed** starts ordinary headed Chrome and attaches through loopback CDP without context
overrides. Restored tabs are preserved; auth/check tabs are never reloaded by the monitor or
closed by failed conversation creation. A navigation timeout preserves the browser and does not
tear down HTTP. The adapter reads rendered answer markdown rather than patching the clipboard.
This can change formatting compared with the old copy-button extraction; verify tool-call replies.

If the loop persists in manual mode, capture only non-secret evidence (time, browser version,
visible challenge code/Ray ID, failing request host/status; never cookies, Authorization headers
or tokens). Check the ordinary browser, JavaScript/cookies and network according to OpenAI's
login troubleshooting. Do not keep hammering the check or relaunching the container.

## Optional guardian in the same process

After the shared library is present, create a private token inside the persisted data volume:

```sh
python -m agenttools_chatgpt --token-file /data/guardian.token init
```

Configure `CHATGPT_GUARDIAN_ENABLED=1` in managed mode. Default behavior is **observe-only**.
Only after a live UI test, explicitly set `CHATGPT_GUARDIAN_APPLY=1` to allow sends. State lives
in `/data/guardian.sqlite3`. API prompts and recovery share the same asynchronous lock/browser;
do not start an independent pm service against this same browser. `pm chatgpt` is a control client.
The authenticated guardian control listener is container-loopback-only on port 8790. Use an
in-container CLI or an authenticated local tunnel; it is deliberately not exposed to the LAN.

Within the container:

```sh
python -m agenttools_chatgpt --token-file /data/guardian.token status
python -m agenttools_chatgpt --token-file /data/guardian.token watch https://chatgpt.com/c/CONVERSATION_ID
python -m agenttools_chatgpt --token-file /data/guardian.token allow-sol https://chatgpt.com/c/CONVERSATION_ID on
```

Open the target conversations manually in that browser. Only registered, currently open tabs
are observed; this does not scan account history or resurrect closed tabs. Temporary HA API chats
are not automatically registered. Astra quota blocks Astra globally in this browser/account;
Sol requires explicit consent and verified Extra High. A manual stop, draft, attachment, active
answer, completed turn or ambiguous UI never authorizes a nudge. Uncertain sends pause that chat.

## Verification / rollback

Run the existing bridge tests plus launcher tests inside the built image:
`python -m pytest -q /app/test_bridge.py /app/test_launcher.py`.
Run the shared recovery suite from its delivery bundle. A local fixture pass is not a live
ChatGPT pass. Validate a disposable failed conversation, quota pause, model/effort selectors and
manual login before enabling sends. The Docker image and live HA integration were not built or
run in the preparation environment. Revert to `legacy` to restore the old execution path;
`CHATGPT_GUARDIAN_APPLY=0` stops recovery writes without changing the profile or stored consent.
