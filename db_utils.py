import os
import asyncpg


def _get_env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def db_config() -> dict:
    return {
        "host": _get_env("DB_HOST", "postgres"),
        "port": _get_env("DB_PORT", "5432"),
        "dbname": _get_env("DB_NAME", "conversations"),
        "user": _get_env("DB_USER", "postgres"),
        "password": _get_env("DB_PASSWORD", "postgres"),
        "sslmode": _get_env("DB_SSLMODE", "disable"),
    }


def _normalize_sslmode(sslmode: str | None):
    if not sslmode:
        return None
    lowered = sslmode.lower()
    if lowered in {"disable", "false", "0", "off", "no"}:
        return None
    return True


def db_connect_kwargs() -> dict:
    cfg = db_config()
    return {
        "host": cfg["host"],
        "port": int(cfg["port"]),
        "database": cfg["dbname"],
        "user": cfg["user"],
        "password": cfg["password"],
        "ssl": _normalize_sslmode(cfg["sslmode"]),
    }


async def create_pool():
    return await asyncpg.create_pool(**db_connect_kwargs())


async def connect():
    return await asyncpg.connect(**db_connect_kwargs())
