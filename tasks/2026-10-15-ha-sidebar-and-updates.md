# HA sidebar migration and Git-managed UI updates

Goal: restore admin-only Ubuntu/ChatGPT noVNC access from HA on Odroid; make HACS Update use Git submodules, preserve forks and commit exact pins; update compatible integrations.

Observed: live HA is /home/ultra/homeassistant on odroidn2, revision 6522947, with unrelated local edits. Native GUI service is disabled and its WD500 profile volume is absent. Backend and allowlist pin the old same-host Docker addresses. Existing updater edits .storage and kills HA, violating current safety requirements.

Design: configurable private Tailnet backend (VNC remains loopback), explicit RAM-only temporary desktop profile while WD500 is disconnected, supervised services, admin auth retained. A checked-in HACS-fork installation hook invokes a transactional submodule installer; normal HACS post-install updates runtime metadata. Exact refs, fork policy, compatible HA versions, dirty-tree protection, backups, rollback, git-pointer commits. Never modify .storage directly.

Checks: baseline suite, failing regression tests, unit/integration tests, live anonymous denial and authorized HTML/WebSocket/framebuffer, real HA update.install calls, safe configuration validation and one restart. Preserve concurrent changes and existing ChatGPT guardian sessions.

Status: diagnosis complete; implementation in isolated worktree. Baseline pytest collection currently fails; details in /tmp/ha-sidebar-baseline.log. No production changes yet.
