import os
import uuid
from datetime import datetime
from pathlib import Path

import boto3
from botocore.config import Config
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.core.config import (
    PHOTO_UPLOAD_DIR,
    R2_ACCESS_KEY_ID,
    R2_BUCKET,
    R2_ENDPOINT,
    R2_PUBLIC_URL,
    R2_SECRET_ACCESS_KEY,
)

router = APIRouter()


def _require_env(value: str, name: str) -> str:
    if not value:
        raise HTTPException(status_code=500, detail=f"{name} not set")
    return value


def _get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=_require_env(R2_ENDPOINT, "R2_ENDPOINT"),
        aws_access_key_id=_require_env(R2_ACCESS_KEY_ID, "R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_require_env(R2_SECRET_ACCESS_KEY, "R2_SECRET_ACCESS_KEY"),
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            # 强制使用虚拟托管样式，这是 R2 的首选方式
            s3={"addressing_style": "virtual"},
            # 优化：增加重试次数，应对网络波动
            retries={"max_attempts": 3, "mode": "standard"},
            # 优化：设置连接超时，防止请求挂死
            connect_timeout=5,
            read_timeout=10
        ),
    )


@router.post("/photos/upload")
async def upload_photo(request: Request, file: UploadFile = File(...)):
    user_id = request.headers.get("x-user-id")
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-ID header")

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty upload")

    image_reference_id = f"img_{user_id[:8]}_{uuid.uuid4().hex[:12]}"
    object_key = f"photos/{user_id}/{image_reference_id}.png"
    content_type = file.content_type or "image/png"
    local_dir = Path(PHOTO_UPLOAD_DIR) / user_id
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / f"{image_reference_id}.png"

    try:
        local_path.write_bytes(image_bytes)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {exc}")

    try:
        s3_client = _get_s3_client()
        s3_client.upload_file(
            Filename=os.fspath(local_path),
            Bucket=_require_env(R2_BUCKET, "R2_BUCKET"),
            Key=object_key,
            ExtraArgs={"ContentType": content_type},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    public_url = _require_env(R2_PUBLIC_URL, "R2_PUBLIC_URL").rstrip("/")
    uploaded_at = datetime.now().isoformat()

    return JSONResponse(
        content={
            "image_reference_id": image_reference_id,
            "image_url": f"{public_url}/{object_key}",
            "uploaded_at": uploaded_at,
        }
    )
