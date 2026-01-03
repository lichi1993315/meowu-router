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
        self.user_activity: dict = {}  # user_id -> {"first_seen", "last_seen", "country", "requests"}
        self.recent_activity: dict = {}  # user_id -> last_activity_timestamp
        self.preset_phrases: set = set() # loaded from db
        self.lock = Lock()
        
        # 今日token统计
        self.today_prompt_tokens = 0
        self.today_completion_tokens = 0
        self.today_cached_tokens = 0
        self.today_date = datetime.now().date()
        
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
        if state_file.exists():
            try:
                with open(state_file, 'r') as f:
                    data = json.load(f)
                    self.processed_files = set(data.get("processed_files", []))
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
                    "user_activity": self.user_activity,
                }, f)
        except Exception as e:
            logger.warning(f"Failed to save state: {e}")
    
    def _init_db(self):
        """初始化SQLite数据库"""
        conn = sqlite3.connect(self._db_file())
        cursor = conn.cursor()
        
        # 对话记录表
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
                file_path TEXT UNIQUE
            )
        ''')
        
        # 用户会话表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_sessions (
                user_id TEXT PRIMARY KEY,
                first_seen TEXT,
                last_seen TEXT,
                total_requests INTEGER DEFAULT 0,
                total_play_seconds INTEGER DEFAULT 0,
                country TEXT,
                is_developer INTEGER DEFAULT 0
            )
        ''')
        
        # 添加is_developer列（如果不存在）
        try:
            cursor.execute('ALTER TABLE user_sessions ADD COLUMN is_developer INTEGER DEFAULT 0')
        except:
            pass  # 列已存在
        
        # 添加session_count列（如果不存在）
        try:
            cursor.execute('ALTER TABLE user_sessions ADD COLUMN session_count INTEGER DEFAULT 0')
        except:
            pass  # 列已存在
        
        # 添加player_name列（如果不存在）- 从system prompt提取的玩家名
        try:
            cursor.execute('ALTER TABLE user_sessions ADD COLUMN player_name TEXT')
        except:
            pass  # 列已存在
        
        # 添加nickname列（如果不存在）- 手动设置的昵称
        try:
            cursor.execute('ALTER TABLE user_sessions ADD COLUMN nickname TEXT')
        except:
            pass  # 列已存在

        # 添加is_blacklisted列（如果不存在）
        try:
            cursor.execute('ALTER TABLE user_sessions ADD COLUMN is_blacklisted INTEGER DEFAULT 0')
        except:
            pass  # 列已存在
        
        # 添加money列（如果不存在）- 玩家金币数
        try:
            cursor.execute('ALTER TABLE user_sessions ADD COLUMN money INTEGER DEFAULT 0')
        except:
            pass  # 列已存在
        
        # 添加游戏进度相关列
        game_progress_columns = [
            ('island_level', 'INTEGER DEFAULT 1'),
            ('tasks_completed', 'INTEGER DEFAULT 0'),
            ('tasks_total', 'INTEGER DEFAULT 0'),
            ('current_task_title', 'TEXT'),
            ('current_task_status', 'TEXT'),
            ('achievements_unlocked', 'INTEGER DEFAULT 0'),
            ('achievements_total', 'INTEGER DEFAULT 0'),
        ]
        for col_name, col_type in game_progress_columns:
            try:
                cursor.execute(f'ALTER TABLE user_sessions ADD COLUMN {col_name} {col_type}')
            except:
                pass  # 列已存在
            
        # 预设对话表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS preset_phrases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phrase TEXT UNIQUE
            )
        ''')
        
        # 添加is_preset列到conversations (如果不存在)
        try:
            cursor.execute('ALTER TABLE conversations ADD COLUMN is_preset INTEGER DEFAULT 0')
        except:
            pass
        
        # 添加message_type列到conversations (如果不存在) - 区分 login/chat/logoff
        try:
            cursor.execute('ALTER TABLE conversations ADD COLUMN message_type TEXT DEFAULT "chat"')
        except:
            pass  # 列已存在
        
        # 添加session_id列 - 精确的 session 追踪
        try:
            cursor.execute('ALTER TABLE conversations ADD COLUMN session_id TEXT')
        except:
            pass
        
        # 添加client_version列 - 客户端版本
        try:
            cursor.execute('ALTER TABLE conversations ADD COLUMN client_version TEXT')
        except:
            pass
        
        # 添加session_duration_sec列 - logoff 时的精确 session 时长
        try:
            cursor.execute('ALTER TABLE conversations ADD COLUMN session_duration_sec REAL')
        except:
            pass
        
        # 创建索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_conv_time ON conversations(timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_conv_country ON conversations(country)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id)')
        
        conn.commit()
        conn.close()
        logger.info("SQLite database initialized")
    
    def mark_processed(self, filepath: str):
        """标记文件为已处理"""
        with self.lock:
            self.processed_files.add(filepath)
    
    def is_processed(self, filepath: str) -> bool:
        """检查文件是否已处理"""
        return filepath in self.processed_files
    
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
            
            cursor.execute('''
                INSERT OR IGNORE INTO conversations 
                (timestamp, user_id, country, user_query, ai_response, ai_action, 
                 duration_ms, prompt_tokens, completion_tokens, cached_tokens, file_path, 
                 message_type, session_id, client_version, session_duration_sec)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                record.get("timestamp"),
                record.get("user_id"),
                record.get("country"),
                record.get("user_query"),
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
                record.get("session_duration_sec")
            ))
            
            # 检查是否为预设对话并更新
            user_query = record.get("user_query", "")
            is_preset = 1 if user_query in self.preset_phrases else 0
            if is_preset:
                cursor.execute(
                    "UPDATE conversations SET is_preset = 1 WHERE file_path = ?", 
                    (record.get("file_path"),)
                )
            
            # 更新用户会话 - 只在新时间戳更新时才更新last_seen
            cursor.execute('''
                INSERT INTO user_sessions (user_id, first_seen, last_seen, total_requests, country)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    first_seen = CASE WHEN excluded.first_seen < first_seen THEN excluded.first_seen ELSE first_seen END,
                    last_seen = CASE WHEN excluded.last_seen > last_seen THEN excluded.last_seen ELSE last_seen END,
                    total_requests = total_requests + 1,
                    country = COALESCE(excluded.country, country)
            ''', (
                record.get("user_id"),
                record.get("timestamp"),
                record.get("timestamp"),
                record.get("country")
            ))
            
            # 更新player_name（如果提取到了，且timestamp更新）
            player_name = record.get("player_name")
            if player_name:
                cursor.execute('''
                    UPDATE user_sessions 
                    SET player_name = ?
                    WHERE user_id = ? AND (last_seen = ? OR player_name IS NULL)
                ''', (player_name, record.get("user_id"), record.get("timestamp")))
            
            # 更新money（如果是logoff且有total_money）
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
                    record.get("user_id")
                ))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Failed to save conversation: {e}")
    
    def recalculate_user_sessions(self):
        """
        计算用户的游玩时长和会话数（3级优先级）:
        1. 优先使用 logoff 的 session_duration_sec (最精确)
        2. 其次使用 session_id 分组计算
        3. 对历史数据使用 5 分钟间隔规则 (fallback)
        """
        try:
            conn = sqlite3.connect(self._db_file())
            cursor = conn.cursor()
            
            # 方法1: 使用 logoff 的 session_duration_sec (最精确)
            # 按 session_id 聚合 logoff 的时长
            cursor.execute('''
                SELECT 
                    user_id,
                    COUNT(DISTINCT session_id) as session_count,
                    CAST(SUM(session_duration_sec) AS INTEGER) as total_play_seconds
                FROM conversations
                WHERE message_type = 'logoff' 
                  AND session_duration_sec IS NOT NULL 
                  AND session_id IS NOT NULL
                  AND user_id IS NOT NULL AND user_id != ''
                GROUP BY user_id
            ''')
            
            logoff_users = {}
            for user_id, session_count, play_seconds in cursor.fetchall():
                logoff_users[user_id] = (session_count, play_seconds or 0)
            
            # 方法2: 使用 session_id 分组 (只计算有 session_id 但无 logoff 的用户)
            cursor.execute('''
                WITH users_with_logoff AS (
                    SELECT DISTINCT user_id FROM conversations 
                    WHERE message_type = 'logoff' AND session_duration_sec IS NOT NULL
                ),
                session_stats AS (
                    SELECT 
                        user_id,
                        session_id,
                        MIN(timestamp) as session_start,
                        MAX(timestamp) as session_end
                    FROM conversations
                    WHERE session_id IS NOT NULL 
                      AND user_id IS NOT NULL AND user_id != ''
                      AND user_id NOT IN (SELECT user_id FROM users_with_logoff)
                    GROUP BY user_id, session_id
                )
                SELECT 
                    user_id,
                    COUNT(DISTINCT session_id) as total_sessions,
                    CAST(SUM((julianday(session_end) - julianday(session_start)) * 86400) AS INTEGER) as total_play_seconds
                FROM session_stats
                GROUP BY user_id
            ''')
            
            session_id_users = {}
            for user_id, session_count, play_seconds in cursor.fetchall():
                session_id_users[user_id] = (session_count, play_seconds or 0)
            
            # 方法3: 使用 login 事件作为 session 边界 (无 session_id 的用户)
            cursor.execute('''
                WITH users_with_session_id AS (
                    SELECT DISTINCT user_id FROM conversations WHERE session_id IS NOT NULL
                ),
                session_boundaries AS (
                    SELECT 
                        user_id,
                        timestamp,
                        message_type,
                        SUM(CASE WHEN message_type = 'login' THEN 1 ELSE 0 END) 
                            OVER (PARTITION BY user_id ORDER BY timestamp) as inferred_session_id
                    FROM conversations
                    WHERE user_id IS NOT NULL AND user_id != ''
                      AND user_id NOT IN (SELECT user_id FROM users_with_session_id)
                ),
                session_stats AS (
                    SELECT 
                        user_id,
                        inferred_session_id,
                        MIN(timestamp) as session_start,
                        MAX(timestamp) as session_end
                    FROM session_boundaries
                    WHERE inferred_session_id > 0
                    GROUP BY user_id, inferred_session_id
                )
                SELECT 
                    user_id,
                    COUNT(DISTINCT inferred_session_id) as total_sessions,
                    CAST(SUM((julianday(session_end) - julianday(session_start)) * 86400) AS INTEGER) as total_play_seconds
                FROM session_stats
                GROUP BY user_id
            ''')
            
            login_users = {}
            for user_id, session_count, play_seconds in cursor.fetchall():
                login_users[user_id] = (session_count, play_seconds or 0)
            
            # 方法4: 5分钟间隔规则 (历史数据 fallback)
            cursor.execute('''
                WITH users_with_session AS (
                    SELECT DISTINCT user_id FROM conversations 
                    WHERE session_id IS NOT NULL OR message_type = 'login'
                ),
                ordered_requests AS (
                    SELECT 
                        c.user_id,
                        c.timestamp,
                        LAG(c.timestamp) OVER (PARTITION BY c.user_id ORDER BY c.timestamp) as prev_timestamp
                    FROM conversations c
                    WHERE c.user_id IS NOT NULL AND c.user_id != ''
                      AND c.user_id NOT IN (SELECT user_id FROM users_with_session)
                ),
                time_diffs AS (
                    SELECT 
                        user_id,
                        CASE 
                            WHEN prev_timestamp IS NULL THEN 0
                            ELSE (julianday(timestamp) - julianday(prev_timestamp)) * 86400
                        END as diff_seconds,
                        CASE 
                            WHEN prev_timestamp IS NULL THEN 1
                            WHEN (julianday(timestamp) - julianday(prev_timestamp)) * 86400 > 300 THEN 1
                            ELSE 0
                        END as is_new_session
                    FROM ordered_requests
                )
                SELECT 
                    user_id,
                    SUM(is_new_session) as session_count,
                    CAST(SUM(CASE WHEN diff_seconds <= 300 THEN diff_seconds ELSE 0 END) AS INTEGER) as actual_play_seconds
                FROM time_diffs
                GROUP BY user_id
            ''')
            
            fallback_users = {}
            for user_id, session_count, play_seconds in cursor.fetchall():
                fallback_users[user_id] = (session_count, play_seconds or 0)
            
            # 合并结果 (优先级: logoff > session_id > login > fallback)
            all_results = {**fallback_users, **login_users, **session_id_users, **logoff_users}
            
            for user_id, (session_count, play_seconds) in all_results.items():
                cursor.execute('''
                    UPDATE user_sessions 
                    SET session_count = ?, total_play_seconds = ?
                    WHERE user_id = ?
                ''', (session_count, play_seconds, user_id))
            
            conn.commit()
            conn.close()
            logger.debug(f"Recalculated sessions: {len(logoff_users)} logoff, {len(session_id_users)} session_id, {len(login_users)} login, {len(fallback_users)} fallback")
        except Exception as e:
            logger.warning(f"Failed to recalculate user sessions: {e}")
    
    def update_user_gauges(self):
        """更新用户统计Gauge"""
        try:
            conn = sqlite3.connect(self._db_file())
            cursor = conn.cursor()
            
            # 从数据库读取用户统计
            cursor.execute('''
                SELECT user_id, country, total_requests, total_play_seconds, session_count
                FROM user_sessions
                WHERE is_developer = 0
            ''')
            
            for row in cursor.fetchall():
                user_id, country, requests, play_seconds, session_count = row
                country = country or "unknown"
                
                user_total_requests.labels(user_id=user_id, country=country).set(requests or 0)
                user_play_seconds.labels(user_id=user_id, country=country).set(play_seconds or 0)
                user_session_count.labels(user_id=user_id, country=country).set(session_count or 0)
            
            # 计算总Token数并设置cost
            cursor.execute('''
                SELECT SUM(prompt_tokens), SUM(completion_tokens), SUM(cached_tokens)
                FROM conversations
            ''')
            row = cursor.fetchone()
            if row:
                p, c, cached = row
                p = p or 0
                c = c or 0
                cached = cached or 0
                
                # 计算费用 (修正逻辑: prompt_tokens 包含 cached_tokens)
                real_prompt = max(0, p - cached)
                cost = (
                    (real_prompt * PRICE_PROMPT / 1_000_000) +
                    (c * PRICE_COMPLETION / 1_000_000) +
                    (cached * PRICE_CACHED / 1_000_000)
                )
                total_cost_usd.set(cost)
                
                # 设置总Tokens和分项Tokens
                total_tokens_global.set((p or 0) + (c or 0))
                total_prompt_tokens_global.set(p or 0)
                total_completion_tokens_global.set(c or 0)
                total_cached_tokens_global.set(cached or 0)
                
                # 从conversations表获取准确的总请求数
                cursor.execute('SELECT COUNT(*) FROM conversations')
                total_reqs = cursor.fetchone()[0]
                total_requests_global.set(total_reqs)
            
            conn.close()
        except Exception as e:
            logger.warning(f"Failed to update user gauges: {e}")

    def get_deep_analytics(self) -> dict:
        """获取深度分析数据 (User Distribution, Median, Whales)"""
        try:
            conn = sqlite3.connect(self._db_file())
            c = conn.cursor()
            
            # 1. 真实对话统计 (排除 is_preset=1) - 这里我们统计每个用户的真实请求数
            # 注意：我们在 conversations 表里标记了 is_preset，但在 user_sessions 里只有 total_requests (包含所有)。
            # 所以我们需要从 conversations 表聚合。
            
            c.execute('''
                SELECT user_id, COUNT(*) as real_reqs
                FROM conversations
                WHERE is_preset = 0 AND user_id != 'anonymous'
                GROUP BY user_id
                HAVING real_reqs > 0
            ''')
            rows = c.fetchall()
            
            req_counts = [r[1] for r in rows]
            total_users = len(req_counts)
            
            if total_users == 0:
                stats = {
                    "avg": 0, "median": 0, 
                    "distribution": {"1": 0, "2-5": 0, "6-20": 0, "21-100": 0, "100+": 0},
                    "whales": {"top_10_percent_vol": 0, "total_vol": 0}
                }
            else:
                req_counts.sort()
                total_vol = sum(req_counts)
                avg_reqs = round(total_vol / total_users, 1)
                median_reqs = req_counts[total_users // 2]
                
                # Distribution
                dist = {"1": 0, "2-5": 0, "6-20": 0, "21-100": 0, "100+": 0}
                for count in req_counts:
                    if count == 1: dist["1"] += 1
                    elif 2 <= count <= 5: dist["2-5"] += 1
                    elif 6 <= count <= 20: dist["6-20"] += 1
                    elif 21 <= count <= 100: dist["21-100"] += 1
                    else: dist["100+"] += 1
                
                # Whales (Top 10%)
                top_10_count = max(1, int(total_users * 0.1))
                top_10_vol = sum(req_counts[-top_10_count:])
                top_10_percent = round((top_10_vol / total_vol) * 100, 1) if total_vol > 0 else 0
                
                stats = {
                    "avg": avg_reqs, 
                    "median": median_reqs, 
                    "distribution": dist,
                    "whales": {
                        "top_10_percent_vol": top_10_percent, 
                        "total_vol": total_vol
                    }
                }
            
            conn.close()
            return stats
        except Exception as e:
            logger.error(f"Analytics error: {e}")
            return {}

    def get_wordcloud_data(self) -> list:
        """生成词云数据"""
        try:
            conn = sqlite3.connect(self._db_file())
            c = conn.cursor()
            
            # 获取所有非预设的真实用户查询
            c.execute("SELECT user_query FROM conversations WHERE is_preset = 0 AND user_query != ''")
            queries = [row[0] for row in c.fetchall()]
            conn.close()
            
            text = "\n".join(queries)
            # 停用词
            stop_words = {'的', '了', '我', '是', '你', '在', '吗', '这', '那', '有', '个', '和', '就', '不', '人', '都', '一', '一个', '上', '也', '很', '到', '说', '去', '与', '会', '对', '但', '能', '而', '之', '用', '于', '着', '等', '及', '下', '以', '帮', '我', '把', '它', '什么', '可以', '如何', '怎么'}
            
            words = jieba.cut(text)
            counts = defaultdict(int)
            
            for word in words:
                if len(word) > 1 and word not in stop_words:
                    counts[word] += 1
            
            # Top 100
            sorted_words = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:100]
            return [{"word": w, "count": c} for w, c in sorted_words]
            
        except Exception as e:
            logger.error(f"Wordcloud error: {e}")
            return []


