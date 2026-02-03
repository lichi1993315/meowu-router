import os

from app.core.config import DB_PATH
from app.core.logging import log
from app.db.sqlite import get_connection


def get_leaderboard_data(user_id: str | None = None) -> dict:
    try:
        if not os.path.exists(DB_PATH):
            return {"entries": [], "rank": None}

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                COALESCE(nickname, player_name, 'anonymous') as name,
                money
            FROM user_sessions
            WHERE (is_developer = 0 OR is_developer IS NULL)
              AND (is_blacklisted = 0 OR is_blacklisted IS NULL)
              AND user_id != 'anonymous_user'
              AND user_id != ''
            ORDER BY money DESC
            LIMIT 10
            """
        )

        top_10 = [{"name": row[0], "money": row[1] or 0} for row in cursor.fetchall()]

        user_rank = None
        if user_id and user_id not in ("anonymous_user", ""):
            cursor.execute(
                """
                WITH ranked AS (
                    SELECT
                        user_id,
                        ROW_NUMBER() OVER (ORDER BY money DESC) as rank
                    FROM user_sessions
                    WHERE (is_developer = 0 OR is_developer IS NULL)
                      AND (is_blacklisted = 0 OR is_blacklisted IS NULL)
                      AND user_id != 'anonymous_user'
                      AND user_id != ''
                )
                SELECT rank FROM ranked WHERE user_id = ?
                """,
                (user_id,),
            )
            result = cursor.fetchone()
            if result:
                user_rank = result[0]

        conn.close()
        return {"entries": top_10, "rank": user_rank}
    except Exception as exc:
        log(f"[WARNING] Failed to get leaderboard: {exc}")
        return {"entries": [], "rank": None}
