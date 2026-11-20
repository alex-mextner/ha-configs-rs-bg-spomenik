#!/usr/bin/env bash
# Wrapper for cron - runs check and auto-fixes if needed
# Anti-flap: skip restart if HA container started < 10 minutes ago
LOG=/tmp/ha_check.log
UPTIME_SEC=$(docker inspect --format='{{.State.StartedAt}}' homeassistant-homeassistant-1 2>/dev/null | xargs -I{} python3 -c "import datetime,sys; d=datetime.datetime.fromisoformat('{}'.replace('Z','+00:00')); print(int((datetime.datetime.now(datetime.timezone.utc)-d).total_seconds()))")
if [ -n "$UPTIME_SEC" ] && [ "$UPTIME_SEC" -lt 600 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] HA container up only ${UPTIME_SEC}s (< 10 min), skipping check to prevent restart loop" >> "$LOG"
    exit 0
fi

if ! /home/ultra/homeassistant/scripts/ha_fail_safe.sh check-ha >> "$LOG" 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Local HA check failed, running fix-all..." >> "$LOG"
    /home/ultra/homeassistant/scripts/ha_fail_safe.sh fix-all >> "$LOG" 2>&1
    exit $?
fi

# Require two consecutive failed probes (cron runs every 5 min) before reloading
# the Dataplicity entry: single blips (e.g. the nightly router reconnect at 03:00)
# used to trigger needless reloads, each of which leaks an m2m client thread.
DP_FAILS=/tmp/ha_fail_safe/dataplicity_consecutive_failures
if /home/ultra/homeassistant/scripts/ha_fail_safe.sh check-dataplicity >> "$LOG" 2>&1; then
    rm -f "$DP_FAILS"
else
    fails=$(( $(cat "$DP_FAILS" 2>/dev/null || echo 0) + 1 ))
    echo "$fails" > "$DP_FAILS"
    if [ "$fails" -lt 2 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Dataplicity check failed ($fails in a row); waiting for next run before fixing" >> "$LOG"
        exit 0
    fi
    rm -f "$DP_FAILS"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Dataplicity check failed $fails times in a row while local HA is online; running dataplicity fix..." >> "$LOG"
    /home/ultra/homeassistant/scripts/ha_fail_safe.sh fix-dataplicity >> "$LOG" 2>&1
    exit $?
fi
