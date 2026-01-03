#!/bin/bash
set -euo pipefail

required_vars=(
  FEISHU_BOT_API_KEY
  FEISHU_BOT_API_SECRET
  FEISHU_CHAT_ID
  GRAFANA_URL
  GRAFANA_TOKEN
)

for var_name in "${required_vars[@]}"; do
  if [[ -z "${!var_name:-}" ]]; then
    echo "Missing required env: ${var_name}"
    exit 1
  fi
done

if [[ -z "${GRAFANA_RENDER_URL:-}" && -z "${GRAFANA_RENDER_PATH:-}" ]]; then
  echo "Missing Grafana render URL. Set GRAFANA_RENDER_URL or GRAFANA_RENDER_PATH."
  exit 1
fi

env_file="/app/report.env"
cron_file="/etc/cron.d/grafana_report"

escape_value() {
  printf "%q" "$1"
}

{
  echo "export FEISHU_BOT_API_KEY=$(escape_value "${FEISHU_BOT_API_KEY}")"
  echo "export FEISHU_BOT_API_SECRET=$(escape_value "${FEISHU_BOT_API_SECRET}")"
  echo "export FEISHU_CHAT_ID=$(escape_value "${FEISHU_CHAT_ID}")"
  echo "export FEISHU_RECEIVE_ID_TYPE=$(escape_value "${FEISHU_RECEIVE_ID_TYPE:-chat_id}")"
  echo "export GRAFANA_URL=$(escape_value "${GRAFANA_URL}")"
  echo "export GRAFANA_TOKEN=$(escape_value "${GRAFANA_TOKEN}")"
  echo "export GRAFANA_RENDER_URL=$(escape_value "${GRAFANA_RENDER_URL:-}")"
  echo "export GRAFANA_RENDER_PATH=$(escape_value "${GRAFANA_RENDER_PATH:-}")"
  echo "export GRAFANA_TIMEOUT=$(escape_value "${GRAFANA_TIMEOUT:-20}")"
  echo "export FEISHU_TIMEOUT=$(escape_value "${FEISHU_TIMEOUT:-20}")"
  echo "export REPORT_TIMEZONE=$(escape_value "${REPORT_TIMEZONE:-Asia/Shanghai}")"
} > "${env_file}"

chmod 0600 "${env_file}"

{
  echo "SHELL=/bin/bash"
  echo "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  echo "CRON_TZ=Asia/Shanghai"
  # UTC 02:00 = 北京时间 10:00
  echo "0 2 * * * root bash -lc 'source /app/report.env && python /app/send_report.py >> /app/output/report_cron.log 2>&1'"
} > "${cron_file}"

chmod 0644 "${cron_file}"

exec cron -f
