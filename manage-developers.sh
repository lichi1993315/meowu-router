#!/bin/bash
# 开发者白名单管理脚本

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_PATH="$SCRIPT_DIR/data/conversations.db"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

if [ ! -f "$DB_PATH" ]; then
    echo -e "${RED}错误: 数据库不存在: $DB_PATH${NC}"
    exit 1
fi

case "${1:-help}" in
    add)
        if [ -z "$2" ]; then
            echo -e "${RED}用法: $0 add <user_id>${NC}"
            exit 1
        fi
        sqlite3 "$DB_PATH" "UPDATE user_sessions SET is_developer = 1 WHERE user_id = '$2'"
        if [ $? -eq 0 ]; then
            echo -e "${GREEN}✅ 已将用户 $2 标记为开发者${NC}"
        fi
        ;;
    
    remove)
        if [ -z "$2" ]; then
            echo -e "${RED}用法: $0 remove <user_id>${NC}"
            exit 1
        fi
        sqlite3 "$DB_PATH" "UPDATE user_sessions SET is_developer = 0 WHERE user_id = '$2'"
        if [ $? -eq 0 ]; then
            echo -e "${GREEN}✅ 已将用户 $2 从开发者移除${NC}"
        fi
        ;;
    
    list)
        echo -e "${YELLOW}=== 开发者列表 ===${NC}"
        sqlite3 -header -column "$DB_PATH" "SELECT user_id, country, total_requests, CAST((julianday(last_seen) - julianday(first_seen)) * 24 * 60 AS INTEGER) as play_minutes FROM user_sessions WHERE is_developer = 1"
        ;;
    
    all)
        echo -e "${YELLOW}=== 所有用户 (请求数>=2) ===${NC}"
        sqlite3 -header -column "$DB_PATH" "SELECT user_id, CASE WHEN is_developer = 1 THEN 'DEV' ELSE '' END as dev, country, total_requests as req, CAST((julianday(last_seen) - julianday(first_seen)) * 24 * 60 AS INTEGER) as mins FROM user_sessions WHERE total_requests >= 2 ORDER BY total_requests DESC"
        ;;
    
    stats)
        echo -e "${YELLOW}=== 统计信息 ===${NC}"
        echo ""
        
        # 总用户数
        total=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM user_sessions WHERE total_requests >= 2")
        devs=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM user_sessions WHERE total_requests >= 2 AND is_developer = 1")
        players=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM user_sessions WHERE total_requests >= 2 AND (is_developer = 0 OR is_developer IS NULL)")
        
        echo "有效用户总数: $total"
        echo "  - 开发者: $devs"
        echo "  - 真实玩家: $players"
        echo ""
        
        # 平均时长
        avg_all=$(sqlite3 "$DB_PATH" "SELECT ROUND(AVG((julianday(last_seen) - julianday(first_seen)) * 24 * 60), 1) FROM user_sessions WHERE total_requests >= 2")
        avg_real=$(sqlite3 "$DB_PATH" "SELECT ROUND(AVG((julianday(last_seen) - julianday(first_seen)) * 24 * 60), 1) FROM user_sessions WHERE total_requests >= 2 AND (is_developer = 0 OR is_developer IS NULL)")
        
        echo "全体平均时长: ${avg_all} 分钟"
        echo "真实平均时长: ${avg_real} 分钟"
        echo ""
        
        # 中位数
        median=$(sqlite3 "$DB_PATH" "SELECT ROUND((julianday(last_seen) - julianday(first_seen)) * 24 * 60, 1) FROM user_sessions WHERE total_requests >= 2 ORDER BY (julianday(last_seen) - julianday(first_seen)) LIMIT 1 OFFSET (SELECT COUNT(*) FROM user_sessions WHERE total_requests >= 2) / 2")
        echo "中位数时长: ${median} 分钟"
        ;;
    
    help|*)
        echo "开发者白名单管理工具"
        echo ""
        echo "用法: $0 <命令> [参数]"
        echo ""
        echo "命令:"
        echo "  add <user_id>    - 将用户标记为开发者"
        echo "  remove <user_id> - 取消用户的开发者标记"
        echo "  list             - 显示所有开发者"
        echo "  all              - 显示所有有效用户"
        echo "  stats            - 显示统计信息"
        echo "  help             - 显示此帮助"
        echo ""
        echo "示例:"
        echo "  $0 add ff026221-82ae-5636-8279-a322225a968e"
        echo "  $0 list"
        echo "  $0 stats"
        ;;
esac
