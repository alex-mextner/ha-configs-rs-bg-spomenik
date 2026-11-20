#!/usr/bin/env bash
# HA Fail-Safe Manager
# Usage: ./ha_fail_safe.sh [action]
# Actions: check, fix-orphaned, restart, status

set -e

LOG_DIR="/tmp/ha_fail_safe"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$(date +%Y%m%d_%H%M%S).log"
RESTART_HISTORY="$LOG_DIR/restart_history"
DATAPLICITY_RELOAD_HISTORY="$LOG_DIR/dataplicity_reload_history"
MAX_RESTARTS_PER_WINDOW="${HA_MAX_RESTARTS_PER_WINDOW:-3}"
RESTART_WINDOW_SEC="${HA_RESTART_WINDOW_SEC:-3600}"
DATAPLICITY_RELOAD_COOLDOWN_SEC="${DATAPLICITY_RELOAD_COOLDOWN_SEC:-900}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"
}

prune_restart_history() {
    local now cutoff tmp
    now=$(date +%s)
    cutoff=$((now - RESTART_WINDOW_SEC))
    tmp="$RESTART_HISTORY.tmp"

    if [ -f "$RESTART_HISTORY" ]; then
        awk -v cutoff="$cutoff" '$1 >= cutoff { print $1 }' "$RESTART_HISTORY" > "$tmp" || true
        mv "$tmp" "$RESTART_HISTORY"
    else
        : > "$RESTART_HISTORY"
    fi
}

recent_restart_count() {
    prune_restart_history
    wc -l < "$RESTART_HISTORY" | tr -d ' '
}

can_restart_ha() {
    if [ "${HA_RESTART_FORCE:-0}" = "1" ]; then
        return 0
    fi

    local count
    count=$(recent_restart_count)
    if [ "$count" -ge "$MAX_RESTARTS_PER_WINDOW" ]; then
        log "Restart limit reached: ${count}/${MAX_RESTARTS_PER_WINDOW} HA restarts in the last ${RESTART_WINDOW_SEC}s; skipping restart"
        return 1
    fi

    return 0
}

record_restart_attempt() {
    prune_restart_history
    date +%s >> "$RESTART_HISTORY"
}

get_dataplicity_url() {
    local config_file="/home/ultra/homeassistant/.storage/core.config"
    if [ -f "$config_file" ]; then
        local url=$(python3 -c "import json; data=json.load(open('$config_file')); print(data.get('data', {}).get('external_url', ''))" 2>/dev/null || echo "")
        if [ -n "$url" ] && echo "$url" | grep -q "dataplicity"; then
            echo "$url"
            return 0
        fi
    fi
    echo ""
}

DATAPLICITY_URL="${DATAPLICITY_URL:-$(get_dataplicity_url)}"
if [ -z "$DATAPLICITY_URL" ]; then
    log "⚠ Could not determine Dataplicity URL from core.config"
    DATAPLICITY_URL="https://spry-gazelle-4693.dataplicity.io/"
fi

# Probe the HA API with the long-lived token when available so health checks get
# 200 instead of flooding home-assistant.log with "invalid authentication" (401).
ha_auth_args() {
    local token
    token=$(sed -n 's/^HA_TOKEN=//p' /home/ultra/.env 2>/dev/null | tail -n 1)
    [ -n "$token" ] && printf '%s
' -H "Authorization: Bearer $token"
}

check_ha() {
    local url="${HA_URL:-http://127.0.0.1:8123}"
    local api_url="${url%/}/api/"
    local container="${HA_CONTAINER:-homeassistant-homeassistant-1}"
    local response="000"
    local health=""
    local attempt

    for attempt in 1 2 3; do
        response=$(ha_auth_args | xargs -d '
' curl -sS --connect-timeout 5 -m 12 -o /dev/null -w '%{http_code}' "$api_url" 2>/dev/null || true)
        if [ "$response" = "200" ] || [ "$response" = "401" ]; then
            log "✓ HA is accessible (HTTP $response)"
            return 0
        fi
        [ "$attempt" -lt 3 ] && sleep 2
    done

    health=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)
    if [ "$health" = "healthy" ]; then
        log "✓ HA API probe returned HTTP ${response:-000}, but container health is healthy"
        return 0
    fi

    log "✗ HA is NOT accessible (HTTP ${response:-000})"
    return 1
}

