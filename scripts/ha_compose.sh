#!/usr/bin/env bash
# The single Compose entrypoint for deployment, boot and manual maintenance.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
ENV_FILE=${HA_COMPOSE_ENV_FILE:-/etc/default/ha-compose}
# Root-owned local deployment settings; contains paths, never HA credentials.
if [[ -f "$ENV_FILE" ]]; then
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
exec docker compose "${args[@]}" "$@"
