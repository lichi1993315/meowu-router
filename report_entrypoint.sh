#!/bin/bash
set -euo pipefail

for var_name in FEISHU_BOT_API_KEY FEISHU_BOT_API_SECRET FEISHU_CHAT_ID; do
  if [[ -z "${!var_name:-}" ]]; then
    echo "Missing required env: ${var_name}"
    exit 1
  fi
done

# Debian cron 使用系统时区；CRON_TZ 不负责该实现的调度时区。
ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
echo Asia/Shanghai > /etc/timezone
umask 077
{
  for var_name in FEISHU_BOT_API_KEY FEISHU_BOT_API_SECRET FEISHU_CHAT_ID; do
    printf 'export %s=%q\n' "$var_name" "${!var_name}"
  done
  printf 'export DB_PATH=%q\n' "${DB_PATH:-/app/data/conversations.db}"
} > /app/report.env

cat > /etc/cron.d/grafana_report <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# 每日 09:00 发送昨日用户日报，09:10/09:20 有限重试；成功后按日期去重。
0,10,20 9 * * * root bash -c 'source /app/report.env && python /app/daily_user_report.py >> /app/output/report_cron.log 2>&1'
CRON
chmod 0644 /etc/cron.d/grafana_report
echo "User daily report scheduled: 09:00 Asia/Shanghai (retry 09:10, 09:20)"
exec cron -f
