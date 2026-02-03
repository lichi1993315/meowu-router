import asyncio
import os

from app.core.config import DB_PATH
from app.core.logging import log
from app.db.sqlite import get_connection


def fetch_blacklist_from_db() -> set[str]:
    try:
        if not os.path.exists(DB_PATH):
            return set()

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM user_sessions WHERE is_blacklisted = 1")
        users = {row[0] for row in cursor.fetchall() if row[0]}
        conn.close()
        return users
    except Exception as exc:
        log(f"[WARNING] Failed to fetch blacklist: {exc}")
        return set()


async def sync_blacklist_loop(state: object) -> None:
    log("🛡️ 黑名单同步服务启动")
    while True:
        try:
            new_blacklist = await asyncio.to_thread(fetch_blacklist_from_db)
            if new_blacklist != state.blacklist:
                log(f"🛡️ 黑名单更新: {len(new_blacklist)} users")
                state.blacklist = new_blacklist
        except Exception as exc:
            log(f"[ERROR] Blacklist sync error: {exc}")

        await asyncio.sleep(60)
