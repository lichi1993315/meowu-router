#!/usr/bin/env python3
"""
开发者管理Web界面 - Docker容器内运行版本
直接访问SQLite数据库
支持：开发者标记、昵称管理、黑名单管理
"""

import sqlite3
import os
import jieba
import json
from collections import defaultdict
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
        .btn-black { background: #343a40; color: white; border: 1px solid #495057; }
        .dev-badge { background: #ff6b6b; color: white; padding: 4px 8px; border-radius: 4px; font-size: 12px; }
        .player-badge { background: #51cf66; color: white; padding: 4px 8px; border-radius: 4px; font-size: 12px; }
        .black-badge { background: #343a40; color: white; padding: 4px 8px; border-radius: 4px; font-size: 12px; border: 1px solid #868e96; }
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
        
        /* New Analytics CSS */
        .analytics-section { margin-top: 40px; border-top: 1px solid rgba(255,255,255,0.1); padding-top: 20px; }
        .chart-container { display: flex; gap: 20px; flex-wrap: wrap; }
        .chart-box { flex: 1; min-width: 300px; background: rgba(255,255,255,0.02); padding: 20px; border-radius: 12px; }
        .bar-chart { display: flex; flex-direction: column; gap: 10px; margin-top: 15px; }
        .bar-row { display: flex; align-items: center; gap: 10px; font-size: 13px; }
        .bar-label { width: 60px; text-align: right; color: #888; }
        .bar-track { flex: 1; background: rgba(255,255,255,0.1); height: 8px; border-radius: 4px; overflow: hidden; }
        .bar-fill { height: 100%; background: #00d9ff; border-radius: 4px; }
        .word-cloud { display: flex; flex-wrap: wrap; gap: 10px; padding: 20px; justify-content: center; }
        .word-tag { background: rgba(0, 217, 255, 0.1); color: #00d9ff; padding: 4px 10px; border-radius: 15px; cursor: default; transition: transform 0.2s; }
        .word-tag:hover { transform: scale(1.1); background: rgba(0, 217, 255, 0.2); }
        
        .preset-list { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }
        .preset-tag { background: #343a40; padding: 5px 10px; border-radius: 4px; border: 1px solid #495057; display: flex; align-items: center; gap: 8px; font-size: 13px; }
        .preset-delete { color: #ff6b6b; cursor: pointer; text-decoration: none; font-weight: bold; }
        .h2-title { color: #ffd43b; border-left: 4px solid #ffd43b; padding-left: 10px; margin: 30px 0 20px 0; }
        
        /* Retention Table */
        .retention-table { width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 13px; }
        .retention-table th, .retention-table td { padding: 8px 12px; text-align: center; border-bottom: 1px solid rgba(255,255,255,0.1); }
        .retention-table th { background: rgba(0,217,255,0.1); color: #00d9ff; }
        .retention-table tr:hover { background: rgba(255,255,255,0.03); }
        .ret-good { color: #51cf66; }
        .ret-ok { color: #ffd43b; }
        .ret-bad { color: #ff6b6b; }
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
                    <th>首次游玩(北京)</th>
                    <th>最后游玩(北京)</th>
                    <th>请求</th>
                    <th>时长(分)</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>%%%ROWS%%%</tbody>
        </table>
            <tbody>%%%ROWS%%%</tbody>
        </table>
        
        <div class="analytics-section">
            <h2 class="h2-title">📊 深度分析 (真实对话)</h2>
            <div class="chart-container">
                <div class="chart-box">
                    <h3>📈 对话分布</h3>
                    <div class="bar-chart">%%%DIST_CHART%%%</div>
                    <div style="margin-top: 15px; font-size: 0.9em; color:#aaa">
                        平均对话: <span style="color:#fff">%%%AVG_REQS%%%</span> | 
                        中位数: <span style="color:#fff">%%%MEDIAN_REQS%%%</span>
                    </div>
                </div>
                <div class="chart-box">
                    <h3>🐋 头部效应 (Whales)</h3>
                    <div style="margin-top: 10px; line-height: 1.6;">
                        Top 10% 用户贡献了 <strong style="color:#ff6b6b; font-size: 1.2em">%%%TOP10_VOL%%%</strong> % 的流量
                    </div>
                </div>
            </div>
            
            <h2 class="h2-title">☁️ 热门词云 (Top 100)</h2>
            <div class="chart-box">
                <div class="word-cloud">%%%WORD_CLOUD%%%</div>
            </div>
            
            <h2 class="h2-title">🛑 预设对话排除</h2>
            <div class="chart-box">
                <p style="color:#888; font-size: 0.9em;">以下文本将不计入"真实对话"统计，且不会出现在词云中。</p>
                <form method="get" style="display:flex; gap:10px; margin-bottom:15px;">
                    <input type="hidden" name="action" value="add_preset">
                    <input type="text" name="phrase" class="nickname-input" style="width: 300px;" placeholder="输入要排除的预设对话..." required>
                    <button type="submit" class="btn btn-save">添加排除</button>
                </form>
                <div class="preset-list">%%%PRESETS%%%</div>
            </div>
            
            <h2 class="h2-title">📈 用户增长 & 留存</h2>
            <div class="chart-box">
                <table class="retention-table">
                    <thead>
                        <tr>
                            <th>日期</th>
                            <th>DAU</th>
                            <th>新增</th>
                            <th>次留</th>
                            <th>3日留</th>
                            <th>7日留</th>
                        </tr>
                    </thead>
                    <tbody>%%%RETENTION_ROWS%%%</tbody>
                </table>
            </div>
        </div>
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
               nickname, player_name,
               datetime(first_seen, '+8 hours') as first_seen_bj,
               datetime(last_seen, '+8 hours') as last_seen_bj,
               COALESCE(is_blacklisted, 0)
        FROM user_sessions WHERE total_requests >= 2 ORDER BY total_requests DESC
    ''')
    users = c.fetchall()
    conn.close()
    return users

def get_presets():
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("SELECT phrase FROM preset_phrases")
    except:
        return []
    presets = [r[0] for r in c.fetchall()]
    conn.close()
    return presets

def add_preset(phrase):
    conn = get_db()
    c = conn.cursor()
    try:
        print(f"Adding preset: {phrase}")
        c.execute("INSERT OR IGNORE INTO preset_phrases (phrase) VALUES (?)", (phrase,))
        # 触发更新
        print(f"Updating conversations for preset: {phrase}")
        c.execute("UPDATE conversations SET is_preset = 1 WHERE user_query = ?", (phrase,))
    except Exception as e:
        print(f"Error adding preset: {e}")
        try:
            c.execute("CREATE TABLE IF NOT EXISTS preset_phrases (id INTEGER PRIMARY KEY, phrase TEXT UNIQUE)")
            c.execute("INSERT OR IGNORE INTO preset_phrases (phrase) VALUES (?)", (phrase,))
        except Exception as e2:
            print(f"Error creating table/inserting preset: {e2}")
    conn.commit()
    conn.close()

def remove_preset(phrase):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM preset_phrases WHERE phrase = ?", (phrase,))
    # 触发更新
    c.execute("UPDATE conversations SET is_preset = 0 WHERE user_query = ?", (phrase,))
    conn.commit()
    conn.close()

def get_analytics():
    conn = get_db()
    c = conn.cursor()
    
    # 真实对话统计
    c.execute('''
        SELECT user_id, COUNT(*) as real_reqs
        FROM conversations
        WHERE is_preset = 0 AND user_id != 'anonymous'
        GROUP BY user_id
        HAVING real_reqs > 0
    ''')
    req_counts = [r[1] for r in c.fetchall()]
    conn.close()
    
    if not req_counts:
        return 0, 0, {}, 0
        
    req_counts.sort()
    total = len(req_counts)
    total_vol = sum(req_counts)
    
    avg = round(total_vol / total, 1)
    median = req_counts[total // 2]
    
    dist = {"1": 0, "2-5": 0, "6-20": 0, "21-100": 0, "100+": 0}
    for count in req_counts:
        if count == 1: dist["1"] += 1
        elif 2 <= count <= 5: dist["2-5"] += 1
        elif 6 <= count <= 20: dist["6-20"] += 1
        elif 21 <= count <= 100: dist["21-100"] += 1
        else: dist["100+"] += 1
        
    top_10_count = max(1, int(total * 0.1))
    top_10_vol = sum(req_counts[-top_10_count:])
    top_10_pct = round((top_10_vol / total_vol) * 100, 1) if total_vol > 0 else 0
    
    return avg, median, dist, top_10_pct

def get_wordcloud():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_query FROM conversations WHERE is_preset = 0 AND user_query != ''")
    queries = [r[0] for r in c.fetchall()]
    conn.close()
    
    text = "\n".join(queries)
    stop_words = {'的', '了', '我', '是', '你', '在', '吗', '这', '那', '有', '个', '和', '就', '不', '人', '都', '一', '一个', '上', '也', '很', '到', '说', '去', '与', '会', '对', '但', '能', '而', '之', '用', '于', '着', '等', '及', '下', '以', '帮', '我', '把', '它', '什么', '可以', '如何', '怎么'}
    
    words = jieba.cut(text)
    counts = defaultdict(int)
    for word in words:
        if len(word) > 1 and word not in stop_words:
            counts[word] += 1
    
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)[:100]

def get_retention_data():
    """获取DAU和留存数据"""
    from datetime import datetime, timedelta
    conn = get_db()
    c = conn.cursor()
    
    today = datetime.now().date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(14)]
    
    c.execute('''
        SELECT date(timestamp) as day, COUNT(DISTINCT user_id) as dau
        FROM conversations
        WHERE date(timestamp) >= date('now', '-14 days')
        GROUP BY day
    ''')
    dau_map = {row[0]: row[1] for row in c.fetchall()}
    
    c.execute('''
        SELECT date(first_seen) as day, COUNT(*) as new_users
        FROM user_sessions
        WHERE date(first_seen) >= date('now', '-14 days')
        GROUP BY day
    ''')
    new_map = {row[0]: row[1] for row in c.fetchall()}
    
    c.execute('''
        SELECT date(first_seen) as day, user_id
        FROM user_sessions
        WHERE date(first_seen) >= date('now', '-21 days')
    ''')
    cohorts = {}
    for row in c.fetchall():
        day = row[0]
        if day not in cohorts:
            cohorts[day] = set()
        cohorts[day].add(row[1])
    
    c.execute('''
        SELECT date(timestamp) as day, user_id
        FROM conversations
        WHERE date(timestamp) >= date('now', '-21 days')
    ''')
    activity = {}
    for row in c.fetchall():
        day = row[0]
        if day not in activity:
            activity[day] = set()
        activity[day].add(row[1])
    
    conn.close()
    
    results = []
    for date_str in dates:
        dau = dau_map.get(date_str, 0)
        new_users = new_map.get(date_str, 0)
        cohort = cohorts.get(date_str, set())
        
        def calc_retention(days_after):
            if not cohort:
                return None
            target_date = (datetime.fromisoformat(date_str) + timedelta(days=days_after)).date().isoformat()
            if target_date > today.isoformat():
                return None
            active_on_target = activity.get(target_date, set())
            retained = len(cohort & active_on_target)
            return round(retained / len(cohort) * 100, 1) if cohort else 0
        
        ret_1 = calc_retention(1)
        ret_3 = calc_retention(3)
        ret_7 = calc_retention(7)
        
        results.append({
            'date': date_str,
            'dau': dau,
            'new': new_users,
            'ret_1': ret_1,
            'ret_3': ret_3,
            'ret_7': ret_7
        })
    
    return results

def set_developer(user_id, is_dev):
    conn = get_db()
    c = conn.cursor()
    # 如果设为开发者，则自动移除黑名单
    is_dev_val = 1 if is_dev else 0
    if is_dev:
        c.execute("UPDATE user_sessions SET is_developer = ?, is_blacklisted = 0 WHERE user_id = ?", (is_dev_val, user_id))
    else:
        c.execute("UPDATE user_sessions SET is_developer = ? WHERE user_id = ?", (is_dev_val, user_id))
    conn.commit()
    conn.close()

def set_blacklist(user_id, is_black):
    conn = get_db()
    c = conn.cursor()
    # 如果设为黑名单，则自动移除开发者身份
    is_black_val = 1 if is_black else 0
    if is_black:
        c.execute("UPDATE user_sessions SET is_blacklisted = ?, is_developer = 0 WHERE user_id = ?", (is_black_val, user_id))
    else:
        c.execute("UPDATE user_sessions SET is_blacklisted = ? WHERE user_id = ?", (is_black_val, user_id))
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
        print(f"DEBUG: do_GET path={self.path}")
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
                    elif action == 'add_blacklist':
                        set_blacklist(user_id, True)
                        message = f'<div class="success">🚫 已将 {user_id[:20]}... 加入黑名单</div>'
                    elif action == 'remove_blacklist':
                        set_blacklist(user_id, False)
                        message = f'<div class="success">✅ 已将 {user_id[:20]}... 移出黑名单</div>'
                    elif action == 'set_nickname':
                        nickname = unquote(query.get('nickname', [''])[0])
                        set_nickname(user_id, nickname)
                        message = f'<div class="success">✅ 已设置昵称: {nickname}</div>'
                except Exception as e:
                    message = f'<div class="error">❌ 操作失败: {e}</div>'
            elif action == 'add_preset':
                try:
                    phrase = unquote(query.get('phrase', [''])[0])
                    add_preset(phrase)
                    message = f'<div class="success">🚫 已添加排除: {phrase}</div>'
                except Exception as e:
                    message = f'<div class="error">❌ 操作失败: {e}</div>'
            elif action == 'remove_preset':
                try:
                    phrase = unquote(query.get('phrase', [''])[0])
                    remove_preset(phrase)
                    message = f'<div class="success">✅ 已移除排除: {phrase}</div>'
                except Exception as e:
                    message = f'<div class="error">❌ 操作失败: {e}</div>'
        
        total, devs, players, avg_all, avg_real, median = get_stats()
        users = get_users()
        
        rows = ""
        for user in users:
            user_id, is_dev, country, requests, mins, nickname, player_name, first_seen_bj, last_seen_bj, is_black = user
            nickname = nickname or ""
            player_name = player_name or ""
            # 显示名称：优先nickname，其次player_name
            display_name = nickname or player_name
            
            if is_black:
                badge = '<span class="black-badge">🚫 黑名单</span>'
                actions = f'<a class="btn btn-player" href="?action=remove_blacklist&user_id={user_id}">移出黑名单</a>'
            elif is_dev:
                badge = '<span class="dev-badge">🔧 开发者</span>'
                actions = f'<a class="btn btn-player" href="?action=remove_dev&user_id={user_id}">设为玩家</a>'
            else:
                badge = '<span class="player-badge">👤 玩家</span>'
                actions = f'''
                    <a class="btn btn-dev" href="?action=add_dev&user_id={user_id}">设为开发</a>
                    <a class="btn btn-black" href="?action=add_blacklist&user_id={user_id}">拉黑</a>
                '''
            
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
            
            # 格式化时间显示 (只显示月日 时分)
            first_time = first_seen_bj[5:16].replace('T', ' ') if first_seen_bj else '-'
            last_time = last_seen_bj[5:16].replace('T', ' ') if last_seen_bj else '-'
            
            rows += f'''<tr>
                <td class="user-id">{user_id}</td>
                <td>{nickname_display}<br>{nickname_form}</td>
                <td>{badge}</td>
                <td>{country or "?"}</td>
                <td style="font-size:12px">{first_time}</td>
                <td style="font-size:12px">{last_time}</td>
                <td>{requests}</td>
                <td>{mins}</td>
                <td>{actions}</td>
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
        
        # 填充 Analytics
        avg, median, dist, top10_pct = get_analytics()
        html = html.replace('%%%AVG_REQS%%%', str(avg))
        html = html.replace('%%%MEDIAN_REQS%%%', str(median))
        html = html.replace('%%%TOP10_VOL%%%', str(top10_pct))
        
        # Distribution Chart
        max_dist = max(dist.values()) if dist else 1
        dist_html = ""
        for label, count in dist.items():
            pct = (count / max_dist) * 100
            dist_html += f'''
            <div class="bar-row">
                <div class="bar-label">{label}</div>
                <div class="bar-track"><div class="bar-fill" style="width: {pct}%"></div></div>
                <div style="width: 30px; font-size: 11px; text-align:right">{count}</div>
            </div>
            '''
        html = html.replace('%%%DIST_CHART%%%', dist_html)
        
        # Word Cloud
        word_data = get_wordcloud()
        wc_html = ""
        if word_data:
            max_count = word_data[0][1]
            for word, count in word_data:
                size = 12 + (count / max_count) * 20  # 12px to 32px
                wc_html += f'<span class="word-tag" style="font-size: {size}px" title="{count}次">{word}</span>'
        html = html.replace('%%%WORD_CLOUD%%%', wc_html or '<div style="color:#666">暂无数据</div>')
        
        # Presets
        presets = get_presets()
        presets_html = ""
        for p in presets:
            presets_html += f'''
            <div class="preset-tag">
                {p} <a href="?action=remove_preset&phrase={p}" class="preset-delete">×</a>
            </div>
            '''
        html = html.replace('%%%PRESETS%%%', presets_html)
        
        # Retention Table
        retention_data = get_retention_data()
        ret_rows = ""
        for row in retention_data:
            def fmt_ret(val):
                if val is None:
                    return '<td style="color:#666">-</td>'
                css = 'ret-good' if val >= 30 else ('ret-ok' if val >= 15 else 'ret-bad')
                return f'<td class="{css}">{val}%</td>'
            
            ret_rows += f'''
            <tr>
                <td>{row['date'][5:]}</td>
                <td>{row['dau']}</td>
                <td>{row['new']}</td>
                {fmt_ret(row['ret_1'])}
                {fmt_ret(row['ret_3'])}
                {fmt_ret(row['ret_7'])}
            </tr>
            '''
        html = html.replace('%%%RETENTION_ROWS%%%', ret_rows)
        
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
