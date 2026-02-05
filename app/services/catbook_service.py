import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.db.sqlite import get_connection


@dataclass
class CatbookError(Exception):
    error_code: str
    detail: str
    status_code: int


class InvalidPayloadError(CatbookError):
    def __init__(self, detail: str = "invalid payload") -> None:
        super().__init__("invalid_payload", detail, 400)


class NotFoundError(CatbookError):
    def __init__(self, error_code: str, detail: str) -> None:
        super().__init__(error_code, detail, 404)


class ConflictError(CatbookError):
    def __init__(self, detail: str = "id conflict") -> None:
        super().__init__("id_conflict", detail, 409)


class InvalidCursorError(CatbookError):
    def __init__(self, detail: str = "invalid cursor") -> None:
        super().__init__("invalid_cursor", detail, 400)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _encode_cursor(server_created_at: int, post_id: str) -> str:
    payload = {"server_created_at": server_created_at, "post_id": post_id}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> Tuple[int, str]:
    try:
        padded = cursor + ("=" * (-len(cursor) % 4))
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("cursor payload not dict")
        if "server_created_at" not in data or "post_id" not in data:
            raise ValueError("cursor missing fields")
        return int(data["server_created_at"]), str(data["post_id"])
    except Exception as exc:
        raise InvalidCursorError(str(exc)) from exc


def _normalize_player_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _rows_to_posts(rows: Iterable[Tuple[Any, ...]]) -> List[Dict[str, Any]]:
    posts = []
    for row in rows:
        posts.append(
            {
                "post_id": row[0],
                "author_id": row[1],
                "player_name": row[2],
                "title": row[3],
                "content": row[4],
                "image_id": row[5],
                "image_url": row[6],
                "server_created_at": row[7],
            }
        )
    return posts


def _rows_to_comments(rows: Iterable[Tuple[Any, ...]]) -> List[Dict[str, Any]]:
    comments = []
    for row in rows:
        comments.append(
            {
                "comment_id": row[0],
                "post_id": row[1],
                "author_id": row[2],
                "player_name": row[3],
                "content": row[4],
                "parent_comment_id": row[5],
                "server_created_at": row[6],
            }
        )
    return comments


