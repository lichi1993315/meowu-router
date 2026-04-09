#!/usr/bin/env python3
"""
开发者管理Web界面 - Docker容器内运行版本
直接访问SQLite数据库和只读JSONL日志目录
支持：开发者标记、昵称管理、黑名单管理、用户JSONL查看
"""

import json
import os
import sqlite3
import hashlib
import hmac
from collections import defaultdict
from datetime import datetime, timedelta
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import jieba

PORT = int(os.getenv("ADMIN_PORT", "9095"))
DB_PATH = os.getenv("DB_PATH", "/app/data/conversations.db")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/app/output")).resolve()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "meowuisland")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
AUTH_SECRET = os.getenv("ADMIN_AUTH_SECRET", "meowuisland-admin-secret")
AUTH_COOKIE_NAME = "developer_admin_auth"


def get_html_template():
    return '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>开发者管理</title>
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
        a { color: #8ce7ff; }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 { color: #00d9ff; text-align: center; margin-bottom: 30px; }
        .page-header { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 20px; }
        .page-header h1 { margin: 0; text-align: left; }
        .top-actions { display: flex; gap: 10px; flex-wrap: wrap; }
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }
        .stat-card, .detail-card {
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            border: 1px solid rgba(255,255,255,0.1);
        }
        .stat-card {
            padding: 20px;
            text-align: center;
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
        th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.1); vertical-align: top; }
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
        .btn-view { background: #7048e8; color: white; }
        .dev-badge { background: #ff6b6b; color: white; padding: 4px 8px; border-radius: 4px; font-size: 12px; }
        .player-badge { background: #51cf66; color: white; padding: 4px 8px; border-radius: 4px; font-size: 12px; }
        .black-badge { background: #343a40; color: white; padding: 4px 8px; border-radius: 4px; font-size: 12px; border: 1px solid #868e96; }
        .success { background: #51cf66; color: white; padding: 10px 20px; border-radius: 8px; margin-bottom: 20px; text-align: center; }
        .error { background: #ff6b6b; color: white; padding: 10px 20px; border-radius: 8px; margin-bottom: 20px; text-align: center; }
        .user-id { font-family: monospace; font-size: 10px; max-width: 280px; overflow: hidden; text-overflow: ellipsis; }
        .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
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
        .retention-table { width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 13px; }
        .retention-table th, .retention-table td { padding: 8px 12px; text-align: center; border-bottom: 1px solid rgba(255,255,255,0.1); }
        .retention-table th { background: rgba(0,217,255,0.1); color: #00d9ff; }
        .retention-table tr:hover { background: rgba(255,255,255,0.03); }
        .ret-good { color: #51cf66; }
        .ret-ok { color: #ffd43b; }
        .ret-bad { color: #ff6b6b; }
        .detail-layout { display: grid; grid-template-columns: 320px minmax(0, 1fr); gap: 20px; align-items: start; }
        .detail-card { padding: 18px; }
        .detail-card h3 { margin-top: 0; color: #8ce7ff; }
        .file-list { display: flex; flex-direction: column; gap: 10px; }
        .file-item {
            display: block;
            padding: 12px;
            border-radius: 10px;
            border: 1px solid rgba(255,255,255,0.08);
            background: rgba(255,255,255,0.03);
            text-decoration: none;
            color: inherit;
        }
        .file-item.active { border-color: rgba(0,217,255,0.45); background: rgba(0,217,255,0.08); }
        .file-meta { color: #9aa6b2; font-size: 12px; margin-top: 6px; }
        .summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            margin-bottom: 16px;
        }
        .summary-item {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 10px;
            padding: 12px;
        }
        .summary-label { color: #9aa6b2; font-size: 12px; margin-bottom: 6px; }
        .summary-value { color: #fff; font-size: 14px; word-break: break-word; }
        .json-block, .raw-block {
            background: rgba(5,10,20,0.72);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
            padding: 14px;
            overflow-x: auto;
        }
        .json-lines { display: flex; flex-direction: column; gap: 14px; }
        .line-card {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
            overflow: hidden;
        }
        .line-header {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            align-items: center;
            padding: 10px 14px;
            background: rgba(255,255,255,0.04);
            border-bottom: 1px solid rgba(255,255,255,0.08);
            font-size: 13px;
        }
        .line-type {
            display: inline-flex;
            padding: 4px 8px;
            border-radius: 999px;
            background: rgba(0,217,255,0.15);
            color: #8ce7ff;
        }
        .line-card pre, .raw-block pre {
            margin: 0;
            white-space: pre-wrap;
            word-break: break-word;
            line-height: 1.5;
            font-size: 13px;
        }
        .muted { color: #94a3b8; }
        .section-title { margin: 0 0 12px 0; color: #ffd43b; }
        .empty { color: #9aa6b2; padding: 20px 0; }
        @media (max-width: 980px) {
            .detail-layout { grid-template-columns: 1fr; }
            .page-header { flex-direction: column; align-items: flex-start; }
        }
    </style>
</head>
<body>
    <div class="container">%%%BODY%%%</div>
</body>
</html>'''


def html_page(body: str) -> bytes:
    return get_html_template().replace("%%%BODY%%%", body).encode("utf-8")


def sign_auth_value(username):
    return hmac.new(AUTH_SECRET.encode("utf-8"), username.encode("utf-8"), hashlib.sha256).hexdigest()


def build_auth_cookie(username):
    return f"{username}:{sign_auth_value(username)}"


def parse_cookies(cookie_header):
    cookies = {}
    if not cookie_header:
        return cookies
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def is_authenticated(cookie_header):
    cookie_value = parse_cookies(cookie_header).get(AUTH_COOKIE_NAME)
    if not cookie_value or ":" not in cookie_value:
        return False
    username, signature = cookie_value.split(":", 1)
    if username != ADMIN_USERNAME:
        return False
    expected = sign_auth_value(username)
    return hmac.compare_digest(signature, expected)


def render_login_page(message=""):
    body = f'''
    <div style="max-width:420px; margin:80px auto;">
        <div class="detail-card" style="padding:28px;">
            <h1 style="text-align:center; margin-top:0;">开发者登录</h1>
            {message}
            <form method="post" action="/login" style="display:flex; flex-direction:column; gap:14px;">
                <div>
                    <div class="muted" style="margin-bottom:6px;">用户名</div>
                    <input type="text" name="username" class="nickname-input" style="width:100%; height:40px;" autocomplete="username" required>
                </div>
                <div>
                    <div class="muted" style="margin-bottom:6px;">密码</div>
                    <input type="password" name="password" class="nickname-input" style="width:100%; height:40px;" autocomplete="current-password" required>
                </div>
                <button type="submit" class="btn btn-view" style="width:100%; height:42px;">登录</button>
            </form>
        </div>
    </div>
    '''
    return html_page(body)


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
    c.execute(
        '''
        SELECT user_id, COALESCE(is_developer, 0), country, total_requests,
               CAST((julianday(last_seen) - julianday(first_seen)) * 24 * 60 AS INTEGER),
               nickname, player_name,
               datetime(first_seen, '+8 hours') as first_seen_bj,
               datetime(last_seen, '+8 hours') as last_seen_bj,
               COALESCE(is_blacklisted, 0)
        FROM user_sessions WHERE total_requests >= 2 ORDER BY total_requests DESC
        '''
    )
    users = c.fetchall()
    conn.close()
    return users


def get_presets():
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("SELECT phrase FROM preset_phrases")
    except Exception:
        conn.close()
        return []
    presets = [r[0] for r in c.fetchall()]
    conn.close()
    return presets


def add_preset(phrase):
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("INSERT OR IGNORE INTO preset_phrases (phrase) VALUES (?)", (phrase,))
        c.execute("UPDATE conversations SET is_preset = 1 WHERE user_query = ?", (phrase,))
    except Exception:
        c.execute("CREATE TABLE IF NOT EXISTS preset_phrases (id INTEGER PRIMARY KEY, phrase TEXT UNIQUE)")
        c.execute("INSERT OR IGNORE INTO preset_phrases (phrase) VALUES (?)", (phrase,))
    conn.commit()
    conn.close()


def remove_preset(phrase):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM preset_phrases WHERE phrase = ?", (phrase,))
    c.execute("UPDATE conversations SET is_preset = 0 WHERE user_query = ?", (phrase,))
    conn.commit()
    conn.close()


def get_analytics():
    conn = get_db()
    c = conn.cursor()
    c.execute(
        '''
        SELECT user_id, COUNT(*) as real_reqs
        FROM conversations
        WHERE is_preset = 0 AND user_id != 'anonymous'
        GROUP BY user_id
        HAVING real_reqs > 0
        '''
    )
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
        if count == 1:
            dist["1"] += 1
        elif 2 <= count <= 5:
            dist["2-5"] += 1
        elif 6 <= count <= 20:
            dist["6-20"] += 1
        elif 21 <= count <= 100:
            dist["21-100"] += 1
        else:
            dist["100+"] += 1
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
    stop_words = {
        '的', '了', '我', '是', '你', '在', '吗', '这', '那', '有', '个', '和', '就', '不',
        '人', '都', '一', '一个', '上', '也', '很', '到', '说', '去', '与', '会', '对', '但',
        '能', '而', '之', '用', '于', '着', '等', '及', '下', '以', '帮', '把', '它', '什么',
        '可以', '如何', '怎么'
    }
    counts = defaultdict(int)
    for word in jieba.cut(text):
        if len(word) > 1 and word not in stop_words:
            counts[word] += 1
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)[:100]


def get_retention_data():
    conn = get_db()
    c = conn.cursor()
    today = datetime.now().date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(14)]
    c.execute(
        '''
        SELECT date(timestamp) as day, COUNT(DISTINCT user_id) as dau
        FROM conversations
        WHERE date(timestamp) >= date('now', '-14 days')
        GROUP BY day
        '''
    )
    dau_map = {row[0]: row[1] for row in c.fetchall()}
    c.execute(
        '''
        SELECT date(first_seen) as day, COUNT(*) as new_users
        FROM user_sessions
        WHERE date(first_seen) >= date('now', '-14 days')
        GROUP BY day
        '''
    )
    new_map = {row[0]: row[1] for row in c.fetchall()}
    c.execute(
        '''
        SELECT date(first_seen) as day, user_id
        FROM user_sessions
        WHERE date(first_seen) >= date('now', '-21 days')
        '''
    )
    cohorts = {}
    for day, user_id in c.fetchall():
        cohorts.setdefault(day, set()).add(user_id)
    c.execute(
        '''
        SELECT date(timestamp) as day, user_id
        FROM conversations
        WHERE date(timestamp) >= date('now', '-21 days')
        '''
    )
    activity = {}
    for day, user_id in c.fetchall():
        activity.setdefault(day, set()).add(user_id)
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

        results.append({
            "date": date_str,
            "dau": dau,
            "new": new_users,
            "ret_1": calc_retention(1),
            "ret_3": calc_retention(3),
            "ret_7": calc_retention(7),
        })
    return results


def set_developer(user_id, is_dev):
    conn = get_db()
    c = conn.cursor()
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


def get_user_profile(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        '''
        SELECT user_id, COALESCE(is_developer, 0), COALESCE(is_blacklisted, 0), country,
               total_requests, nickname, player_name,
               datetime(first_seen, '+8 hours'),
               datetime(last_seen, '+8 hours')
        FROM user_sessions
        WHERE user_id = ?
        ''',
        (user_id,),
    )
    row = c.fetchone()
    conn.close()
    return row


def get_user_conversation_summary(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        '''
        SELECT COUNT(*) as total_records,
               SUM(CASE WHEN message_type = 'chat' THEN 1 ELSE 0 END) as chat_count,
               SUM(CASE WHEN message_type = 'login' THEN 1 ELSE 0 END) as login_count,
               SUM(CASE WHEN message_type = 'logoff' THEN 1 ELSE 0 END) as logoff_count,
               COUNT(DISTINCT session_id) as session_count,
               MAX(datetime(timestamp, '+8 hours')) as last_event_bj
        FROM conversations
        WHERE user_id = ?
        ''',
        (user_id,),
    )
    row = c.fetchone()
    conn.close()
    return row


def get_recent_user_records(user_id, limit=20):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        '''
        SELECT datetime(timestamp, '+8 hours') as ts_bj,
               message_type,
               COALESCE(session_id, ''),
               COALESCE(client_version, ''),
               COALESCE(ai_action, ''),
               COALESCE(substr(user_query, 1, 120), ''),
               COALESCE(substr(file_path, 1, 200), '')
        FROM conversations
        WHERE user_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
        ''',
        (user_id, limit),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def safe_user_dir(user_id):
    if not user_id:
        return None
    user_dir = (OUTPUT_DIR / user_id).resolve()
    try:
        user_dir.relative_to(OUTPUT_DIR)
    except ValueError:
        return None
    return user_dir


FILES_PER_PAGE = 30
LINES_PER_PAGE = 50


def parse_positive_int(value, default):
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except Exception:
        return default


def paginate_items(items, page, page_size):
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), total_pages)
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end], total, total_pages, page


def build_query(base_path, **params):
    parts = []
    for key, value in params.items():
        if value is None or value == "":
            continue
        parts.append(f"{quote(str(key))}={quote(str(value))}")
    return f"{base_path}?{'&'.join(parts)}" if parts else base_path


def render_pagination(base_path, current_page, total_pages, extra_params, page_param="page"):
    if total_pages <= 1:
        return ""

    links = []
    if current_page > 1:
        links.append(f'<a class="btn btn-player" href="{build_query(base_path, **extra_params, **{page_param: current_page - 1})}">上一页</a>')

    start = max(1, current_page - 2)
    end = min(total_pages, current_page + 2)
    for page in range(start, end + 1):
        if page == current_page:
            links.append(f'<span class="btn btn-view">{page}</span>')
        else:
            links.append(f'<a class="btn btn-player" href="{build_query(base_path, **extra_params, **{page_param: page})}">{page}</a>')

    if current_page < total_pages:
        links.append(f'<a class="btn btn-player" href="{build_query(base_path, **extra_params, **{page_param: current_page + 1})}">下一页</a>')

    return f'<div style="margin-top:12px; display:flex; flex-wrap:wrap; gap:8px;">{"".join(links)}</div>'


def render_user_pagination(user_id, filename, file_page, file_total_pages, line_page, line_total_pages):
    sections = []

    if file_total_pages > 1:
        links = []
        if file_page > 1:
            links.append(
                f'<a class="btn btn-player" href="{build_query("/user", user_id=user_id, file=filename, file_page=file_page - 1, line_page=line_page)}">文件上一页</a>'
            )
        if file_page < file_total_pages:
            links.append(
                f'<a class="btn btn-player" href="{build_query("/user", user_id=user_id, file=filename, file_page=file_page + 1, line_page=line_page)}">文件下一页</a>'
            )
        sections.append(f'<div style="display:flex; flex-wrap:wrap; gap:8px; align-items:center;"><span class="muted">文件页 {file_page}/{file_total_pages}</span>{"".join(links)}</div>')

    if line_total_pages > 1:
        links = []
        if line_page > 1:
            links.append(
                f'<a class="btn btn-player" href="{build_query("/user", user_id=user_id, file=filename, file_page=file_page, line_page=line_page - 1)}">内容上一页</a>'
            )
        if line_page < line_total_pages:
            links.append(
                f'<a class="btn btn-player" href="{build_query("/user", user_id=user_id, file=filename, file_page=file_page, line_page=line_page + 1)}">内容下一页</a>'
            )
        sections.append(f'<div style="display:flex; flex-wrap:wrap; gap:8px; align-items:center;"><span class="muted">内容页 {line_page}/{line_total_pages}</span>{"".join(links)}</div>')

    return "".join(f'<div style="margin-top:10px;">{section}</div>' for section in sections)


def get_user_jsonl_files(user_id):
    user_dir = safe_user_dir(user_id)
    if not user_dir or not user_dir.exists():
        return []
    files = []
    for path in sorted(user_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = path.stat()
        files.append({
            "name": path.name,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return files


def resolve_user_jsonl_file(user_id, filename):
    if not filename or "/" in filename or "\\" in filename:
        return None
    if not filename.endswith(".jsonl"):
        return None
    user_dir = safe_user_dir(user_id)
    if not user_dir:
        return None
    target = (user_dir / filename).resolve()
    try:
        target.relative_to(user_dir)
    except ValueError:
        return None
    if not target.exists() or not target.is_file():
        return None
    return target


def load_jsonl_page(user_id, filename, page=1, page_size=LINES_PER_PAGE):
    path = resolve_user_jsonl_file(user_id, filename)
    if not path:
        return None

    page_size = max(1, page_size)
    page = max(1, page)
    start_index = (page - 1) * page_size + 1
    end_index = start_index + page_size - 1

    page_lines = []
    json_type_counts = defaultdict(int)
    session_id = ""
    user_id_in_file = ""
    first_timestamp = ""
    last_timestamp = ""
    line_count = 0

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            if not raw_line.strip():
                continue
            line_count += 1
            raw_line = raw_line.rstrip("\n")
            pretty = raw_line
            line_type = "value"
            timestamp = ""
            raw_len = len(raw_line)

            try:
                parsed = json.loads(raw_line)
                pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
                if isinstance(parsed, dict):
                    line_type = parsed.get("type", "unknown")
                    timestamp = parsed.get("timestamp") or parsed.get("created_at") or parsed.get("updated_at") or ""
                    session_id = session_id or parsed.get("session_id", "")
                    user_id_in_file = user_id_in_file or parsed.get("user_id", "")
                    json_type_counts[line_type] += 1
                else:
                    line_type = "value"
            except Exception as exc:
                pretty = f"[Invalid JSON] {exc}\n{raw_line}"
                line_type = "invalid"
                json_type_counts["invalid"] += 1

            if timestamp and not first_timestamp:
                first_timestamp = timestamp
            if timestamp:
                last_timestamp = timestamp

            if start_index <= line_count <= end_index:
                page_lines.append({
                    "index": line_count,
                    "type": line_type,
                    "timestamp": timestamp,
                    "pretty": pretty,
                    "raw_len": raw_len,
                })

    total_pages = max(1, (line_count + page_size - 1) // page_size)
    page = min(page, total_pages)
    visible_start = 0 if line_count == 0 else (page - 1) * page_size + 1
    visible_end = min(page * page_size, line_count)

    return {
        "path": path,
        "lines": page_lines,
        "line_count": line_count,
        "current_page": page,
        "total_pages": total_pages,
        "page_size": page_size,
        "visible_start": visible_start,
        "visible_end": visible_end,
        "session_id": session_id,
        "user_id": user_id_in_file,
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "type_counts": dict(json_type_counts),
        "size": path.stat().st_size,
    }


def load_raw_text(user_id, filename):
    path = resolve_user_jsonl_file(user_id, filename)
    if not path:
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def format_bytes(size):
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def render_main_page(message):
    total, devs, players, avg_all, avg_real, median = get_stats()
    rows = ""
    for user in get_users():
        user_id, is_dev, country, requests, mins, nickname, player_name, first_seen_bj, last_seen_bj, is_black = user
        nickname = nickname or ""
        player_name = player_name or ""
        if is_black:
            badge = '<span class="black-badge">黑名单</span>'
            actions = f'<a class="btn btn-player" href="/?action=remove_blacklist&user_id={quote(user_id)}">移出黑名单</a>'
        elif is_dev:
            badge = '<span class="dev-badge">开发者</span>'
            actions = f'<a class="btn btn-player" href="/?action=remove_dev&user_id={quote(user_id)}">设为玩家</a>'
        else:
            badge = '<span class="player-badge">玩家</span>'
            actions = (
                f'<a class="btn btn-dev" href="/?action=add_dev&user_id={quote(user_id)}">设为开发</a>'
                f'<a class="btn btn-black" href="/?action=add_blacklist&user_id={quote(user_id)}">拉黑</a>'
            )
        actions += f'<a class="btn btn-view" href="/user?user_id={quote(user_id)}">查看 JSONL</a>'
        if nickname:
            nickname_display = f'<span class="nickname-display">{escape(nickname)}</span>'
        elif player_name:
            nickname_display = f'<span style="color:#aaa">🎮 {escape(player_name)}</span>'
        else:
            nickname_display = '<span style="color:#666">未设置</span>'
        nickname_form = f'''
        <form class="nickname-form" method="get" action="/">
            <input type="hidden" name="action" value="set_nickname">
            <input type="hidden" name="user_id" value="{escape(user_id)}">
            <input type="text" name="nickname" class="nickname-input" placeholder="输入昵称" value="{escape(nickname)}">
            <button type="submit" class="btn btn-save">保存</button>
        </form>
        '''
        first_time = first_seen_bj[5:16].replace('T', ' ') if first_seen_bj else '-'
        last_time = last_seen_bj[5:16].replace('T', ' ') if last_seen_bj else '-'
        rows += f'''<tr>
            <td class="user-id">{escape(user_id)}</td>
            <td>{nickname_display}<br>{nickname_form}</td>
            <td>{badge}</td>
            <td>{escape(country or "?")}</td>
            <td style="font-size:12px">{escape(first_time)}</td>
            <td style="font-size:12px">{escape(last_time)}</td>
            <td>{requests}</td>
            <td>{mins}</td>
            <td>{actions}</td>
        </tr>'''

    avg, median_reqs, dist, top10_pct = get_analytics()
    max_dist = max(dist.values()) if dist else 1
    dist_html = ""
    for label, count in dist.items():
        pct = (count / max_dist) * 100 if max_dist else 0
        dist_html += f'''
        <div class="bar-row">
            <div class="bar-label">{escape(label)}</div>
            <div class="bar-track"><div class="bar-fill" style="width: {pct}%"></div></div>
            <div style="width: 30px; font-size: 11px; text-align:right">{count}</div>
        </div>
        '''

    word_data = get_wordcloud()
    wc_html = ""
    if word_data:
        max_count = word_data[0][1]
        for word, count in word_data:
            size = 12 + (count / max_count) * 20
            wc_html += f'<span class="word-tag" style="font-size: {size}px" title="{count}次">{escape(word)}</span>'
    else:
        wc_html = '<div style="color:#666">暂无数据</div>'

    presets_html = ""
    for phrase in get_presets():
        presets_html += f'<div class="preset-tag">{escape(phrase)} <a href="/?action=remove_preset&phrase={quote(phrase)}" class="preset-delete">×</a></div>'

    ret_rows = ""
    for row in get_retention_data():
        def fmt_ret(val):
            if val is None:
                return '<td style="color:#666">-</td>'
            css = 'ret-good' if val >= 30 else ('ret-ok' if val >= 15 else 'ret-bad')
            return f'<td class="{css}">{val}%</td>'

        ret_rows += f'''
        <tr>
            <td>{escape(row["date"][5:])}</td>
            <td>{row["dau"]}</td>
            <td>{row["new"]}</td>
            {fmt_ret(row["ret_1"])}
            {fmt_ret(row["ret_3"])}
            {fmt_ret(row["ret_7"])}
        </tr>
        '''

    body = f'''
    <h1>用户管理后台</h1>
    {message}
    <div style="display:flex; justify-content:flex-end; margin-bottom:14px;">
        <a class="btn btn-black" href="/logout">退出登录</a>
    </div>
    <div class="stats">
        <div class="stat-card"><div class="stat-value">{total}</div><div class="stat-label">有效用户</div></div>
        <div class="stat-card"><div class="stat-value">{devs}</div><div class="stat-label">开发者</div></div>
        <div class="stat-card"><div class="stat-value">{players}</div><div class="stat-label">真实玩家</div></div>
        <div class="stat-card"><div class="stat-value">{avg_all}</div><div class="stat-label">全体平均(分)</div></div>
        <div class="stat-card"><div class="stat-value">{avg_real}</div><div class="stat-label">真实平均(分)</div></div>
        <div class="stat-card"><div class="stat-value">{median}</div><div class="stat-label">中位数(分)</div></div>
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
        <tbody>{rows}</tbody>
    </table>
    <div class="analytics-section">
        <h2 class="h2-title">深度分析 (真实对话)</h2>
        <div class="chart-container">
            <div class="chart-box">
                <h3>对话分布</h3>
                <div class="bar-chart">{dist_html}</div>
                <div style="margin-top: 15px; font-size: 0.9em; color:#aaa">
                    平均对话: <span style="color:#fff">{avg}</span> |
                    中位数: <span style="color:#fff">{median_reqs}</span>
                </div>
            </div>
            <div class="chart-box">
                <h3>头部效应 (Whales)</h3>
                <div style="margin-top: 10px; line-height: 1.6;">
                    Top 10% 用户贡献了 <strong style="color:#ff6b6b; font-size: 1.2em">{top10_pct}</strong> % 的流量
                </div>
            </div>
        </div>
        <h2 class="h2-title">热门词云 (Top 100)</h2>
        <div class="chart-box"><div class="word-cloud">{wc_html}</div></div>
        <h2 class="h2-title">预设对话排除</h2>
        <div class="chart-box">
            <p style="color:#888; font-size: 0.9em;">以下文本将不计入真实对话统计，且不会出现在词云中。</p>
            <form method="get" action="/" style="display:flex; gap:10px; margin-bottom:15px;">
                <input type="hidden" name="action" value="add_preset">
                <input type="text" name="phrase" class="nickname-input" style="width: 300px;" placeholder="输入要排除的预设对话..." required>
                <button type="submit" class="btn btn-save">添加排除</button>
            </form>
            <div class="preset-list">{presets_html}</div>
        </div>
        <h2 class="h2-title">用户增长 & 留存</h2>
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
                <tbody>{ret_rows}</tbody>
            </table>
        </div>
    </div>
    '''
    return html_page(body)


def render_user_detail_page(user_id, filename, file_page, line_page, message):
    profile = get_user_profile(user_id)
    if not profile:
        return html_page(
            f'''
            <div class="page-header">
                <h1>用户 JSONL 查看</h1>
                <div class="top-actions"><a class="btn btn-player" href="/">返回列表</a></div>
            </div>
            <div class="error">未找到用户: {escape(user_id)}</div>
            '''
        )

    all_files = get_user_jsonl_files(user_id)
    paged_files, file_total, file_total_pages, file_page = paginate_items(all_files, file_page, FILES_PER_PAGE)
    selected_name = filename or (paged_files[0]["name"] if paged_files else (all_files[0]["name"] if all_files else ""))
    selected = load_jsonl_page(user_id, selected_name, page=line_page) if selected_name else None
    summary = get_user_conversation_summary(user_id)
    recent_rows = get_recent_user_records(user_id)
    if filename and not selected:
        message = f'<div class="error">日志文件不存在或不可访问: {escape(filename)}</div>'
        if all_files:
            selected_name = all_files[0]["name"]
            selected = load_jsonl_page(user_id, selected_name, page=1)

    if selected_name and all_files:
        selected_index = next((idx for idx, item in enumerate(all_files) if item["name"] == selected_name), None)
        if selected_index is not None:
            file_page = selected_index // FILES_PER_PAGE + 1
            paged_files, file_total, file_total_pages, file_page = paginate_items(all_files, file_page, FILES_PER_PAGE)

    user_id_val, is_dev, is_black, country, total_requests, nickname, player_name, first_seen_bj, last_seen_bj = profile
    total_records, chat_count, login_count, logoff_count, session_count, last_event_bj = summary or (0, 0, 0, 0, 0, "")

    file_links = ""
    for file_info in paged_files:
        active = " active" if file_info["name"] == selected_name else ""
        href = build_query("/user", user_id=user_id, file=file_info["name"], file_page=file_page, line_page=1)
        file_links += f'''
        <a class="file-item{active}" href="{href}">
            <div class="mono">{escape(file_info["name"])}</div>
            <div class="file-meta">{escape(file_info["mtime"])} · {escape(format_bytes(file_info["size"]))}</div>
        </a>
        '''
    if not file_links:
        file_links = '<div class="empty">当前用户目录下没有 .jsonl 文件。</div>'
    else:
        file_links += f'<div class="muted" style="margin-top:10px;">文件 {((file_page - 1) * FILES_PER_PAGE + 1) if file_total else 0}-{min(file_page * FILES_PER_PAGE, file_total)} / {file_total}</div>'
        file_links += render_pagination("/user", file_page, file_total_pages, {"user_id": user_id, "file": selected_name, "line_page": 1}, page_param="file_page")

    summary_cards = f'''
    <div class="summary-grid">
        <div class="summary-item"><div class="summary-label">用户ID</div><div class="summary-value mono">{escape(user_id_val)}</div></div>
        <div class="summary-item"><div class="summary-label">昵称</div><div class="summary-value">{escape(nickname or player_name or "未设置")}</div></div>
        <div class="summary-item"><div class="summary-label">国家</div><div class="summary-value">{escape(country or "?")}</div></div>
        <div class="summary-item"><div class="summary-label">总请求</div><div class="summary-value">{total_requests or 0}</div></div>
        <div class="summary-item"><div class="summary-label">会话数</div><div class="summary-value">{session_count or 0}</div></div>
        <div class="summary-item"><div class="summary-label">结构化记录数</div><div class="summary-value">{total_records or 0}</div></div>
        <div class="summary-item"><div class="summary-label">类型</div><div class="summary-value">{'黑名单' if is_black else ('开发者' if is_dev else '玩家')}</div></div>
        <div class="summary-item"><div class="summary-label">首次游玩(北京)</div><div class="summary-value">{escape(first_seen_bj or "-")}</div></div>
        <div class="summary-item"><div class="summary-label">最后游玩(北京)</div><div class="summary-value">{escape(last_seen_bj or "-")}</div></div>
        <div class="summary-item"><div class="summary-label">最近事件(北京)</div><div class="summary-value">{escape(last_event_bj or "-")}</div></div>
        <div class="summary-item"><div class="summary-label">chat / login / logoff</div><div class="summary-value">{chat_count or 0} / {login_count or 0} / {logoff_count or 0}</div></div>
    </div>
    '''

    selected_html = '<div class="empty">请选择左侧文件查看。</div>'
    if selected:
        type_summary = " / ".join(f"{escape(k)}: {v}" for k, v in sorted(selected["type_counts"].items())) or "-"
        line_html = ""
        for item in selected["lines"]:
            line_html += f'''
            <div class="line-card">
                <div class="line-header">
                    <div>第 {item["index"]} 行 <span class="line-type">{escape(item["type"])}</span></div>
                    <div class="muted mono">{escape(item["timestamp"] or "-")}</div>
                </div>
                <div class="json-block"><pre>{escape(item["pretty"])}</pre></div>
            </div>
            '''
        selected_html = f'''
        <div class="detail-card">
            <h3>文件摘要</h3>
            <div class="muted" style="margin-bottom:12px;">
                当前显示第 {selected["visible_start"]}-{selected["visible_end"]} 行，共 {selected["line_count"]} 行
            </div>
            <div class="summary-grid">
                <div class="summary-item"><div class="summary-label">文件名</div><div class="summary-value mono">{escape(selected["path"].name)}</div></div>
                <div class="summary-item"><div class="summary-label">大小</div><div class="summary-value">{escape(format_bytes(selected["size"]))}</div></div>
                <div class="summary-item"><div class="summary-label">JSON 行数</div><div class="summary-value">{selected["line_count"]}</div></div>
                <div class="summary-item"><div class="summary-label">session_id</div><div class="summary-value mono">{escape(selected["session_id"] or "-")}</div></div>
                <div class="summary-item"><div class="summary-label">文件内 user_id</div><div class="summary-value mono">{escape(selected["user_id"] or "-")}</div></div>
                <div class="summary-item"><div class="summary-label">类型分布</div><div class="summary-value">{type_summary}</div></div>
                <div class="summary-item"><div class="summary-label">首时间戳</div><div class="summary-value mono">{escape(selected["first_timestamp"] or "-")}</div></div>
                <div class="summary-item"><div class="summary-label">末时间戳</div><div class="summary-value mono">{escape(selected["last_timestamp"] or "-")}</div></div>
                <div class="summary-item"><div class="summary-label">容器内路径</div><div class="summary-value mono">{escape(str(selected["path"]))}</div></div>
            </div>
            <div style="display:flex; flex-wrap:wrap; gap:8px;">
                <a class="btn btn-view" href="{build_query("/raw", user_id=user_id, file=selected_name)}" target="_blank">打开完整原文</a>
                <a class="btn btn-player" href="{build_query("/raw", user_id=user_id, file=selected_name, download=1)}">下载原文</a>
            </div>
            {render_pagination("/user", selected["current_page"], selected["total_pages"], {"user_id": user_id, "file": selected_name, "file_page": file_page}, page_param="line_page")}
        </div>
        <div class="detail-card">
            <h3 class="section-title">格式化 JSON</h3>
            <div class="json-lines">{line_html}</div>
        </div>
        '''

    recent_html = ""
    for ts_bj, message_type, session_id, client_version, ai_action, user_query, file_path in recent_rows:
        recent_html += f'''
        <tr>
            <td>{escape(ts_bj or "-")}</td>
            <td>{escape(message_type or "-")}</td>
            <td class="mono">{escape(session_id or "-")}</td>
            <td>{escape(client_version or "-")}</td>
            <td>{escape(ai_action or "-")}</td>
            <td>{escape(user_query or "-")}</td>
            <td class="mono">{escape(file_path or "-")}</td>
        </tr>
        '''
    if not recent_html:
        recent_html = '<tr><td colspan="7" class="empty">暂无 conversations 记录。</td></tr>'

    body = f'''
    <div class="page-header">
        <h1>用户 JSONL 查看</h1>
        <div class="top-actions">
            <a class="btn btn-player" href="/">返回列表</a>
            <a class="btn btn-view" href="{build_query("/user", user_id=user_id, file=selected_name, file_page=file_page, line_page=selected["current_page"] if selected else 1)}">刷新当前用户</a>
            <a class="btn btn-black" href="/logout">退出登录</a>
        </div>
    </div>
    {message}
    {summary_cards}
    <div class="detail-layout">
        <div class="detail-card">
            <h3>用户文件</h3>
            <div class="file-list">{file_links}</div>
        </div>
        <div style="display:flex; flex-direction:column; gap:20px;">
            {selected_html}
            <div class="detail-card">
                <h3 class="section-title">最近结构化记录</h3>
                <table>
                    <thead>
                        <tr>
                            <th>时间(北京)</th>
                            <th>类型</th>
                            <th>Session</th>
                            <th>版本</th>
                            <th>动作</th>
                            <th>摘要</th>
                            <th>file_path</th>
                        </tr>
                    </thead>
                    <tbody>{recent_html}</tbody>
                </table>
            </div>
        </div>
    </div>
    '''
    return html_page(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_html(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body, status=200, download_name=None):
        self.send_response(status)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _redirect(self, location, cookie=None, clear_cookie=False):
        self.send_response(302)
        self.send_header("Location", location)
        if cookie is not None:
            self.send_header("Set-Cookie", f"{AUTH_COOKIE_NAME}={cookie}; Path=/; HttpOnly; SameSite=Lax")
        elif clear_cookie:
            self.send_header("Set-Cookie", f"{AUTH_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        self.end_headers()

    def _is_authenticated(self):
        return is_authenticated(self.headers.get("Cookie", ""))

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        message = ""

        if parsed.path == "/login":
            if self._is_authenticated():
                self._redirect("/")
                return
            self._send_html(render_login_page())
            return

        if parsed.path == "/logout":
            self._redirect("/login", clear_cookie=True)
            return

        if not self._is_authenticated():
            self._redirect("/login")
            return

        if parsed.path == "/":
            if "action" in query:
                action = query["action"][0]
                user_id = query.get("user_id", [""])[0]
                try:
                    if action == "add_dev" and user_id:
                        set_developer(user_id, True)
                        message = f'<div class="success">已将 {escape(user_id[:20])}... 标记为开发者</div>'
                    elif action == "remove_dev" and user_id:
                        set_developer(user_id, False)
                        message = f'<div class="success">已将 {escape(user_id[:20])}... 设为普通玩家</div>'
                    elif action == "add_blacklist" and user_id:
                        set_blacklist(user_id, True)
                        message = f'<div class="success">已将 {escape(user_id[:20])}... 加入黑名单</div>'
                    elif action == "remove_blacklist" and user_id:
                        set_blacklist(user_id, False)
                        message = f'<div class="success">已将 {escape(user_id[:20])}... 移出黑名单</div>'
                    elif action == "set_nickname" and user_id:
                        nickname = unquote(query.get("nickname", [""])[0])
                        set_nickname(user_id, nickname)
                        message = f'<div class="success">已设置昵称: {escape(nickname)}</div>'
                    elif action == "add_preset":
                        phrase = unquote(query.get("phrase", [""])[0])
                        add_preset(phrase)
                        message = f'<div class="success">已添加排除: {escape(phrase)}</div>'
                    elif action == "remove_preset":
                        phrase = unquote(query.get("phrase", [""])[0])
                        remove_preset(phrase)
                        message = f'<div class="success">已移除排除: {escape(phrase)}</div>'
                except Exception as exc:
                    message = f'<div class="error">操作失败: {escape(str(exc))}</div>'
            self._send_html(render_main_page(message))
            return

        if parsed.path == "/user":
            user_id = query.get("user_id", [""])[0]
            filename = query.get("file", [""])[0] or None
            file_page = parse_positive_int(query.get("file_page", ["1"])[0], 1)
            line_page = parse_positive_int(query.get("line_page", ["1"])[0], 1)
            if not user_id:
                self._send_html(html_page('<div class="error">缺少 user_id</div><a class="btn btn-player" href="/">返回列表</a>'), 400)
                return
            self._send_html(render_user_detail_page(user_id, filename, file_page, line_page, message))
            return

        if parsed.path == "/raw":
            user_id = query.get("user_id", [""])[0]
            filename = query.get("file", [""])[0]
            if not user_id or not filename:
                self._send_html(html_page('<div class="error">缺少 user_id 或 file</div><a class="btn btn-player" href="/">返回列表</a>'), 400)
                return
            raw_text = load_raw_text(user_id, filename)
            if raw_text is None:
                self._send_html(html_page('<div class="error">日志文件不存在或不可访问</div><a class="btn btn-player" href="/">返回列表</a>'), 404)
                return
            download = query.get("download", ["0"])[0] == "1"
            self._send_text(raw_text, download_name=filename if download else None)
            return

        self._send_html(html_page('<div class="error">页面不存在</div><a class="btn btn-player" href="/">返回列表</a>'), 404)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path != "/login":
            if not self._is_authenticated():
                self._redirect("/login")
                return
            self._send_html(html_page('<div class="error">页面不存在</div><a class="btn btn-player" href="/">返回列表</a>'), 404)
            return

        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        form = parse_qs(raw_body)
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            self._redirect("/", cookie=build_auth_cookie(username))
            return

        self._send_html(render_login_page('<div class="error">用户名或密码错误</div>'), 401)


def main():
    print(f"用户管理后台启动 - 端口 {PORT}")
    print(f"数据库: {DB_PATH}")
    print(f"日志目录: {OUTPUT_DIR}")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
