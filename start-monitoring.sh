#!/bin/bash
# LLM Router 监控系统启动脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  LLM Router 监控系统${NC}"
echo -e "${GREEN}========================================${NC}"

# 创建数据目录
mkdir -p data

# 检查Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}错误: Docker 未安装${NC}"
    exit 1
fi

if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo -e "${RED}错误: Docker Compose 未安装${NC}"
    exit 1
fi

# 检查是docker-compose还是docker compose
if docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
else
    COMPOSE_CMD="docker-compose"
fi

case "${1:-start}" in
    start)
        echo -e "${YELLOW}正在启动监控服务...${NC}"
        $COMPOSE_CMD -f docker-compose.monitoring.yml up -d --build
        
        echo ""
        echo -e "${GREEN}✅ 监控服务已启动!${NC}"
        echo ""
        echo -e "  📊 Grafana:    ${GREEN}http://localhost:3000${NC}"
        echo -e "     用户名: admin"
        echo -e "     密码:   admin"
        echo ""
        echo -e "  📈 Prometheus: ${GREEN}http://localhost:9091${NC}"
        echo -e "  🔢 Metrics:    ${GREEN}http://localhost:9090/metrics${NC}"
        echo ""
        echo -e "${YELLOW}提示: 首次启动需要等待几秒钟让服务完全初始化${NC}"
        ;;
    
    stop)
        echo -e "${YELLOW}正在停止监控服务...${NC}"
        $COMPOSE_CMD -f docker-compose.monitoring.yml down
        echo -e "${GREEN}✅ 监控服务已停止${NC}"
        ;;
    
    restart)
        echo -e "${YELLOW}正在重启监控服务...${NC}"
        $COMPOSE_CMD -f docker-compose.monitoring.yml restart
        echo -e "${GREEN}✅ 监控服务已重启${NC}"
        ;;
    
    logs)
        $COMPOSE_CMD -f docker-compose.monitoring.yml logs -f "${2:-}"
        ;;
    
    status)
        echo -e "${YELLOW}监控服务状态:${NC}"
        $COMPOSE_CMD -f docker-compose.monitoring.yml ps
        ;;
    
    clean)
        echo -e "${RED}警告: 这将删除所有监控数据!${NC}"
        read -p "确认删除? (y/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            $COMPOSE_CMD -f docker-compose.monitoring.yml down -v
            rm -rf data/*
            echo -e "${GREEN}✅ 已清理所有数据${NC}"
        fi
        ;;
    
    *)
        echo "用法: $0 {start|stop|restart|logs|status|clean}"
        echo ""
        echo "  start   - 启动监控服务"
        echo "  stop    - 停止监控服务"
        echo "  restart - 重启监控服务"
        echo "  logs    - 查看日志 (可选: logs [服务名])"
        echo "  status  - 查看服务状态"
        echo "  clean   - 清理所有数据"
        exit 1
        ;;
esac