# ============ 日志解析 ============

def extract_user_query(body: dict) -> str:
    """从请求body中提取<user_query>内容"""
    try:
        messages = body.get("messages", [])
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                # 提取<user_query>标签内容
                match = re.search(r'<user_query>(.*?)</user_query>', content, re.DOTALL)
                if match:
                    return match.group(1).strip()
        return ""
    except Exception:
        return ""


def extract_player_name(body: dict) -> str:
    """从system prompt中提取玩家名 (Master名字)
    格式: - **Master**: 主人名字
    """
    try:
        messages = body.get("messages", [])
        for msg in messages:
            if msg.get("role") == "system":
                content = msg.get("content", "")
                # 匹配 - **Master**: 后面的文本
                match = re.search(r'-\s*\*\*Master\*\*:\s*(.+?)(?:\n|$)', content)
                if match:
                    name = match.group(1).strip()
                    # 排除默认值 "主人"
                    if name and name != "主人":
                        return name
        return ""
    except Exception:
        return ""


def extract_ai_response(body: dict) -> tuple:
    """从响应body中提取AI回复和action"""
    try:
        choices = body.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            
            # 提取tool_calls
            tool_calls = message.get("tool_calls", [])
            if tool_calls:
                action = tool_calls[0].get("function", {}).get("name", "")
                args = tool_calls[0].get("function", {}).get("arguments", "{}")
                try:
                    args_dict = json.loads(args)
                    # 提取talk或self_talk
                    talk = args_dict.get("talk", "")
                    self_talk = args_dict.get("self_talk", [])
                    if isinstance(self_talk, list):
                        self_talk = " | ".join(self_talk)
                    response = talk or self_talk
                except:
                    response = args
                return response[:500], action  # 限制长度
            
            # 普通content回复
            content = message.get("content", "")
            return content[:500], ""
    except Exception:
        pass
    return "", ""


def parse_jsonl_file(filepath: Path, state: MetricsState):
    """解析单个JSONL文件"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        if len(lines) < 2:
            return
        
        # 解析两行数据
        line1_data = json.loads(lines[0])
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
            
            if state.is_processed(filepath_str):
                continue
            
            parse_jsonl_file(jsonl_file, state)
            state.mark_processed(filepath_str)
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
