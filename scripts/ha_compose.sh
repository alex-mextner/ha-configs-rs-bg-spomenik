#!/usr/bin/env bash
# The single Compose entrypoint for deployment, boot and manual maintenance.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$ROOT"
ENV_FILE=${HA_COMPOSE_ENV_FILE:-/etc/default/ha-compose}
# Root-owned local deployment settings; contains paths, never HA credentials.
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$ENV_FILE"
fi
args=(--project-name "${HA_COMPOSE_PROJECT_NAME:-homeassistant}" --project-directory "$ROOT" -f "$ROOT/ha.docker-compose.yaml")
IFS=: read -r -a overlays <<< "${HA_COMPOSE_OVERLAYS:-}"
for overlay in "${overlays[@]}"; do
  [[ -n "$overlay" ]] || continue
  if [[ ! -f "$overlay" ]]; then
    echo "Missing Compose overlay: $overlay" >&2
    exit 2
  fi
  args+=(-f "$overlay")
done
args+=(-f "$ROOT/docker/bluetooth/compose.final.yaml")
# Machine/global options belong in the settings or COMPOSE_PROFILES. Requiring
# the verb first also prevents an `exec SERVICE run` payload from acting as `run`.
case "${1:-}" in
  up|create|run)
    python3 "$ROOT/scripts/ha_bluetooth_network_check.py" "${args[@]}"
    ;;
  -h|--help|--version|"") ;;
  -*) echo 'Pass the Compose command first; use deployment settings for global options.' >&2; exit 2 ;;
esac
if [[ "${1:-}" == up && " $* " != *" --no-start "* ]]; then
  # Reserve the static address and prove forwarding BEFORE other services can
  # be recreated. A concurrent claimant can only fail this isolated DNS step.
  docker compose "${args[@]}" up -d --no-deps --wait --wait-timeout 90 ha-docker-dns
fi
exec docker compose "${args[@]}" "$@"
