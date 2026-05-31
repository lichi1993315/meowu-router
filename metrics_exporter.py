#!/usr/bin/env python3
"""
LLM Router Metrics Exporter

扫描JSONL日志文件，暴露Prometheus指标并存储对话记录到SQLite。

功能:
- 增量扫描output目录下的JSONL文件
- 暴露Prometheus HTTP端口 (:9090/metrics)
- 提取用户对话(<user_query>)和AI回复存入SQLite
- 计算CCU、游玩时长、地域分布等指标
"""

import os
import re
import json
import time
import sqlite3
import logging
import jieba
from pathlib import Path
from datetime import datetime, timedelta
from threading import Thread, Lock
from collections import defaultdict
from prometheus_client import (
    Counter, Histogram, Gauge, Info,
    start_http_server, REGISTRY
)

# ============ 配置 ============
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./output"))
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "9090"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))  # 秒
CCU_WINDOW = int(os.getenv("CCU_WINDOW", "300"))  # CCU统计窗口(秒)

# Gemini定价 ($/1M tokens) - 基于 Gemini 1.5 Pro 价格 (用户反馈实际消耗匹配Pro)
# Input: $3.50, Output: $10.50, Cached: $0.875
PRICE_PROMPT = float(os.getenv("PRICE_PROMPT", "3.50"))
PRICE_COMPLETION = float(os.getenv("PRICE_COMPLETION", "10.50"))
PRICE_CACHED = float(os.getenv("PRICE_CACHED", "0.875"))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============ Prometheus Metrics ============

# 请求计数器
requests_total = Counter(
    'llm_requests_total',
    'Total number of LLM requests',
    ['user_id', 'country', 'model', 'status']
)

# 延迟直方图
request_duration = Histogram(
    'llm_request_duration_seconds',
    'Request duration in seconds',
    ['user_id', 'country', 'model'],
    buckets=[0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60]
)

# Token计数器
prompt_tokens_total = Counter(
    'llm_prompt_tokens_total',
    'Total prompt tokens used',
    ['user_id', 'model']
)

completion_tokens_total = Counter(
    'llm_completion_tokens_total', 
    'Total completion tokens used',
    ['user_id', 'model']
)

cached_tokens_total = Counter(
    'llm_cached_tokens_total',
    'Total cached tokens',
    ['user_id', 'model']
)

# 带宽计数器
bandwidth_bytes = Counter(
    'llm_bandwidth_bytes_total',
    'Total bandwidth in bytes',
    ['user_id', 'direction']  # direction: in/out
)

# 实时Gauge
active_users = Gauge(
    'llm_active_users',
    'Number of active users in the last 5 minutes',
    ['country']
)

total_active_users = Gauge(
    'llm_total_active_users',
    'Total number of active users across all countries'
)

# 用户统计
user_total_requests = Gauge(
    'llm_user_total_requests',
    'Total requests per user',
    ['user_id', 'country']
)

user_play_seconds = Gauge(
    'llm_user_play_seconds',
    'Total play time in seconds per user',
    ['user_id', 'country']
)

user_session_count = Gauge(
    'llm_user_session_count',
    'Total play session count per user',
    ['user_id', 'country']
)

# 费用
total_cost_usd = Gauge(
    'llm_total_cost_usd',
    'Total estimated cost in USD'
)

# 全局统计 (持久化)
total_requests_global = Gauge(
    'llm_total_requests_global',
    'Total requests all time (persistent)'
)

total_tokens_global = Gauge(
    'llm_total_tokens_global',
    'Total tokens all time (persistent)'
)

total_prompt_tokens_global = Gauge(
    'llm_total_prompt_tokens_global',
    'Total prompt tokens all time (persistent)'
)

total_completion_tokens_global = Gauge(
    'llm_total_completion_tokens_global',
    'Total completion tokens all time (persistent)'
)

total_cached_tokens_global = Gauge(
    'llm_total_cached_tokens_global',
    'Total cached tokens all time (persistent)'
)

# ============ 状态存储 ============