check_dataplicity() {
    local api_url="${DATAPLICITY_URL%/}/api/"
    local root_url="${DATAPLICITY_URL%/}/"
    local headers body response template subdomain
    local root_headers root_body

    root_headers=$(mktemp "$LOG_DIR/dataplicity_root_headers.XXXXXX")
    root_body=$(mktemp "$LOG_DIR/dataplicity_root_body.XXXXXX")
    curl -sS --connect-timeout 10 -m 20 -D "$root_headers" -o "$root_body" "$root_url" >/dev/null 2>&1 || true

    if grep -Eqi "Unknown domain|isn'?t registered|isn&#x27;t registered|not registered" "$root_body"; then
        subdomain=$(awk -F': ' 'tolower($1) == "wormhole-subdomain" { print $2; exit }' "$root_headers" | tr -d '\r')
        rm -f "$root_headers" "$root_body"
        log "✗ Dataplicity URL is not registered by Wormhole (${subdomain:-unknown subdomain}) for $root_url; update the Dataplicity/Wormhole URL. Restarting HA will not fix this."
        return 2
    fi

    if grep -qi '^wormhole-template:' "$root_headers"; then
        template=$(awk -F': ' 'tolower($1) == "wormhole-template" { print $2; exit }' "$root_headers" | tr -d '\r')
        rm -f "$root_headers" "$root_body"
        log "✗ Dataplicity tunnel returned wormhole template ${template:-unknown} for $root_url"
        return 1
    fi

    rm -f "$root_headers" "$root_body"

    headers=$(mktemp "$LOG_DIR/dataplicity_headers.XXXXXX")
    body=$(mktemp "$LOG_DIR/dataplicity_body.XXXXXX")
    response=$(ha_auth_args | xargs -d '
' curl -sS -L --connect-timeout 10 -m 20 -D "$headers" -o "$body" -w '%{http_code}' "$api_url" 2>/dev/null || true)

    if grep -Eqi "Unknown domain|isn'?t registered|isn&#x27;t registered|not registered" "$body"; then
        subdomain=$(awk -F': ' 'tolower($1) == "wormhole-subdomain" { print $2; exit }' "$headers" | tr -d '\r')
        rm -f "$headers" "$body"
        log "✗ Dataplicity URL is not registered by Wormhole (${subdomain:-unknown subdomain}) for $api_url; update the Dataplicity/Wormhole URL. Restarting HA will not fix this."
        return 2
    fi

    if grep -qi '^wormhole-template:' "$headers"; then
        template=$(awk -F': ' 'tolower($1) == "wormhole-template" { print $2; exit }' "$headers" | tr -d '\r')
        subdomain=$(awk -F': ' 'tolower($1) == "wormhole-subdomain" { print $2; exit }' "$headers" | tr -d '\r')

        rm -f "$headers" "$body"
        log "✗ Dataplicity tunnel returned wormhole template ${template:-unknown} for $api_url"
        return 1
    fi

    rm -f "$headers" "$body"

    case "$response" in
        200|401)
            log "✓ Dataplicity tunnel reaches HA API (HTTP $response)"
            return 0
            ;;
        000|"")
            log "✗ Dataplicity tunnel did not respond for $api_url"
            return 1
            ;;
        *)
            log "✗ Dataplicity tunnel returned unexpected HTTP $response for $api_url"
            return 1
            ;;
    esac
}

fix_dataplicity() {
    local dataplicity_status

    if check_dataplicity; then
        log "Dataplicity tunnel is already online"
        return 0
    else
        dataplicity_status=$?
    fi

    if [ "$dataplicity_status" -eq 2 ]; then
        log "Skipping HA restart because Dataplicity reports an unregistered Wormhole domain"
        return 2
    fi

    log "Attempting to fix dataplicity tunnel by reloading only the Dataplicity config entry..."
    if ! reload_dataplicity_entry; then
        log "✗ Dataplicity config entry reload failed"
        return 1
    fi

    log "Waiting 75s for dataplicity m2m to reconnect..."
    sleep 75
    if check_dataplicity; then
        log "✓ Dataplicity tunnel recovered after config entry reload"
        return 0
    fi

    log "✗ Dataplicity tunnel still offline after config entry reload"
    return 1
}

