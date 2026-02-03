import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]

OUTPUT_DIR = BASE_DIR / "output"
ERROR_LOG_DIR = OUTPUT_DIR / "error_logs"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_BASE_URL = os.getenv(
    "GEMINI_BASE_URL",
    "https://generativelanguage.googleapis.com/v1beta/openai",
)
GEMINI_SDK_URL = os.getenv(
    "GEMINI_SDK_URL",
    "https://generativelanguage.googleapis.com/v1beta",
)

PORT = int(os.getenv("PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", "/app/data/conversations.db")
