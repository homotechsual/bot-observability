# Bot Observability

Shared observability stack for both bots without SSHing into servers.

## Included Services

* Uptime Kuma (status dashboard): [http://127.0.0.1:3001](http://127.0.0.1:3001)
* Grafana (log dashboard): [http://127.0.0.1:3000](http://127.0.0.1:3000)
* Loki (log storage)
* Promtail (log shipping from bot log folders)
* Prometheus (metrics for Grafana): [http://127.0.0.1:9090](http://127.0.0.1:9090)

## 1. Configure Environment

1. Copy `.env.example` to `.env`.
2. Set `GRAFANA_ADMIN_USER` and `GRAFANA_ADMIN_PASSWORD`.
3. Set `HALO_LOG_PATH` and `PANDA_LOG_PATH` to absolute log directories on your host.
4. If Uptime Kuma metrics auth is enabled, set `KUMA_METRICS_API_KEY`.
5. Create Prometheus auth file from that key:

```powershell
New-Item -ItemType Directory -Force -Path .\secrets | Out-Null
Set-Content -Path .\secrets\kuma_metrics_api_key -Value $env:KUMA_METRICS_API_KEY -NoNewline
```

## 2. Start Stack

```powershell
docker compose up -d
```

## 3. Access Dashboards

* Uptime Kuma: `http://127.0.0.1:3001`
* Grafana: `http://127.0.0.1:3000`

Grafana includes pre-provisioned datasources for Loki and Prometheus.

A starter dashboard named `Bot Ops Overview` is auto-provisioned with:

* Uptime Kuma scrape health (Prometheus)
* Error/warning stats and trends (Loki)
* Recent error logs (Loki)

If the `Uptime Kuma Scrape Health` panel shows no data, enable Prometheus metrics in Uptime Kuma settings and refresh Grafana.

## 3.1 Make Dashboards Web Accessible (No SSH)

Use a reverse proxy with TLS in front of local-only container ports.

Required server configuration:

1. DNS records:
   * `grafana.<your-domain>` -> server public IP
   * `kuma.<your-domain>` -> server public IP
2. Firewall:
   * Open inbound `80/tcp` and `443/tcp`.
   * Keep `3000`, `3001`, and `3100` closed publicly (containers bind to `127.0.0.1` by default).
3. Reverse proxy:
   * Configure Nginx (or Caddy/Traefik) to proxy HTTPS traffic to `127.0.0.1:3000` (Grafana) and `127.0.0.1:3001` (Kuma).
   * Example config: `deploy/nginx-observability.conf.example`.
4. TLS certificates:
   * Use Let's Encrypt certificates for both hostnames.

Optional hardening:

* Restrict Grafana/Kuma access by IP allow-list in proxy.
* Add Auth at proxy layer in addition to app auth.

## 4. Add Bot Monitors (Uptime Kuma)

Recommended monitors:

* HTTP(s) monitor to bot health endpoint (if you add one)
* Push monitor (heartbeat style)
* TCP monitor to known bot-adjacent service

If the bots do not expose HTTP health yet, use Push monitors and have each bot send periodic heartbeats.

## 5. Verify Logs in Grafana

In Grafana Explore:

* Halo logs query: `{service="halo-bot"}`
* Panda logs query: `{service="panda-bot"}`

## Notes

* This project is intentionally separate from bot codebases.
* Keep it as shared infra for all current/future bots.
* Add alerts later (Grafana Alerting or Kuma notifications to Discord).

## CI/CD (Push = Deploy)

This repo includes GitHub Actions workflows:

* `.github/workflows/validate-compose.yml` validates Compose config on PRs and pushes.
* `.github/workflows/deploy.yml` validates, then deploys automatically on pushes to `main` (and supports manual runs).

Deployment safety controls included:

* Validation gate (`deploy` waits for `validate` to pass)
* Concurrency lock (single active deployment)
* GitHub `production` environment target (use environment protection rules)
* Automatic rollback to previous release bundle if deployment fails

Configure these repository secrets before first deployment:

* `OBSERVABILITY_HOST`: server hostname or IP
* `OBSERVABILITY_USER`: SSH username
* `OBSERVABILITY_SSH_KEY`: private key used by Actions
* `OBSERVABILITY_PORT`: SSH port (optional, defaults to 22)
* `OBSERVABILITY_TARGET_PATH`: absolute deployment directory on host
* `GRAFANA_ADMIN_USER`: Grafana admin username
* `GRAFANA_ADMIN_PASSWORD`: Grafana admin password
* `HALO_LOG_PATH`: absolute Halo bot logs path on host
* `PANDA_LOG_PATH`: absolute Panda bot logs path on host
* `KUMA_METRICS_API_KEY`: Uptime Kuma Prometheus API key for `/metrics` scraping when auth is enabled

Deployment behavior:

1. Bundle repo contents.
2. Upload bundle to host.
3. Extract into `OBSERVABILITY_TARGET_PATH/current`.
4. Generate `.env` from secrets.
5. Run `docker compose pull` and `docker compose up -d --remove-orphans`.
6. If any deploy step fails, restore previous release and restart stack from backup.

## Uptime Kuma Data Persistence and Backup

Uptime Kuma state (monitors, settings, notifications, API keys) is stored in a dedicated named Docker volume:

* `bot-observability-kuma-data`

This volume is independent from release files under `current`, so deploys and release rollbacks do not overwrite Kuma config.

During each GitHub Actions deploy, a snapshot backup of Kuma data is created in:

* `OBSERVABILITY_TARGET_PATH/backups/kuma-data-YYYYMMDDHHMMSS.tar.gz`

To restore Kuma data from a snapshot on the server:

```bash
docker compose stop uptime-kuma
docker run --rm -v bot-observability-kuma-data:/target -v /opt/bot-observability/backups:/backup alpine:3.20 sh -c "rm -rf /target/* && tar -xzf /backup/kuma-data-YYYYMMDDHHMMSS.tar.gz -C /target"
docker compose up -d uptime-kuma
```

## Reset Observability Data

Use the reset helper to start from a clean baseline:

```bash
./deploy/reset-observability-logs.sh
```

What it clears:

* Bot `*.log` files under `HALO_LOG_PATH` and `PANDA_LOG_PATH`
* Loki data volume (`*loki-data`)
* Prometheus data volume (`*prometheus-data`) - this clears Grafana history panels such as `Bot Uptime Status Over Time`

What it preserves:

* Grafana state volume
* Uptime Kuma configuration/monitors volume

Reset prerequisites:

* Ensure `KUMA_METRICS_API_KEY` is set in `.env` or `./secrets/kuma_metrics_api_key` exists and is non-empty.
* The reset script validates this before startup so Prometheus can scrape Kuma immediately after reset.
