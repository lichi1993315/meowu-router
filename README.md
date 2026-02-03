# LLM Router Monitoring Stack

## Overview
This project runs a monitoring stack using Docker Compose, including:
- PostgreSQL (primary database)
- Metrics Exporter
- Prometheus
- Grafana (dashboards)
- Grafana Image Renderer
- Grafana Report Sender
- Developer Admin UI

All services run inside Docker containers.

## Prerequisites
- Docker + Docker Compose

## Environment Variables
Create a `.env` if needed (optional for defaults):

```bash
DB_HOST=postgres
DB_PORT=5432
DB_NAME=conversations
DB_USER=postgres
DB_PASSWORD=postgres
DB_SSLMODE=disable

GRAFANA_TOKEN=...         # if using grafana-report
FEISHU_BOT_API_KEY=...
FEISHU_BOT_API_SECRET=...
FEISHU_CHAT_ID=...
```

## Start the Stack
From the repo root:

```bash
sudo docker compose -f docker-compose.monitoring.yml up -d --build
```

## Start Grafana (only)
Grafana depends on Prometheus and the renderer; start all required services:

```bash
sudo docker compose -f docker-compose.monitoring.yml up -d grafana prometheus renderer
```

Grafana will be available at:
- http://<host>:9001
- Default login: `admin` / `admin`

## Check Service Status
```bash
sudo docker compose -f docker-compose.monitoring.yml ps
```

## View Logs
```bash
sudo docker compose -f docker-compose.monitoring.yml logs -f grafana
```

## Stop the Stack
```bash
sudo docker compose -f docker-compose.monitoring.yml down
```

## Database Migration (SQLite -> PostgreSQL)
See `POSTGRES_MIGRATION.md` for steps and rollback guidance.
