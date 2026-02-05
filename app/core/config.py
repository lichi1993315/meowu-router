import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]

OUTPUT_DIR = BASE_DIR / "output"
ERROR_LOG_DIR = OUTPUT_DIR / "error_logs"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
_gemini_base_url = os.getenv("GEMINI_BASE_URL", "").strip()
GEMINI_BASE_URL = (
    _gemini_base_url
    if _gemini_base_url
    else "https://generativelanguage.googleapis.com/v1beta/openai"
)
_gemini_sdk_url = os.getenv("GEMINI_SDK_URL", "").strip()
GEMINI_SDK_URL = (
    _gemini_sdk_url
    if _gemini_sdk_url
    else "https://generativelanguage.googleapis.com/v1beta"
)

PORT = int(os.getenv("PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", "/app/data/conversations.db")

R2_ENDPOINT = os.getenv("R2_ENDPOINT", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.getenv("R2_BUCKET", "meowu-island-photos")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL", "")
PHOTO_UPLOAD_DIR = os.getenv("PHOTO_UPLOAD_DIR", "/app/data/photos")
