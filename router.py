"""LLM API 路由代理服务 - 高并发版本

使用 httpx 异步客户端转发请求到 Gemini API
支持 100+ 并发请求
"""

import os
import sys
import json
import uuid
import httpx
import asyncio
import aiofiles
import sqlite3
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

# 添加项目根目录到 sys.path 以支持导入
_project_root = Path(__file__).parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from encrypted_saver import decrypt_string_only

# 请求记录输出目录
OUTPUT_DIR = Path(__file__).parent / "output"


# 敏感请求头（不保存到文件）
SENSITIVE_HEADERS = {"authorization", "x-api-key", "api-key", "cookie", "set-cookie"}


async def save_request_to_file(
    request_body: bytes, 
    path: str, 
    method: str, 
    headers: dict, 
    user_id: str | None,
    filepath: Path,
    timestamp_iso: str,
):
    """异步保存请求到JSONL文件（第一行）
    
    Args:
        request_body: 请求体
        path: 请求路径
        method: HTTP方法
        headers: 请求头
        user_id: 用户UUID（来自X-User-ID header）
        filepath: 文件路径
        timestamp_iso: ISO格式时间戳
    """
    try:
        # 解析请求体
        try:
            request_json = json.loads(request_body) if request_body else None
        except json.JSONDecodeError:
            request_json = request_body.decode("utf-8", errors="replace")
        
        # 过滤敏感请求头
        safe_headers = {
            k: v for k, v in headers.items() 
            if k.lower() not in SENSITIVE_HEADERS
        }
        
        # 构建请求记录
        request_record = {
            "type": "request",
            "timestamp": timestamp_iso,
            "user_id": user_id,
            "method": method,
            "path": path,
            "headers": safe_headers,
            "body": request_json,
        }
        
        # 异步追加写入文件
        async with aiofiles.open(filepath, "a", encoding="utf-8") as f:
            await f.write(json.dumps(request_record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[WARNING] Failed to save request log: {e}")


async def save_response_to_file(
    response_json: dict | None,
    response_status: int,
    duration_ms: float,
    user_id: str | None,
    filepath: Path,
    timestamp_iso: str,
):
    """异步保存响应到JSONL文件（第二行）
    
    Args:
        response_json: Gemini响应的JSON（包含usage等信息）
        response_status: HTTP响应状态码
        duration_ms: 请求耗时（毫秒）
        user_id: 用户UUID（来自X-User-ID header）
        filepath: 文件路径
        timestamp_iso: ISO格式时间戳
    """
    try:
        # 提取token usage信息（重要分析数据）
        usage = None
        if response_json and "usage" in response_json:
            usage = response_json["usage"]
        
        # 构建响应记录
        response_record = {
            "type": "response",
            "timestamp": timestamp_iso,
            "user_id": user_id,
            "duration_ms": round(duration_ms, 2),
            "status_code": response_status,
            "usage": usage,
            "body": response_json,
        }
        
        # 异步追加写入文件
        async with aiofiles.open(filepath, "a", encoding="utf-8") as f:
            await f.write(json.dumps(response_record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[WARNING] Failed to save response log: {e}")

# 加载 .env 文件
load_dotenv()

# 配置
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_BASE_URL = os.getenv(
    "GEMINI_BASE_URL", 
    "https://generativelanguage.googleapis.com/v1beta/openai"
)

PORT = int(os.getenv("PORT", "8000"))

# 全局 HTTP 客户端
http_client: httpx.AsyncClient = None

# 黑名单管理
DB_PATH = os.getenv("DB_PATH", "/app/data/conversations.db")
blacklist_users: set = set()

def fetch_blacklist_from_db() -> set:
    """从数据库读取黑名单用户ID"""
    try:
        if not os.path.exists(DB_PATH):
            return set()
            
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT user_id FROM user_sessions WHERE is_blacklisted = 1")
        users = {row[0] for row in c.fetchall() if row[0]}
        conn.close()
        return users
    except Exception as e:
        print(f"[WARNING] Failed to fetch blacklist: {e}")
        return set()

async def sync_blacklist_loop():
    """后台任务：定期同步黑名单"""
    global blacklist_users
    print("🛡️ 黑名单同步服务启动")
    while True:
        try:
            new_blacklist = await asyncio.to_thread(fetch_blacklist_from_db)
            if new_blacklist != blacklist_users:
                print(f"🛡️ 黑名单更新: {len(new_blacklist)} users")
                blacklist_users = new_blacklist
        except Exception as e:
            print(f"[ERROR] Blacklist sync error: {e}")
        
        await asyncio.sleep(60)  # 每60秒同步一次


@asynccontextmanager
async def lifespan(app: FastAPI):
    """生命周期管理：创建和销毁连接池"""
    global http_client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    )
    
    # 启动黑名单同步
    sync_task = asyncio.create_task(sync_blacklist_loop())
    
    yield
    
    sync_task.cancel()
    await http_client.aclose()


app = FastAPI(title="LLM Router", lifespan=lifespan)


@app.post("/login")
@app.post("/v1/login")
async def login(request: Request):
    """记录登录事件（不转发上游）"""
    try:
        raw_body = await request.body()
        user_id = request.headers.get("x-user-id")

        user_folder = user_id if user_id else "anonymous"
        user_dir = OUTPUT_DIR / user_folder
        user_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        timestamp_iso = datetime.now().isoformat()
        unique_id = str(uuid.uuid4())[:8]
        filename = f"{timestamp}-{unique_id}.jsonl"
        filepath = user_dir / filename

        asyncio.create_task(save_request_to_file(
            request_body=raw_body,
            path=str(request.url.path),
            method=request.method,
            headers=dict(request.headers),
            user_id=user_id,
            filepath=filepath,
            timestamp_iso=timestamp_iso,
        ))

        start_time = datetime.now()
        response_json = {"status": "ok"}
        duration_ms = (datetime.now() - start_time).total_seconds() * 1000
        asyncio.create_task(save_response_to_file(
            response_json=response_json,
            response_status=200,
            duration_ms=duration_ms,
            user_id=user_id,
            filepath=filepath,
            timestamp_iso=timestamp_iso,
        ))

        return JSONResponse(content=response_json, status_code=200)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    """转发 chat completions 请求到 Gemini"""
    api_key = GEMINI_API_KEY
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not set")

    try:
        # 读取请求体
        raw_body = await request.body()
        
        # 提取用户ID（来自X-User-ID header），没有则使用匿名
        user_id = request.headers.get("x-user-id") or "anonymous_user"
        
        # 🛡️ 检查黑名单
        if user_id and user_id in blacklist_users:
            print(f"🚫 Blocked blacklisted user: {user_id}")
            raise HTTPException(status_code=403, detail="Access denied")
        
        # 检查是否为加密请求
        is_encrypted = request.headers.get("x-encrypted", "").lower() == "true"
        
        if is_encrypted:
            # 解密请求体（直接解密为字符串）
            encrypted_str = raw_body.decode("utf-8")
            decrypted_str = decrypt_string_only(encrypted_str)
            if decrypted_str is None:
                raise HTTPException(status_code=400, detail="Failed to decrypt request body")
            body = decrypted_str.encode("utf-8")
        else:
            body = raw_body
        
        # 生成文件路径（在请求前生成，以便先写入request）
        user_folder = user_id if user_id else "anonymous"
        user_dir = OUTPUT_DIR / user_folder
        user_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # 精确到毫秒
        timestamp_iso = datetime.now().isoformat()
        unique_id = str(uuid.uuid4())[:8]
        filename = f"{timestamp}-{unique_id}.jsonl"
        filepath = user_dir / filename
        
        # 先保存请求到文件（异步，不等待）
        asyncio.create_task(save_request_to_file(
            request_body=body,
            path=str(request.url.path),
            method=request.method,
            headers=dict(request.headers),
            user_id=user_id,
            filepath=filepath,
            timestamp_iso=timestamp_iso,
        ))
        
        # 记录开始时间
        start_time = datetime.now()
        
        # 转发请求到Gemini
        response = await http_client.post(
            f"{GEMINI_BASE_URL}/chat/completions",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        
        # 计算耗时
        duration_ms = (datetime.now() - start_time).total_seconds() * 1000
        
        # 解析响应
        response_json = response.json()
        
        # 保存响应到文件（追加到同一个文件）
        asyncio.create_task(save_response_to_file(
            response_json=response_json,
            response_status=response.status_code,
            duration_ms=duration_ms,
            user_id=user_id,
            filepath=filepath,
            timestamp_iso=timestamp_iso,
        ))
        
        return JSONResponse(content=response_json, status_code=response.status_code)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream timeout")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    asyncio.run(uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT)).serve())