reload_dataplicity_entry() {
    local entry_id token response body_file status now last_reload age
    body_file=$(mktemp "$LOG_DIR/dataplicity_reload_body.XXXXXX")

    now=$(date +%s)
    last_reload=$(tail -n 1 "$DATAPLICITY_RELOAD_HISTORY" 2>/dev/null || true)
    if [ -n "$last_reload" ]; then
        age=$((now - last_reload))
        if [ "$age" -lt "$DATAPLICITY_RELOAD_COOLDOWN_SEC" ]; then
            log "Dataplicity reload cooldown active (${age}s < ${DATAPLICITY_RELOAD_COOLDOWN_SEC}s); skipping reload"
            rm -f "$body_file"
            return 1
        fi
    fi

    entry_id=$(python3 - << 'PYEOF'
import json

with open("/home/ultra/homeassistant/.storage/core.config_entries") as f:
    data = json.load(f)

for entry in data["data"]["entries"]:
    if entry.get("domain") == "dataplicity":
        print(entry["entry_id"])
        break
PYEOF
)
    if [ -z "$entry_id" ]; then
        log "Dataplicity config entry not found"
        rm -f "$body_file"
        return 1
    fi

    token=$(sed -n 's/^HA_TOKEN=//p' /home/ultra/.env | tail -n 1)
    if [ -z "$token" ]; then
        log "HA_TOKEN not found in /home/ultra/.env"
        rm -f "$body_file"
        return 1
    fi

    response=$(curl -sS --connect-timeout 5 -m 30 \
        -X POST \
        -H "Authorization: Bearer $token" \
        -H "Content-Type: application/json" \
        -d "{\"entry_id\":\"$entry_id\"}" \
        -o "$body_file" \
        -w '%{http_code}' \
        http://127.0.0.1:8123/api/services/homeassistant/reload_config_entry 2>/dev/null || true)

    status=1
    if [ "$response" = "200" ]; then
        log "✓ Dataplicity config entry reload requested"
        echo "$now" >> "$DATAPLICITY_RELOAD_HISTORY"
        status=0
    else
        log "✗ Dataplicity config entry reload returned HTTP ${response:-000}: $(cat "$body_file")"
    fi

    rm -f "$body_file"
    return "$status"
}

fix_orphaned() {
    local registry="/home/ultra/homeassistant/.storage/core.entity_registry"
    local backup="$LOG_DIR/entity_registry.backup.$(date +%Y%m%d_%H%M%S)"

    if [ ! -f "$registry" ]; then
        log "Entity registry not found"
        return 1
    fi

    log "Creating backup: $backup"
    cp "$registry" "$backup"

    log "Finding orphaned TTS entities..."

    python3 << 'PYEOF'
import json
import sys

registry_path = "/home/ultra/homeassistant/.storage/core.entity_registry"
with open(registry_path, 'r') as f:
    data = json.load(f)

entities = data["data"]["entities"]
orphaned_tts = [e for e in entities if e.get("entity_id", "").startswith("tts.") and e.get("orphaned_timestamp")]

if orphaned_tts:
    print(f"Found {len(orphaned_tts)} orphaned TTS entities:")
    for e in orphaned_tts:
        print(f"  - {e['entity_id']} (unique_id: {e.get('unique_id')}, config_entry_id: {e.get('config_entry_id')})")

    # Remove orphaned entries that have no config_entry_id
    before = len(entities)
    entities = [e for e in entities if not (e.get("entity_id", "").startswith("tts.") and e.get("orphaned_timestamp") and not e.get("config_entry_id"))]
    after = len(entities)

    if before != after:
        data["data"]["entities"] = entities
        with open(registry_path, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"Removed {before - after} orphaned TTS entities")
        print("SUCCESS")
    else:
        print("No orphaned entities to remove")
else:
    print("No orphaned TTS entities found")
PYEOF

    local result=$?
    if [ $result -eq 0 ]; then
        log "Orphaned entities cleaned successfully"
        return 0
    else
        log "Failed to clean orphaned entities"
        return 1
    fi
}

restart_ha() {
    local container="${HA_CONTAINER:-homeassistant-homeassistant-1}"

    can_restart_ha || return 1

    log "Checking HA before restart..."
    check_ha || log "HA already down, proceeding with restart"

    log "Restarting container: $container"
    record_restart_attempt
    if ! docker restart "$container" 2>&1 | tee -a "$LOG"; then
        log "✗ Docker restart command failed"
        return 1
    fi

    log "Waiting for HA to come back (max 120s)..."
    local count=0
    while [ $count -lt 60 ]; do
        if check_ha; then
            log "✓ HA is back online after restart"
            return 0
        fi
        sleep 2
        count=$((count + 1))
        echo -n "."
    done

    log "✗ HA failed to come back after restart"
    return 1
}

status_report() {
    echo "=== HA Fail-Safe Status Report ==="
    echo "Time: $(date '+%Y-%m-%d %H:%M:%S')"
    echo ""

    echo "HA Accessibility:"
    check_ha && echo "Status: ONLINE" || echo "Status: OFFLINE"
    echo ""

    echo "Dataplicity Tunnel:"
    check_dataplicity && echo "Status: ONLINE" || echo "Status: OFFLINE"
    echo ""

    echo "Orphaned Entities:"
    python3 << 'PYEOF'
import json
registry_path = "/home/ultra/homeassistant/.storage/core.entity_registry"
try:
    with open(registry_path, 'r') as f:
        data = json.load(f)
    entities = data["data"]["entities"]
    orphaned = [e for e in entities if e.get("orphaned_timestamp")]
    print(f"  Total orphaned: {len(orphaned)}")
    tts_orphaned = [e for e in orphaned if e.get("entity_id", "").startswith("tts.")]
    if tts_orphaned:
        print(f"  TTS orphaned: {len(tts_orphaned)}")
        for e in tts_orphaned:
            print(f"    - {e['entity_id']}")
except Exception as e:
    print(f"  Error: {e}")
PYEOF
    echo ""

    echo "TTS Pipeline Status:"
    python3 << 'PYEOF'
import json
pipeline_path = "/home/ultra/homeassistant/.storage/assist_pipeline.pipelines"
registry_path = "/home/ultra/homeassistant/.storage/core.entity_registry"

try:
    with open(pipeline_path, 'r') as f:
        pipelines = json.load(f)["data"]["items"]
    with open(registry_path, 'r') as f:
        registry = json.load(f)["data"]["entities"]

    for p in pipelines:
        tts = p.get("tts_engine")
        if tts:
            entity = next((e for e in registry if e["entity_id"] == tts), None)
            if entity:
                status = "✓ OK" if entity.get("config_entry_id") and not entity.get("orphaned_timestamp") else "✗ BROKEN"
                print(f"  {p['name']}: {tts} [{status}]")
            else:
                print(f"  {p['name']}: {tts} [✗ NOT FOUND]")
        else:
            print(f"  {p['name']}: (no TTS configured)")
except Exception as e:
    print(f"  Error: {e}")
PYEOF
    echo ""

    echo "Container Status:"
    docker ps --filter name=homeassistant-homeassistant --format "{{.Status}}"
}

case "${1:-status}" in
    check)
        check_ha
        check_dataplicity
        ;;
    check-ha)
        check_ha
        ;;
    check-dataplicity)
        check_dataplicity
        ;;
    fix-orphaned)
        fix_orphaned
        ;;
    fix-dataplicity)
        fix_dataplicity
        ;;
    fix-all)
        log "Running full fix cycle..."
        if check_ha; then
            if ! check_dataplicity; then
                log "Dataplicity check failed while local HA is online; running dataplicity fix"
                fix_dataplicity || true
            fi
        else
            fix_orphaned || true
            restart_ha || true
        fi
        ;;
    restart)
        restart_ha
        ;;
    status)
        status_report
        ;;
    *)
        echo "Usage: $0 {check|check-ha|check-dataplicity|fix-orphaned|fix-dataplicity|fix-all|restart|status}"
        exit 1
        ;;
esac
