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

# Load .env values if present so HALO_LOG_PATH / PANDA_LOG_PATH / HUDU_LOG_PATH can be used.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

ensure_kuma_metrics_secret() {
  local secret_file="${REPO_ROOT}/secrets/kuma_metrics_api_key"

  mkdir -p "${REPO_ROOT}/secrets"

  if [[ -n "${KUMA_METRICS_API_KEY:-}" ]]; then
    printf '%s' "${KUMA_METRICS_API_KEY}" > "${secret_file}"
    chmod 644 "${secret_file}"
    echo "[reset] Wrote Kuma metrics auth secret from KUMA_METRICS_API_KEY"
  fi

  if [[ ! -f "${secret_file}" ]]; then
    echo "[reset] ERROR: Missing ${secret_file}. Set KUMA_METRICS_API_KEY in .env or create secrets/kuma_metrics_api_key." >&2
    exit 1
  fi

  if [[ ! -s "${secret_file}" ]]; then
    echo "[reset] ERROR: ${secret_file} is empty. Populate it with the Kuma metrics password." >&2
    exit 1
  fi
}

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
  # Support both legacy underscore and current hyphenated volume names.
  docker volume ls --format '{{.Name}}' | grep -E '(^|[_-])loki-data$' || true
}

find_prometheus_volumes() {
  # Support both legacy underscore and current hyphenated volume names.
  docker volume ls --format '{{.Name}}' | grep -E '(^|[_-])prometheus-data$' || true
}

ensure_external_volume() {
  local volume_name="$1"

  if docker volume inspect "${volume_name}" >/dev/null 2>&1; then
    echo "[reset] Volume exists: ${volume_name}"
  else
    echo "[reset] Creating missing volume: ${volume_name}"
    docker volume create "${volume_name}" >/dev/null
  fi
}

echo "[reset] Stopping observability stack"
docker compose down

echo "[reset] Clearing bot log files"
truncate_logs "${HALO_LOG_PATH:-}" "Halo"
truncate_logs "${PANDA_LOG_PATH:-}" "Panda"
truncate_logs "${HUDU_LOG_PATH:-}" "Hudu"
truncate_logs "${HOMOTECHSUAL_LOG_PATH:-}" "Homotechsual"

echo "[reset] Removing Loki data volume(s)"
mapfile -t loki_volumes < <(find_loki_volumes)

if [[ ${#loki_volumes[@]} -eq 0 ]]; then
  echo "[reset] No Loki volume matching *loki-data found"
else
  for vol in "${loki_volumes[@]}"; do
    echo "[reset] Removing volume: ${vol}"
    docker volume rm "${vol}"
  done
fi

echo "[reset] Removing Prometheus data volume(s) to clear uptime/stat history"
mapfile -t prometheus_volumes < <(find_prometheus_volumes)

if [[ ${#prometheus_volumes[@]} -eq 0 ]]; then
  echo "[reset] No Prometheus volume matching *prometheus-data found"
else
  for vol in "${prometheus_volumes[@]}"; do
    echo "[reset] Removing volume: ${vol}"
    docker volume rm "${vol}"
  done
fi

echo "[reset] Starting observability stack"
ensure_kuma_metrics_secret
ensure_external_volume "bot-observability-grafana-data"
ensure_external_volume "bot-observability-loki-data"
ensure_external_volume "bot-observability-kuma-data"
ensure_external_volume "bot-observability-prometheus-data"
docker compose up -d

# Wait briefly for first scrape so dashboard stat cards repopulate after TSDB reset.
sleep 5

echo "[reset] Done"
echo "[reset] Note: Uptime monitor definitions in Kuma are preserved; Prometheus metric history is reset."
echo "[reset] Verify:"
echo "  docker compose exec -T grafana wget -qO- http://loki:3100/ready"
echo "  docker compose exec -T prometheus wget -qO- \"http://localhost:9090/api/v1/query?query=up%7Bjob%3D%5C%22uptime-kuma%5C%22%7D\""
