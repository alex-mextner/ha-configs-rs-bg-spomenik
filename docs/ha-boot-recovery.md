# Home Assistant boot recovery

The `ha-compose.service` oneshot preserves the normal Compose DNS health gate.
A temporary unhealthy DNS container no longer strands the unit in `failed`:
systemd retries a failed start after 30 seconds. A successful start remains
active and does not restart healthy Home Assistant.

`StartLimitIntervalSec=infinity` and `StartLimitBurst=5` impose a total five-start
budget since the counter was reset, not a rolling time window. This also bounds
slow failures when ExecStartPre and ExecStart use separate timeout budgets.
After five unsuccessful starts the fault stays visible, without an infinite
retry loop. Explicit manual starts also count. A reboot resets the counter;
after repairing a persistent fault, run `systemctl reset-failed ha-compose.service`
then `systemctl start ha-compose.service` to deliberately grant a fresh budget.

The retry is idempotent `ha_compose.sh up -d`, not a host reboot, container reset,
network reset, or print-job replay. Explicit stop/reload operations remain intact.

After installing the repository unit template, validate it with
`systemd-analyze verify /etc/systemd/system/ha-compose.service` and run
`systemctl daemon-reload`. Do not restart an active healthy unit merely to
clear an old status.

Verification includes unit regression tests and real transient systemd oneshots
that recover after two failures, and stop after five failures including slow
pre-start plus timeout-length startup. Test timing is accelerated; real host
reboot acceptance is recorded separately from these deterministic tests.