class MetricsState:
    """存储增量扫描状态和用户会话信息"""
    
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self.processed_files: set = set()
        self.processed_file_mtimes: dict = {}
        self.user_activity: dict = {}  # user_id -> {"first_seen", "last_seen", "country", "requests"}
        self.recent_activity: dict = {}  # user_id -> last_activity_timestamp
        self.preset_phrases: set = set()
        self.lock = Lock()
        
        self._load_state()
        self._init_db()
        self._load_presets()

    def _state_file(self) -> Path:
        return self.data_dir / "exporter_state.json"
    
    def _db_file(self) -> Path:
        return self.data_dir / "conversations.db"
    
    def _load_state(self):
        """加载已处理文件列表"""
        state_file = self._state_file()
        if not state_file.exists():
            return
        try:
            with open(state_file, 'r') as f:
                data = json.load(f)
            self.processed_files = set(data.get("processed_files", []))
            self.processed_file_mtimes = data.get("processed_file_mtimes", {})
            self.user_activity = data.get("user_activity", {})
            logger.info(f"Loaded state: {len(self.processed_files)} processed files")
        except Exception as e:
            logger.warning(f"Failed to load state: {e}")
    
    def _save_state(self):
        """保存状态到文件"""
        try:
            with open(self._state_file(), 'w') as f:
                json.dump({
                    "processed_files": list(self.processed_files),
                    "processed_file_mtimes": self.processed_file_mtimes,
                    "user_activity": self.user_activity,
                }, f)
        except Exception as e:
            logger.warning(f"Failed to save state: {e}")
    
    @staticmethod
    def _add_column_if_missing(cursor, table: str, column: str, definition: str):
        try:
            cursor.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        except sqlite3.OperationalError:
            pass
    
    def _init_db(self):
        """初始化SQLite数据库"""
        conn = sqlite3.connect(self._db_file())
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                user_id TEXT,
                country TEXT,
                user_query TEXT,
                ai_response TEXT,
                ai_action TEXT,
                duration_ms REAL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                cached_tokens INTEGER,
                file_path TEXT UNIQUE,
                is_preset INTEGER DEFAULT 0,
                message_type TEXT DEFAULT "chat",
                session_id TEXT,
                client_version TEXT,
                session_duration_sec REAL
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_sessions (
                user_id TEXT PRIMARY KEY,
                first_seen TEXT,
                last_seen TEXT,
                total_requests INTEGER DEFAULT 0,
                total_play_seconds INTEGER DEFAULT 0,
                country TEXT,
                is_developer INTEGER DEFAULT 0,
                nickname TEXT,
                session_count INTEGER DEFAULT 0,
                player_name TEXT,
                is_blacklisted INTEGER DEFAULT 0,
                money INTEGER DEFAULT 0,
                island_level INTEGER DEFAULT 1,
                tasks_completed INTEGER DEFAULT 0,
                tasks_total INTEGER DEFAULT 0,
                current_task_title TEXT,
                current_task_status TEXT,
                achievements_unlocked INTEGER DEFAULT 0,
                achievements_total INTEGER DEFAULT 0,
                is_fake_user INTEGER DEFAULT 0
            )
        ''')
        
        user_columns = [
            ("is_developer", "INTEGER DEFAULT 0"),
            ("nickname", "TEXT"),
            ("session_count", "INTEGER DEFAULT 0"),
            ("player_name", "TEXT"),
            ("is_blacklisted", "INTEGER DEFAULT 0"),
            ("money", "INTEGER DEFAULT 0"),
            ("island_level", "INTEGER DEFAULT 1"),
            ("tasks_completed", "INTEGER DEFAULT 0"),
            ("tasks_total", "INTEGER DEFAULT 0"),
            ("current_task_title", "TEXT"),
            ("current_task_status", "TEXT"),
            ("achievements_unlocked", "INTEGER DEFAULT 0"),
            ("achievements_total", "INTEGER DEFAULT 0"),
            ("is_fake_user", "INTEGER DEFAULT 0"),
        ]
        for column, definition in user_columns:
            self._add_column_if_missing(cursor, "user_sessions", column, definition)
        
        conversation_columns = [
            ("is_preset", "INTEGER DEFAULT 0"),
            ("message_type", 'TEXT DEFAULT "chat"'),
            ("session_id", "TEXT"),
            ("client_version", "TEXT"),
            ("session_duration_sec", "REAL"),
        ]
        for column, definition in conversation_columns:
            self._add_column_if_missing(cursor, "conversations", column, definition)
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS preset_phrases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phrase TEXT UNIQUE
            )
        ''')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_conv_time ON conversations(timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_conv_country ON conversations(country)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id)')
        
        conn.commit()
        conn.close()
        logger.info("SQLite database initialized")
    def mark_processed(self, filepath: str, mtime_ns: int | None = None):
        """标记文件为已处理"""
        with self.lock:
            self.processed_files.add(filepath)
            if mtime_ns is not None:
                self.processed_file_mtimes[filepath] = int(mtime_ns)
    
    def is_processed(self, filepath: str, mtime_ns: int | None = None) -> bool:
        """检查文件是否已处理"""
        if filepath not in self.processed_files:
            return False
        if mtime_ns is None:
            return True
        stored_mtime = self.processed_file_mtimes.get(filepath)
        if stored_mtime is None:
            # 兼容旧状态文件：对于需要mtime追踪的可变文件，允许重跑一次建立基线
            return False
        return int(stored_mtime) == int(mtime_ns)
    
    def update_user_activity(self, user_id: str, timestamp: str, country: str):
        """更新用户活动状态"""
        with self.lock:
            now_ts = time.time()
            self.recent_activity[user_id] = now_ts
            
            if user_id not in self.user_activity:
                self.user_activity[user_id] = {
                    "first_seen": timestamp,
                    "last_seen": timestamp,
                    "country": country,
                    "requests": 0
                }
            
            self.user_activity[user_id]["last_seen"] = timestamp
            self.user_activity[user_id]["requests"] += 1
            if country:
                self.user_activity[user_id]["country"] = country

    def _load_presets(self):
        """加载预设对话到内存"""
        try:
            conn = sqlite3.connect(self._db_file())
            c = conn.cursor()
            c.execute("SELECT phrase FROM preset_phrases")
            self.preset_phrases = {row[0] for row in c.fetchall()}
            conn.close()
        except Exception as e:
            logger.warning(f"Failed to load presets: {e}")

    def add_preset(self, phrase: str):
        """添加预设对话"""
        if not phrase: return
        try:
            conn = sqlite3.connect(self._db_file())
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO preset_phrases (phrase) VALUES (?)", (phrase,))
            conn.commit()
            conn.close()
            with self.lock:
                self.preset_phrases.add(phrase)
        except Exception as e:
            logger.error(f"Failed to add preset: {e}")

    def remove_preset(self, phrase: str):
        """移除预设对话"""
        try:
            conn = sqlite3.connect(self._db_file())
            c = conn.cursor()
            c.execute("DELETE FROM preset_phrases WHERE phrase = ?", (phrase,))
            conn.commit()
            conn.close()
            with self.lock:
                if phrase in self.preset_phrases:
                    self.preset_phrases.remove(phrase)
        except Exception as e:
            logger.error(f"Failed to remove preset: {e}")
    
    def get_presets(self) -> list:
        return list(self.preset_phrases)
    

    
    def get_active_users_by_country(self) -> dict:
        """获取按国家分组的活跃用户数"""
        cutoff = time.time() - CCU_WINDOW
        active_by_country = defaultdict(set)
        
        with self.lock:
            for user_id, last_ts in self.recent_activity.items():
                if last_ts >= cutoff:
                    country = self.user_activity.get(user_id, {}).get("country", "unknown")
                    active_by_country[country].add(user_id)
        
        return {country: len(users) for country, users in active_by_country.items()}
    
    def save_conversation(self, record: dict):
        """保存对话记录到SQLite"""
        try:
            conn = sqlite3.connect(self._db_file())
            cursor = conn.cursor()

            file_path = record.get("file_path")
            user_id = record.get("user_id") or "anonymous"
            timestamp = record.get("timestamp") or datetime.now().isoformat()
            user_query = record.get("user_query", "")
            is_preset = 1 if user_query in self.preset_phrases else 0
            existing = cursor.execute(
                "SELECT 1 FROM conversations WHERE file_path = ?",
                (file_path,),
            ).fetchone()
            request_increment = 0 if existing else 1

            cursor.execute('''
                INSERT INTO conversations
                (timestamp, user_id, country, user_query, ai_response, ai_action, 
                 duration_ms, prompt_tokens, completion_tokens, cached_tokens, file_path, 
                 message_type, session_id, client_version, session_duration_sec, is_preset)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    timestamp = excluded.timestamp,
                    user_id = excluded.user_id,
                    country = excluded.country,
                    user_query = excluded.user_query,
                    ai_response = excluded.ai_response,
                    ai_action = excluded.ai_action,
                    duration_ms = excluded.duration_ms,
                    prompt_tokens = excluded.prompt_tokens,
                    completion_tokens = excluded.completion_tokens,
                    cached_tokens = excluded.cached_tokens,
                    message_type = excluded.message_type,
                    session_id = excluded.session_id,
                    client_version = excluded.client_version,
                    session_duration_sec = excluded.session_duration_sec,
                    is_preset = excluded.is_preset
            ''', (
                timestamp,
                user_id,
                record.get("country"),
                user_query,
                record.get("ai_response"),
                record.get("ai_action"),
                record.get("duration_ms"),
                record.get("prompt_tokens", 0),
                record.get("completion_tokens", 0),
                record.get("cached_tokens", 0),
                record.get("file_path"),
                record.get("message_type", "chat"),
                record.get("session_id"),
                record.get("client_version"),
                record.get("session_duration_sec"),
                is_preset,
            ))

            cursor.execute('''
                INSERT INTO user_sessions (user_id, first_seen, last_seen, total_requests, country)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    first_seen = CASE
                        WHEN excluded.first_seen < first_seen THEN excluded.first_seen
                        ELSE first_seen
                    END,
                    last_seen = CASE
                        WHEN excluded.last_seen > last_seen THEN excluded.last_seen
                        ELSE last_seen
                    END,
                    total_requests = total_requests + excluded.total_requests,
                    country = COALESCE(excluded.country, country)
            ''', (
                user_id,
                timestamp,
                timestamp,
                request_increment,
                record.get("country"),
            ))

            player_name = record.get("player_name")
            if player_name:
                cursor.execute('''
                    UPDATE user_sessions
                    SET player_name = ?
                    WHERE user_id = ? AND (last_seen = ? OR player_name IS NULL)
                ''', (player_name, user_id, timestamp))

            total_money = record.get("total_money")
            if total_money is not None:
                cursor.execute('''
                    UPDATE user_sessions
                    SET money = ?,
                        island_level = COALESCE(?, island_level),
                        tasks_completed = COALESCE(?, tasks_completed),
                        tasks_total = COALESCE(?, tasks_total),
                        current_task_title = COALESCE(?, current_task_title),
                        current_task_status = COALESCE(?, current_task_status),
                        achievements_unlocked = COALESCE(?, achievements_unlocked),
                        achievements_total = COALESCE(?, achievements_total)
                    WHERE user_id = ?
                ''', (
                    total_money,
                    record.get("island_level"),
                    record.get("tasks_completed"),
                    record.get("tasks_total"),
                    record.get("current_task_title"),
                    record.get("current_task_status"),
                    record.get("achievements_unlocked"),
                    record.get("achievements_total"),
                    user_id,
                ))

            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Failed to save conversation: {e}")

    def recalculate_user_sessions(self):
        """
        计算用户的游玩时长和会话数（混合策略）:
        1. 有 logoff + session_duration_sec 的 session：使用精确时长
        2. 有 session_id 但无 logoff 的 session：使用首尾时间差
        3. 无 session_id 的历史数据：使用 5 分钟间隔规则
        """
        try:
            conn = sqlite3.connect(self._db_file())
            cursor = conn.cursor()

            cursor.execute('SELECT DISTINCT user_id FROM conversations WHERE user_id IS NOT NULL AND user_id != ""')
            all_users = [row[0] for row in cursor.fetchall()]
            user_stats = {}

            for user_id in all_users:
                total_play_seconds = 0
                total_sessions = 0

                cursor.execute('''
                    SELECT session_id, session_duration_sec
                    FROM conversations
                    WHERE user_id = ?
                      AND message_type = 'logoff'
                      AND session_duration_sec IS NOT NULL
                      AND session_id IS NOT NULL
                ''', (user_id,))
                logoff_sessions = {}
                for session_id, duration in cursor.fetchall():
                    logoff_sessions[session_id] = duration
                    total_play_seconds += int(duration or 0)
                    total_sessions += 1

                cursor.execute('''
                    SELECT session_id, MIN(timestamp), MAX(timestamp)
                    FROM conversations
                    WHERE user_id = ? AND session_id IS NOT NULL
                    GROUP BY session_id
                ''', (user_id,))
                for session_id, start_ts, end_ts in cursor.fetchall():
                    if session_id in logoff_sessions or not start_ts or not end_ts:
                        continue
                    cursor.execute(
                        'SELECT (julianday(?) - julianday(?)) * 86400',
                        (end_ts, start_ts),
                    )
                    diff = cursor.fetchone()[0] or 0
                    total_play_seconds += int(diff)
                    total_sessions += 1

                cursor.execute('''
                    SELECT timestamp
                    FROM conversations
                    WHERE user_id = ? AND session_id IS NULL
                    ORDER BY timestamp
                ''', (user_id,))
                no_session_records = [row[0] for row in cursor.fetchall()]
                if no_session_records:
                    prev_ts = None
                    session_play = 0
                    sessions_from_fallback = 0
                    for ts in no_session_records:
                        if prev_ts is None:
                            sessions_from_fallback = 1
                        else:
                            cursor.execute(
                                'SELECT (julianday(?) - julianday(?)) * 86400',
                                (ts, prev_ts),
                            )
                            diff = cursor.fetchone()[0] or 0
                            if diff <= 300:
                                session_play += diff
                            else:
                                sessions_from_fallback += 1
                        prev_ts = ts

                    total_play_seconds += int(session_play)
                    total_sessions += sessions_from_fallback

                user_stats[user_id] = (total_sessions, total_play_seconds)

            for user_id, (session_count, play_seconds) in user_stats.items():
                cursor.execute('''
                    UPDATE user_sessions
                    SET session_count = ?, total_play_seconds = ?
                    WHERE user_id = ?
                ''', (session_count, play_seconds, user_id))

            cursor.execute('''
                UPDATE user_sessions
                SET is_fake_user = CASE WHEN total_requests < 2 THEN 1 ELSE 0 END
            ''')

            conn.commit()
            conn.close()
            logger.debug(f"Recalculated sessions for {len(user_stats)} users")
        except Exception as e:
            logger.warning(f"Failed to recalculate user sessions: {e}")

    def update_user_gauges(self):
        """更新用户统计Gauge"""
        try:
            conn = sqlite3.connect(self._db_file())
            cursor = conn.cursor()

            cursor.execute('''
                SELECT user_id, country, total_requests, total_play_seconds, session_count
                FROM user_sessions
                WHERE is_developer = 0
            ''')

            for user_id, country, requests, play_seconds, session_count in cursor.fetchall():
                country = country or "unknown"
                user_total_requests.labels(user_id=user_id, country=country).set(requests or 0)
                user_play_seconds.labels(user_id=user_id, country=country).set(play_seconds or 0)
                user_session_count.labels(user_id=user_id, country=country).set(session_count or 0)

            cursor.execute('''
                SELECT SUM(prompt_tokens), SUM(completion_tokens), SUM(cached_tokens)
                FROM conversations
            ''')
            row = cursor.fetchone()
            if row:
                prompt_tokens, completion_tokens, cached_tokens = row
                prompt_tokens = prompt_tokens or 0
                completion_tokens = completion_tokens or 0
                cached_tokens = cached_tokens or 0

                real_prompt = max(0, prompt_tokens - cached_tokens)
                cost = (
                    (real_prompt * PRICE_PROMPT / 1_000_000) +
                    (completion_tokens * PRICE_COMPLETION / 1_000_000) +
                    (cached_tokens * PRICE_CACHED / 1_000_000)
                )
                total_cost_usd.set(cost)
                total_tokens_global.set(prompt_tokens + completion_tokens)
                total_prompt_tokens_global.set(prompt_tokens)
                total_completion_tokens_global.set(completion_tokens)
                total_cached_tokens_global.set(cached_tokens)

                cursor.execute('SELECT COUNT(*) FROM conversations')
                total_requests_global.set(cursor.fetchone()[0])

            conn.close()
        except Exception as e:
            logger.warning(f"Failed to update user gauges: {e}")