def ensure_schema() -> None:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS catbook_posts (
                post_id TEXT PRIMARY KEY,
                author_id TEXT NOT NULL,
                player_name TEXT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                image_id TEXT,
                image_url TEXT,
                server_created_at INTEGER NOT NULL
            )
            """
        )
        cursor.execute("PRAGMA table_info(catbook_posts)")
        columns = {row[1] for row in cursor.fetchall()}
        if "player_name" not in columns:
            cursor.execute("ALTER TABLE catbook_posts ADD COLUMN player_name TEXT")
        if "image_url" not in columns:
            cursor.execute("ALTER TABLE catbook_posts ADD COLUMN image_url TEXT")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS catbook_comments (
                comment_id TEXT PRIMARY KEY,
                post_id TEXT NOT NULL,
                author_id TEXT NOT NULL,
                player_name TEXT,
                content TEXT NOT NULL,
                parent_comment_id TEXT,
                server_created_at INTEGER NOT NULL
            )
            """
        )
        cursor.execute("PRAGMA table_info(catbook_comments)")
        comment_columns = {row[1] for row in cursor.fetchall()}
        if "player_name" not in comment_columns:
            cursor.execute("ALTER TABLE catbook_comments ADD COLUMN player_name TEXT")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS catbook_likes (
                post_id TEXT NOT NULL,
                actor_global_id TEXT NOT NULL,
                server_created_at INTEGER NOT NULL,
                UNIQUE(post_id, actor_global_id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS catbook_bookmarks (
                post_id TEXT NOT NULL,
                actor_global_id TEXT NOT NULL,
                server_created_at INTEGER NOT NULL,
                UNIQUE(post_id, actor_global_id)
            )
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_catbook_posts_created ON catbook_posts(server_created_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_catbook_comments_post ON catbook_comments(post_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_catbook_comments_created ON catbook_comments(server_created_at)"
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_post(conn, post_id: str) -> Optional[Dict[str, Any]]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT post_id, author_id, player_name, title, content, image_id, image_url, server_created_at
        FROM catbook_posts
        WHERE post_id = ?
        """,
        (post_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return _rows_to_posts([row])[0]


def _fetch_comment(conn, comment_id: str) -> Optional[Dict[str, Any]]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT comment_id, post_id, author_id, player_name, content, parent_comment_id, server_created_at
        FROM catbook_comments
        WHERE comment_id = ?
        """,
        (comment_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return _rows_to_comments([row])[0]


def create_post(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_schema()
    conn = get_connection()
    try:
        existing = _fetch_post(conn, payload["post_id"])
        if existing:
            payload_player_name = _normalize_player_name(payload.get("player_name"))
            mismatch = any(
                existing[field] != payload.get(field)
                for field in ("author_id", "title", "content", "image_id", "image_url")
            )
            existing_player_name = _normalize_player_name(existing.get("player_name"))
            player_name_conflict = (
                payload_player_name
                and existing_player_name
                and payload_player_name != existing_player_name
            )
            if mismatch:
                raise ConflictError("post_id already exists with different payload")
            if player_name_conflict:
                raise ConflictError("post_id already exists with different player_name")
            if payload_player_name and not existing_player_name:
                conn.execute(
                    "UPDATE catbook_posts SET player_name = ? WHERE post_id = ?",
                    (payload_player_name, payload["post_id"]),
                )
                conn.commit()
            return {"post_id": existing["post_id"], "server_created_at": existing["server_created_at"]}

        server_created_at = _now_ms()
        conn.execute(
            """
            INSERT INTO catbook_posts
                (post_id, author_id, player_name, title, content, image_id, image_url, server_created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["post_id"],
                payload["author_id"],
                _normalize_player_name(payload.get("player_name")),
                payload["title"],
                payload["content"],
                payload.get("image_id"),
                payload.get("image_url"),
                server_created_at,
            ),
        )
        conn.commit()
        return {"post_id": payload["post_id"], "server_created_at": server_created_at}
    finally:
        conn.close()


def create_comment(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_schema()
    conn = get_connection()
    try:
        post = _fetch_post(conn, payload["post_id"])
        if not post:
            raise NotFoundError("post_not_found", "post not found")

        existing = _fetch_comment(conn, payload["comment_id"])
        if existing:
            payload_player_name = _normalize_player_name(payload.get("player_name"))
            mismatch = any(
                existing[field] != payload.get(field)
                for field in ("post_id", "author_id", "content", "parent_comment_id")
            )
            existing_player_name = _normalize_player_name(existing.get("player_name"))
            player_name_conflict = (
                payload_player_name
                and existing_player_name
                and payload_player_name != existing_player_name
            )
            if mismatch:
                raise ConflictError("comment_id already exists with different payload")
            if player_name_conflict:
                raise ConflictError("comment_id already exists with different player_name")
            if payload_player_name and not existing_player_name:
                conn.execute(
                    "UPDATE catbook_comments SET player_name = ? WHERE comment_id = ?",
                    (payload_player_name, payload["comment_id"]),
                )
                conn.commit()
            return {
                "comment_id": existing["comment_id"],
                "server_created_at": existing["server_created_at"],
            }

        parent_comment_id = payload.get("parent_comment_id")
        if parent_comment_id:
            parent = _fetch_comment(conn, parent_comment_id)
            if not parent or parent["post_id"] != payload["post_id"]:
                raise NotFoundError("parent_comment_not_found", "parent_comment_id invalid")

        server_created_at = _now_ms()
        conn.execute(
            """
            INSERT INTO catbook_comments
                (comment_id, post_id, author_id, player_name, content, parent_comment_id, server_created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["comment_id"],
                payload["post_id"],
                payload["author_id"],
                _normalize_player_name(payload.get("player_name")),
                payload["content"],
                parent_comment_id,
                server_created_at,
            ),
        )
        conn.commit()
        return {"comment_id": payload["comment_id"], "server_created_at": server_created_at}
    finally:
        conn.close()


def create_interaction(kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_schema()
    conn = get_connection()
    try:
        post = _fetch_post(conn, payload["post_id"])
        if not post:
            raise NotFoundError("post_not_found", "post not found")

        table = "catbook_likes" if kind == "like" else "catbook_bookmarks"
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT server_created_at
            FROM {table}
            WHERE post_id = ? AND actor_global_id = ?
            """,
            (payload["post_id"], payload["actor_global_id"]),
        )
        row = cursor.fetchone()
        if row:
            return {
                "post_id": payload["post_id"],
                "actor_global_id": payload["actor_global_id"],
                "server_created_at": row[0],
            }

        server_created_at = _now_ms()
        conn.execute(
            f"""
            INSERT INTO {table}
                (post_id, actor_global_id, server_created_at)
            VALUES (?, ?, ?)
            """,
            (payload["post_id"], payload["actor_global_id"], server_created_at),
        )
        conn.commit()
        return {
            "post_id": payload["post_id"],
            "actor_global_id": payload["actor_global_id"],
            "server_created_at": server_created_at,
        }
    finally:
        conn.close()


def list_posts(cursor: Optional[str], limit: int) -> Dict[str, Any]:
    ensure_schema()
    conn = get_connection()
    try:
        args: List[Any] = []
        where_clause = ""
        if cursor:
            created_at, post_id = _decode_cursor(cursor)
            where_clause = (
                "WHERE (server_created_at < ? OR (server_created_at = ? AND post_id < ?))"
            )
            args.extend([created_at, created_at, post_id])

        query = f"""
            SELECT post_id, author_id, player_name, title, content, image_id, image_url, server_created_at
            FROM catbook_posts
            {where_clause}
            ORDER BY server_created_at DESC, post_id DESC
            LIMIT ?
        """
        args.append(limit + 1)
        cursor_db = conn.cursor()
        cursor_db.execute(query, args)
        rows = cursor_db.fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        posts = _rows_to_posts(rows)
        next_cursor = None
        if posts:
            last = posts[-1]
            next_cursor = _encode_cursor(last["server_created_at"], last["post_id"])
        return {"posts": posts, "cursor": next_cursor, "has_more": has_more}
    finally:
        conn.close()


def get_post(post_id: str) -> Dict[str, Any]:
    ensure_schema()
    conn = get_connection()
    try:
        post = _fetch_post(conn, post_id)
        if not post:
            raise NotFoundError("post_not_found", "post not found")
        return post
    finally:
        conn.close()


def list_comments(post_id: str) -> Dict[str, Any]:
    ensure_schema()
    conn = get_connection()
    try:
        post = _fetch_post(conn, post_id)
        if not post:
            raise NotFoundError("post_not_found", "post not found")
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT comment_id, post_id, author_id, player_name, content, parent_comment_id, server_created_at
            FROM catbook_comments
            WHERE post_id = ?
            ORDER BY server_created_at ASC, comment_id ASC
            """,
            (post_id,),
        )
        comments = _rows_to_comments(cursor.fetchall())
        return {"comments": comments}
    finally:
        conn.close()


def sync_items(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    ensure_schema()
    acks: List[Dict[str, Any]] = []
    for item in items:
        entity_id = item.get("entity_id", "")
        kind = item.get("kind", "")
        payload = item.get("payload", {})
        try:
            if kind == "post":
                ack = create_post(payload)
                acks.append(
                    {
                        "entity_id": entity_id,
                        "ok": True,
                        "server_created_at": ack["server_created_at"],
                    }
                )
            elif kind == "comment":
                ack = create_comment(payload)
                acks.append(
                    {
                        "entity_id": entity_id,
                        "ok": True,
                        "server_created_at": ack["server_created_at"],
                    }
                )
            elif kind in ("like", "bookmark"):
                ack = create_interaction(kind, payload)
                acks.append(
                    {
                        "entity_id": entity_id,
                        "ok": True,
                        "server_created_at": ack["server_created_at"],
                    }
                )
            else:
                raise InvalidPayloadError("unknown kind")
        except CatbookError as exc:
            acks.append(
                {
                    "entity_id": entity_id,
                    "ok": False,
                    "error_code": exc.error_code,
                }
            )
        except Exception:
            acks.append({"entity_id": entity_id, "ok": False, "error_code": "unknown_error"})
    return {"acks": acks}
