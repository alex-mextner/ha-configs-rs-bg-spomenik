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
dry_run=0
no_start=0
dns_build_options=()
if [[ "${1:-}" == up ]]; then
  for arg in "$@"; do
    case "$arg" in
      --dry-run) dry_run=1 ;;
      --no-start) no_start=1 ;;
      --build|--no-build) dns_build_options+=("$arg") ;;
    esac
  done
fi
case "${1:-}" in
  up|create|run)
    if (( ! dry_run )); then
      python3 "$ROOT/scripts/ha_bluetooth_network_check.py" "${args[@]}"
    fi
    ;;
  -h|--help|--version|"") ;;
  -*) echo 'Pass the Compose command first; use deployment settings for global options.' >&2; exit 2 ;;
esac
if [[ "${1:-}" == up ]] && (( ! no_start && ! dry_run )); then
  # Reserve the static address and prove forwarding BEFORE other services can
  # be recreated. Apply requested build options before waiting on an old image.
  docker compose "${args[@]}" up -d --no-deps --wait --wait-timeout 90 "${dns_build_options[@]}" ha-docker-dns
fi
exec docker compose "${args[@]}" "$@"