def _parse_int(value, default=0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _select_session_events(session_record: dict) -> dict:
    """从 session 聚合记录中挑选 login/logoff 事件（同类型按 source 优先级择优）"""
    selected = {}
    sources = session_record.get("sources", {})
    if not isinstance(sources, dict):
        return selected

    def source_priority(source_name: str) -> int:
        if source_name == "launcher":
            return 2
        if source_name == "python":
            return 1
        return 0

    for source_name, source_data in sources.items():
        if not isinstance(source_data, dict):
            continue
        priority = source_priority(source_name)
        for event_type in ("login", "logoff"):
            event_data = source_data.get(event_type)
            if not isinstance(event_data, dict):
                continue

            current = selected.get(event_type)
            if current is None:
                selected[event_type] = {
                    "source": source_name,
                    "priority": priority,
                    "event": event_data,
                }
                continue

            current_payload = current["event"].get("payload", {})
            current_payload_len = len(current_payload) if isinstance(current_payload, dict) else 0
            incoming_payload = event_data.get("payload", {})
            incoming_payload_len = len(incoming_payload) if isinstance(incoming_payload, dict) else 0

            if priority > current["priority"] or (
                priority == current["priority"] and incoming_payload_len > current_payload_len
            ):
                selected[event_type] = {
                    "source": source_name,
                    "priority": priority,
                    "event": event_data,
                }
    return selected


def _process_session_file(session_record: dict, filepath: Path, state: MetricsState) -> None:
    """处理 session-*.jsonl 聚合文件，将 login/logoff 展开为 conversations 记录"""
    if not isinstance(session_record, dict):
        return

    selected_events = _select_session_events(session_record)
    if not selected_events:
        return

    session_id_global = session_record.get("session_id")
    user_id_global = session_record.get("user_id")
    created_at = session_record.get("created_at")
    updated_at = session_record.get("updated_at")

    for event_type in ("login", "logoff"):
        event_wrap = selected_events.get(event_type)
        if not event_wrap:
            continue

        event_data = event_wrap.get("event", {})
        if not isinstance(event_data, dict):
            continue

        headers = event_data.get("headers", {})
        if not isinstance(headers, dict):
            headers = {}
        payload = event_data.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        timestamp = (
            event_data.get("received_at")
            or event_data.get("event_timestamp")
            or (created_at if event_type == "login" else updated_at)
            or updated_at
            or created_at
            or datetime.now().isoformat()
        )

        user_id = (
            user_id_global
            or headers.get("x-user-id")
            or headers.get("X-User-ID")
            or "anonymous"
        )
        country = headers.get("cf-ipcountry") or "unknown"
        session_id = (
            session_id_global
            or payload.get("session_id")
            or headers.get("x-session-id")
            or headers.get("X-Session-ID")
        )
        client_version = (
            headers.get("x-client-version")
            or headers.get("X-Client-Version")
            or payload.get("client_version")
        )

        username = payload.get("username") or "anonymous"
        user_query = f"[{event_type.upper()}] {username}"

        # 登录/退出属于系统事件，不计入tokens和延迟统计
        model = "system"
        status_str = "200"
        content_length_in = _parse_int(headers.get("content-length"), 0)
        content_length_out = 0

        requests_total.labels(
            user_id=user_id, country=country, model=model, status=status_str
        ).inc()
        bandwidth_bytes.labels(user_id=user_id, direction="in").inc(content_length_in)
        bandwidth_bytes.labels(user_id=user_id, direction="out").inc(content_length_out)
        state.update_user_activity(user_id, timestamp, country)

        state.save_conversation({
            "timestamp": timestamp,
            "user_id": user_id,
            "country": country,
            "user_query": user_query,
            "ai_response": "",
            "ai_action": event_type,
            "duration_ms": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "file_path": f"{filepath}#{event_type}",
            "player_name": username if username != "anonymous" else "",
            "message_type": event_type,
            "session_id": session_id,
            "client_version": client_version,
            "session_duration_sec": payload.get("session_duration_sec") if event_type == "logoff" else None,
            "total_money": payload.get("total_money") if event_type == "logoff" else None,
            "island_level": payload.get("island_level") if event_type == "logoff" else None,
            "tasks_completed": payload.get("tasks_completed") if event_type == "logoff" else None,
            "tasks_total": payload.get("tasks_total") if event_type == "logoff" else None,
            "current_task_title": payload.get("current_task_title") if event_type == "logoff" else None,
            "current_task_status": payload.get("current_task_status") if event_type == "logoff" else None,
            "achievements_unlocked": payload.get("achievements_unlocked") if event_type == "logoff" else None,
            "achievements_total": payload.get("achievements_total") if event_type == "logoff" else None,
        })

        logger.debug(
            "Processed session event: %s | user=%s | session=%s | source=%s",
            event_type,
            user_id,
            session_id,
            event_wrap.get("source", "unknown"),
        )


def extract_user_query(body: dict) -> str:
    """从请求body中提取<user_query>内容"""
    try:
        messages = body.get("messages", [])
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            match = re.search(r'<user_query>(.*?)</user_query>', content, re.DOTALL)
            if match:
                return match.group(1).strip()
        return ""
    except Exception:
        return ""


def extract_player_name(body: dict) -> str:
    """从system prompt中提取玩家名"""
    try:
        messages = body.get("messages", [])
        for msg in messages:
            if msg.get("role") != "system":
                continue
            content = msg.get("content", "")
            match = re.search(r'-\s*\*\*Master\*\*:\s*(.+?)(?:\n|$)', content)
            if match:
                name = match.group(1).strip()
                if name and name != "主人":
                    return name
        return ""
    except Exception:
        return ""


def extract_ai_response(body: dict) -> tuple:
    """从响应body中提取AI回复和action"""
    try:
        choices = body.get("choices", [])
        if not choices:
            return "", ""

        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls", [])
        if tool_calls:
            action = tool_calls[0].get("function", {}).get("name", "")
            args = tool_calls[0].get("function", {}).get("arguments", "{}")
            try:
                args_dict = json.loads(args)
                talk = args_dict.get("talk", "")
                self_talk = args_dict.get("self_talk", [])
                if isinstance(self_talk, list):
                    self_talk = " | ".join(self_talk)
                response = talk or self_talk
            except Exception:
                response = args
            return response[:500], action

        content = message.get("content", "")
        return content[:500], ""
    except Exception:
        return "", ""


def parse_jsonl_file(filepath: Path, state: MetricsState):
    """解析单个JSONL文件"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f if line.strip()]

        if not lines:
            return

        # session 聚合日志（单行）
        line1_data = json.loads(lines[0])
        if line1_data.get("type") == "session":
            _process_session_file(line1_data, filepath, state)
            return

        if len(lines) < 2:
            return

        # 解析两行 request/response 数据
        line2_data = json.loads(lines[1])
        
        # 根据 type 字段正确识别 request 和 response（不依赖行顺序）
        if line1_data.get("type") == "request" and line2_data.get("type") == "response":
            request_data = line1_data
            response_data = line2_data
        elif line1_data.get("type") == "response" and line2_data.get("type") == "request":
            # 顺序颠倒的情况
            request_data = line2_data
            response_data = line1_data
        else:
            # 无法识别的格式
            return
        
        # 提取基本信息
        user_id = request_data.get("user_id", "anonymous")
        timestamp = request_data.get("timestamp", "")
        headers = request_data.get("headers", {})
        path = request_data.get("path", "")
        
        country = headers.get("cf-ipcountry", "unknown")
        
        # 提取 session_id 和 client_version (从 header 或 body)
        session_id = headers.get("x-session-id") or headers.get("X-Session-ID")
        client_version = headers.get("x-client-version") or headers.get("X-Client-Version")
        
        body = request_data.get("body") or {}
        if not isinstance(body, dict):
            body = {}
        
        # 从 body 补充 session_id 和 client_version（如果 header 没有）
        if not session_id:
            session_id = body.get("session_id")
        if not client_version:
            client_version = body.get("client_version")
        
        # 检测消息类型: login / logoff / chat
        if path == "/login" or path == "/v1/login":
            message_type = "login"
        elif path == "/logoff" or path == "/v1/logoff":
            message_type = "logoff"
        else:
            message_type = "chat"
        
        duration_ms = response_data.get("duration_ms", 0)
        status_code = response_data.get("status_code", 0)
        usage = response_data.get("usage") or {}
        response_body = response_data.get("body") or {}
        
        # 系统消息 (login/logoff) 特殊处理
        session_duration_sec = None
        if message_type in ("login", "logoff"):
            username = body.get("username") or "anonymous"
            user_query = f"[{message_type.upper()}] {username}"
            ai_response = ""
            ai_action = message_type
            player_name = username if username != "anonymous" else ""
            prompt_tokens = 0
            completion_tokens = 0
            cached_tokens = 0
            model = "system"
            content_length_in = int(headers.get("content-length", 0))
            content_length_out = len(json.dumps(response_body))
            
            # logoff 特有: session_duration_sec, total_money, 游戏进度
            total_money = None
            island_level = None
            tasks_completed = None
            tasks_total = None
            current_task_title = None
            current_task_status = None
            achievements_unlocked = None
            achievements_total = None
            
            if message_type == "logoff":
                session_duration_sec = body.get("session_duration_sec")
                total_money = body.get("total_money")
                island_level = body.get("island_level")
                tasks_completed = body.get("tasks_completed")
                tasks_total = body.get("tasks_total")
                current_task_title = body.get("current_task_title")
                current_task_status = body.get("current_task_status")
                achievements_unlocked = body.get("achievements_unlocked")
                achievements_total = body.get("achievements_total")
        else:
            # 正常对话处理
            model = body.get("model", "unknown")
            content_length_in = int(headers.get("content-length", 0))
            
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            cached_tokens = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
            
            # 估算响应大小
            content_length_out = len(json.dumps(response_body))
            
            # 提取对话内容
            user_query = extract_user_query(body)
            ai_response, ai_action = extract_ai_response(response_body)
            player_name = extract_player_name(body)
        
        # 更新Prometheus指标 (只对 chat 消息统计 tokens)
        status_str = str(status_code)
        requests_total.labels(
            user_id=user_id, country=country, model=model, status=status_str
        ).inc()
        
        if message_type == "chat":
            request_duration.labels(
                user_id=user_id, country=country, model=model
            ).observe(duration_ms / 1000)  # 转换为秒
            
            prompt_tokens_total.labels(user_id=user_id, model=model).inc(prompt_tokens)
            completion_tokens_total.labels(user_id=user_id, model=model).inc(completion_tokens)
            cached_tokens_total.labels(user_id=user_id, model=model).inc(cached_tokens)
        
        bandwidth_bytes.labels(user_id=user_id, direction="in").inc(content_length_in)
        bandwidth_bytes.labels(user_id=user_id, direction="out").inc(content_length_out)
        
        # 更新状态
        state.update_user_activity(user_id, timestamp, country)
        
        # 增加总费用 (只对 chat 消息)
        if message_type == "chat":
            real_prompt_tokens = max(0, prompt_tokens - cached_tokens)
            request_cost = (
                (real_prompt_tokens * PRICE_PROMPT / 1_000_000) +
                (completion_tokens * PRICE_COMPLETION / 1_000_000) +
                (cached_tokens * PRICE_CACHED / 1_000_000)
            )
            total_cost_usd.inc(request_cost)
            
            # 增加全局统计
            total_requests_global.inc()
            total_tokens_global.inc(prompt_tokens + completion_tokens)
            total_prompt_tokens_global.inc(prompt_tokens)
            total_completion_tokens_global.inc(completion_tokens)
            total_cached_tokens_global.inc(cached_tokens)
        
        # 保存对话到SQLite
        state.save_conversation({
            "timestamp": timestamp,
            "user_id": user_id,
            "country": country,
            "user_query": user_query,
            "ai_response": ai_response,
            "ai_action": ai_action,
            "duration_ms": duration_ms,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": cached_tokens,
            "file_path": str(filepath),
            "player_name": player_name,
            "message_type": message_type,
            "session_id": session_id,
            "client_version": client_version,
            "session_duration_sec": session_duration_sec,
            "total_money": total_money if message_type == "logoff" else None,
            "island_level": island_level if message_type == "logoff" else None,
            "tasks_completed": tasks_completed if message_type == "logoff" else None,
            "tasks_total": tasks_total if message_type == "logoff" else None,
            "current_task_title": current_task_title if message_type == "logoff" else None,
            "current_task_status": current_task_status if message_type == "logoff" else None,
            "achievements_unlocked": achievements_unlocked if message_type == "logoff" else None,
            "achievements_total": achievements_total if message_type == "logoff" else None
        })
        
        logger.debug(f"Processed: {filepath.name} | user={user_id} | type={message_type} | session={session_id}")
        
    except Exception as e:
        logger.warning(f"Failed to parse {filepath}: {e}")


def scan_output_directory(state: MetricsState):
    """扫描output目录下的所有JSONL文件"""
    if not OUTPUT_DIR.exists():
        logger.warning(f"Output directory does not exist: {OUTPUT_DIR}")
        return
    
    new_files = 0
    
    for user_dir in OUTPUT_DIR.iterdir():
        if not user_dir.is_dir():
            continue
        
        for jsonl_file in user_dir.glob("*.jsonl"):
            filepath_str = str(jsonl_file)

            is_session_file = jsonl_file.name.startswith("session-")
            mtime_ns = None
            if is_session_file:
                mtime_ns = jsonl_file.stat().st_mtime_ns

            if state.is_processed(filepath_str, mtime_ns):
                continue
            
            parse_jsonl_file(jsonl_file, state)
            state.mark_processed(filepath_str, mtime_ns)
            new_files += 1
    
    if new_files > 0:
        logger.info(f"Processed {new_files} new files")
        state._save_state()
    
    # 更新CCU指标
    active_by_country = state.get_active_users_by_country()
    total_ccu = 0
    for country, count in active_by_country.items():
        active_users.labels(country=country).set(count)
        total_ccu += count
    total_active_users.set(total_ccu)
    
    first_run = not hasattr(scan_output_directory, "initialized")
    if first_run:
         scan_output_directory.initialized = True
         # 初始化时从数据库加载总费用 (已在update_user_gauges中处理)
         state.update_user_gauges()
    
    # 重新计算用户会话统计
    state.recalculate_user_sessions()
    
    # 更新用户统计
    state.update_user_gauges()


def scanner_loop(state: MetricsState):
    """后台扫描循环"""
    while True:
        try:
            scan_output_directory(state)
        except Exception as e:
            logger.error(f"Scan error: {e}")
        time.sleep(SCAN_INTERVAL)


def main():
    """主入口"""
    logger.info(f"Starting LLM Router Metrics Exporter")
    logger.info(f"Output directory: {OUTPUT_DIR}")
    logger.info(f"Data directory: {DATA_DIR}")
    logger.info(f"Metrics port: {METRICS_PORT}")
    logger.info(f"Scan interval: {SCAN_INTERVAL}s")
    
    # 初始化状态
    state = MetricsState(DATA_DIR)
    
    # 首次扫描
    logger.info("Performing initial scan...")
    scan_output_directory(state)
    
    # 启动后台扫描线程
    scanner_thread = Thread(target=scanner_loop, args=(state,), daemon=True)
    scanner_thread.start()
    
    # 启动Prometheus HTTP服务
    logger.info(f"Starting metrics server on port {METRICS_PORT}")
    start_http_server(METRICS_PORT)
    
    # 保持主线程运行
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        state._save_state()


if __name__ == "__main__":
    main()
