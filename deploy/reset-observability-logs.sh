#!/usr/bin/env bash
set -euo pipefail

# Reset bot file logs and Loki history for a clean observability baseline.
# Safe to rerun.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

if ! command -v docker >/dev/null 2>&1; then
  echo "[reset] docker is required but not found in PATH" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "[reset] docker compose is required but not available" >&2
  exit 1
fi

# Load .env values if present so HALO_LOG_PATH / PANDA_LOG_PATH can be used.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

truncate_logs() {
  local path="$1"
  local label="$2"

  if [[ -z "${path}" ]]; then
    echo "[reset] ${label}: path not configured, skipping"
    return
  fi

  if [[ ! -d "${path}" ]]; then
    echo "[reset] ${label}: directory does not exist (${path}), skipping"
    return
  fi

  echo "[reset] ${label}: truncating *.log files under ${path}"
  find "${path}" -type f -name "*.log" -exec truncate -s 0 {} \;
}

find_loki_volumes() {
  docker volume ls --format '{{.Name}}' | grep -E '(^|_)loki-data$' || true
}

echo "[reset] Stopping observability stack"
docker compose down

echo "[reset] Clearing bot log files"
truncate_logs "${HALO_LOG_PATH:-}" "Halo"
truncate_logs "${PANDA_LOG_PATH:-}" "Panda"

echo "[reset] Removing Loki data volume(s)"
mapfile -t loki_volumes < <(find_loki_volumes)

if [[ ${#loki_volumes[@]} -eq 0 ]]; then
  echo "[reset] No Loki volume matching *_loki-data found"
else
  for vol in "${loki_volumes[@]}"; do
    echo "[reset] Removing volume: ${vol}"
    docker volume rm "${vol}"
  done
fi

echo "[reset] Starting observability stack"
docker compose up -d

echo "[reset] Done"
echo "[reset] Verify:"
echo "  docker compose exec -T grafana wget -qO- http://loki:3100/ready"
echo "  docker compose exec -T prometheus wget -qO- \"http://localhost:9090/api/v1/query?query=up%7Bjob%3D%5C%22uptime-kuma%5C%22%7D\""
