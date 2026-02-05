#!/bin/bash

# Router API 使用 Docker 容器运行
# 参考 docker-compose.monitoring.yml 中的 router-api 服务

set -e

COMPOSE_FILE="docker-compose.monitoring.yml"
SERVICE_NAME="router-api"

echo "=== Stopping existing $SERVICE_NAME container (if any) ==="
sudo docker compose -f "$COMPOSE_FILE" stop "$SERVICE_NAME" 2>/dev/null || true

echo "=== Rebuilding and starting $SERVICE_NAME ==="
sudo docker compose -f "$COMPOSE_FILE" up -d --build "$SERVICE_NAME"

echo "=== Checking $SERVICE_NAME status ==="
sudo docker compose -f "$COMPOSE_FILE" ps "$SERVICE_NAME"

echo ""
echo "✅ $SERVICE_NAME started successfully!"
echo "   - API available at: http://localhost:9000"
echo "   - View logs: sudo docker compose -f $COMPOSE_FILE logs -f $SERVICE_NAME"
