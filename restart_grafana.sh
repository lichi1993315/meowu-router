#!/bin/bash
# 一键重启 Grafana 容器
#这会触发重新加载 provisioning 目录下的 Dashboard 和 Datasource 配置

echo "🔄 Restarting Grafana..."
sudo docker compose -f docker-compose.monitoring.yml restart grafana

echo "✅ Grafana restarted."
echo "Wait a few seconds for it to satisfy health checks."
