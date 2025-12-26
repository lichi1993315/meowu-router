#!/usr/bin/env python3
"""
开发者管理Web界面 - Docker容器内运行版本
直接访问SQLite数据库
支持：开发者标记、昵称管理
"""

import sqlite3
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, unquote

PORT = int(os.getenv("ADMIN_PORT", "9095"))
DB_PATH = os.getenv("DB_PATH", "/app/data/conversations.db")

def get_html_template():
    return '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>🔧 开发者管理</title>
    <style>
        * { box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: #e0e0e0;
            margin: 0;
            padding: 20px;
            min-height: 100vh;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 { color: #00d9ff; text-align: center; margin-bottom: 30px; }
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            padding: 20px;
            text-align: center;
            border: 1px solid rgba(255,255,255,0.1);
        }
        .stat-value { font-size: 1.8em; font-weight: bold; color: #00d9ff; }
        .stat-label { color: #888; margin-top: 5px; font-size: 0.9em; }
        table {
            width: 100%;
            border-collapse: collapse;
            background: rgba(255,255,255,0.02);
            border-radius: 12px;
            overflow: hidden;
        }
        th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.1); }
        th { background: rgba(0,217,255,0.1); color: #00d9ff; }
        tr:hover { background: rgba(255,255,255,0.05); }
        .btn {
            padding: 6px 12px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
            text-decoration: none;
            display: inline-block;
            margin: 2px;
        }
        .btn-dev { background: #ff6b6b; color: white; }
        .btn-player { background: #51cf66; color: white; }
        .btn-save { background: #339af0; color: white; }
        .dev-badge { background: #ff6b6b; color: white; padding: 4px 8px; border-radius: 4px; font-size: 12px; }
        .player-badge { background: #51cf66; color: white; padding: 4px 8px; border-radius: 4px; font-size: 12px; }
        .success { background: #51cf66; color: white; padding: 10px 20px; border-radius: 8px; margin-bottom: 20px; text-align: center; }
        .error { background: #ff6b6b; color: white; padding: 10px 20px; border-radius: 8px; margin-bottom: 20px; text-align: center; }
        .user-id { font-family: monospace; font-size: 10px; max-width: 280px; overflow: hidden; text-overflow: ellipsis; }
        .nickname-input { 
            background: rgba(255,255,255,0.1); 
            border: 1px solid rgba(255,255,255,0.2); 
            border-radius: 4px; 
            padding: 5px 8px; 
            color: #fff; 
            width: 120px;
            font-size: 13px;
        }
        .nickname-input:focus { outline: none; border-color: #00d9ff; }
        .nickname-form { display: flex; gap: 5px; align-items: center; }
        .nickname-display { color: #ffd43b; font-weight: bold; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔧 用户管理后台</h1>
        %%%MESSAGE%%%
        <div class="stats">
            <div class="stat-card">
                <div class="stat-value">%%%TOTAL%%%</div>
                <div class="stat-label">有效用户</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">%%%DEVS%%%</div>
                <div class="stat-label">开发者</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">%%%PLAYERS%%%</div>
                <div class="stat-label">真实玩家</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">%%%AVG_ALL%%%</div>
                <div class="stat-label">全体平均(分)</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">%%%AVG_REAL%%%</div>
                <div class="stat-label">真实平均(分)</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">%%%MEDIAN%%%</div>
                <div class="stat-label">中位数(分)</div>
            </div>
        </div>
        <table>
            <thead>
                <tr>
                    <th>用户ID</th>
                    <th>昵称</th>
                    <th>类型</th>
                    <th>国家</th>
                    <th>请求</th>
                    <th>时长(分)</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>%%%ROWS%%%</tbody>
        </table>
    </div>
</body>
</html>'''

def get_db():
    return sqlite3.connect(DB_PATH)

def get_stats():
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM user_sessions WHERE total_requests >= 2")
    total = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM user_sessions WHERE total_requests >= 2 AND is_developer = 1")
    devs = c.fetchone()[0]
    
    c.execute("SELECT ROUND(AVG((julianday(last_seen) - julianday(first_seen)) * 24 * 60), 1) FROM user_sessions WHERE total_requests >= 2")
    avg_all = c.fetchone()[0] or 0
    
    c.execute("SELECT ROUND(AVG((julianday(last_seen) - julianday(first_seen)) * 24 * 60), 1) FROM user_sessions WHERE total_requests >= 2 AND (is_developer = 0 OR is_developer IS NULL)")
    avg_real = c.fetchone()[0] or 0
    
    c.execute("SELECT ROUND((julianday(last_seen) - julianday(first_seen)) * 24 * 60, 1) FROM user_sessions WHERE total_requests >= 2 ORDER BY (julianday(last_seen) - julianday(first_seen)) LIMIT 1 OFFSET (SELECT COUNT(*) FROM user_sessions WHERE total_requests >= 2) / 2")
    median = c.fetchone()[0] or 0
    
    conn.close()
    return total, devs, total - devs, avg_all, avg_real, median

def get_users():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT user_id, COALESCE(is_developer, 0), country, total_requests, 
               CAST((julianday(last_seen) - julianday(first_seen)) * 24 * 60 AS INTEGER),
               nickname, player_name
        FROM user_sessions WHERE total_requests >= 2 ORDER BY total_requests DESC
    ''')
    users = c.fetchall()
    conn.close()
    return users

def set_developer(user_id, is_dev):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE user_sessions SET is_developer = ? WHERE user_id = ?", (1 if is_dev else 0, user_id))
    conn.commit()
    conn.close()

def set_nickname(user_id, nickname):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE user_sessions SET nickname = ? WHERE user_id = ?", (nickname, user_id))
    conn.commit()
    conn.close()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass
    
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        
        message = ""
        
        if 'action' in query:
            user_id = query.get('user_id', [''])[0]
            action = query['action'][0]
            if user_id:
                try:
                    if action == 'add_dev':
                        set_developer(user_id, True)
                        message = f'<div class="success">✅ 已将 {user_id[:20]}... 标记为开发者</div>'
                    elif action == 'remove_dev':
                        set_developer(user_id, False)
                        message = f'<div class="success">✅ 已将 {user_id[:20]}... 设为普通玩家</div>'
                    elif action == 'set_nickname':
                        nickname = unquote(query.get('nickname', [''])[0])
                        set_nickname(user_id, nickname)
                        message = f'<div class="success">✅ 已设置昵称: {nickname}</div>'
                except Exception as e:
                    message = f'<div class="error">❌ 操作失败: {e}</div>'
        
        total, devs, players, avg_all, avg_real, median = get_stats()
        users = get_users()
        
        rows = ""
        for user in users:
            user_id, is_dev, country, requests, mins, nickname, player_name = user
            nickname = nickname or ""
            player_name = player_name or ""
            # 显示名称：优先nickname，其次player_name
            display_name = nickname or player_name
            badge = '<span class="dev-badge">🔧 开发者</span>' if is_dev else '<span class="player-badge">👤 玩家</span>'
            
            if is_dev:
                dev_btn = f'<a class="btn btn-player" href="?action=remove_dev&user_id={user_id}">设为玩家</a>'
            else:
                dev_btn = f'<a class="btn btn-dev" href="?action=add_dev&user_id={user_id}">标记开发者</a>'
            
            # 昵称显示和编辑
            if nickname:
                nickname_display = f'<span class="nickname-display">{nickname}</span>'
            elif player_name:
                nickname_display = f'<span style="color:#aaa">🎮 {player_name}</span>'
            else:
                nickname_display = '<span style="color:#666">未设置</span>'
            
            nickname_form = f'''
            <form class="nickname-form" method="get" style="display:inline-flex;">
                <input type="hidden" name="action" value="set_nickname">
                <input type="hidden" name="user_id" value="{user_id}">
                <input type="text" name="nickname" class="nickname-input" placeholder="输入昵称" value="{nickname}">
                <button type="submit" class="btn btn-save">保存</button>
            </form>
            '''
            
            rows += f'''<tr>
                <td class="user-id">{user_id}</td>
                <td>{nickname_display}<br>{nickname_form}</td>
                <td>{badge}</td>
                <td>{country or "?"}</td>
                <td>{requests}</td>
                <td>{mins}</td>
                <td>{dev_btn}</td>
            </tr>'''
        
        html = get_html_template()
        html = html.replace('%%%MESSAGE%%%', message)
        html = html.replace('%%%TOTAL%%%', str(total))
        html = html.replace('%%%DEVS%%%', str(devs))
        html = html.replace('%%%PLAYERS%%%', str(players))
        html = html.replace('%%%AVG_ALL%%%', str(avg_all))
        html = html.replace('%%%AVG_REAL%%%', str(avg_real))
        html = html.replace('%%%MEDIAN%%%', str(median))
        html = html.replace('%%%ROWS%%%', rows)
        
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

def main():
    print(f"🔧 用户管理后台启动 - 端口 {PORT}")
    print(f"📁 数据库: {DB_PATH}")
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    server.serve_forever()

if __name__ == '__main__':
    main()
