import sqlite3

from app.core.config import DB_PATH


def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)
