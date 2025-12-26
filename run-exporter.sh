#!/bin/bash
# LLM Router Metrics Exporter - 本地运行脚本（无Docker）
# 适用于没有Docker的环境

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  LLM Router Metrics Exporter (Local)${NC}"
echo -e "${GREEN}========================================${NC}"

# 检查Python
if ! command -v python3 &> /dev/null; then
    echo "错误: Python3 未安装"
    exit 1
fi

# 安装依赖
echo -e "${YELLOW}检查/安装依赖...${NC}"
pip install prometheus-client --quiet 2>/dev/null || pip install prometheus-client

# 创建数据目录
mkdir -p data

case "${1:-start}" in
    start)
        echo -e "${YELLOW}启动 Metrics Exporter...${NC}"
        echo ""
        echo -e "  🔢 Metrics 端点: ${GREEN}http://localhost:9090/metrics${NC}"
        echo ""
        echo -e "${YELLOW}提示: 使用 Ctrl+C 停止${NC}"
        echo ""
        
        python3 metrics_exporter.py
        ;;
    
    background)
        echo -e "${YELLOW}后台启动 Metrics Exporter...${NC}"
        nohup python3 metrics_exporter.py > exporter.log 2>&1 &
        echo $! > exporter.pid
        echo -e "${GREEN}✅ 已启动 (PID: $(cat exporter.pid))${NC}"
        echo -e "   日志: tail -f exporter.log"
        echo -e "   停止: $0 stop"
        ;;
    
    stop)
        if [ -f exporter.pid ]; then
            kill $(cat exporter.pid) 2>/dev/null || true
            rm -f exporter.pid
            echo -e "${GREEN}✅ 已停止${NC}"
        else
            echo "未找到运行中的exporter"
        fi
        ;;
    
    status)
        if [ -f exporter.pid ] && kill -0 $(cat exporter.pid) 2>/dev/null; then
            echo -e "${GREEN}运行中 (PID: $(cat exporter.pid))${NC}"
            curl -s http://localhost:9090/metrics | head -20
        else
            echo "未运行"
        fi
        ;;
    
    query)
        # 查询SQLite数据库
        python3 << 'EOF'
import sqlite3
import sys

conn = sqlite3.connect('data/conversations.db')
c = conn.cursor()

print("=== 数据统计 ===")
c.execute('SELECT COUNT(*) FROM conversations')
print(f"总对话数: {c.fetchone()[0]}")

c.execute('SELECT COUNT(DISTINCT user_id) FROM conversations')
print(f"唯一用户数: {c.fetchone()[0]}")

print("\n=== 国家分布 ===")
c.execute('SELECT country, COUNT(*) FROM conversations GROUP BY country ORDER BY COUNT(*) DESC')
for row in c.fetchall():
    print(f"  {row[0]}: {row[1]}")

print("\n=== 最近对话 ===")
c.execute('SELECT timestamp, user_id, user_query, ai_action FROM conversations WHERE user_query != "" ORDER BY timestamp DESC LIMIT 5')
for row in c.fetchall():
    print(f"  [{row[0][:19]}] {row[1][:8]}... | {row[2][:30]} -> {row[3]}")

conn.close()
EOF
        ;;
    
    *)
        echo "用法: $0 {start|background|stop|status|query}"
        echo ""
        echo "  start      - 前台启动 (显示日志)"
        echo "  background - 后台启动"
        echo "  stop       - 停止后台运行的exporter"
        echo "  status     - 查看状态"
        echo "  query      - 查询SQLite数据库统计"
        exit 1
        ;;
esac
