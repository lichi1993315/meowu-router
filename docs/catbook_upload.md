图片上传与相册同步实现计划
目标
让猫在 catbook_post 时使用已拍摄并上传的照片，实现拍照自动上传、相册同步、同步状态展示。

一、数据模型
1.1 新建 src/schemas/photo_sync.py

class PhotoOutboxItem(BaseModel):
    filename: str                    # 本地文件名
    local_path: str                  # 本地绝对路径
    owner_id: str                    # 拍摄者 cat.id
    status: Literal["pending", "in_flight", "retry_wait", "dead"] = "pending"
    attempt: int = 0
    created_at: float
    next_retry_at: float = 0.0
    last_error: Optional[str] = None

class PhotoSyncState(BaseModel):
    outbox: list[PhotoOutboxItem] = []
1.2 扩展 album_metadata.json (v1 → v2)
新增字段：

width, height - 图片尺寸
sync_status - "pending" | "uploading" | "uploaded" | "failed"
image_reference_id - 服务器返回的唯一 ID
image_url - 可访问的 URL
server_uploaded_at - 服务器上传时间戳
last_error - 最后一次错误信息
二、客户端实现
2.1 新建 src/systems/photo_api_client.py
upload_photo(image_data, filename, owner_id) → 返回 {image_reference_id, image_url, uploaded_at}
使用 httpx，超时 30s
2.2 新建 src/systems/photo_sync_system.py
参考 catbook_sync_system.py 的 outbox 模式：

后台线程处理上传队列
enqueue_photo(local_path, owner_id) - 拍照后调用
get_uploaded_photos(owner_id) - 获取已上传照片列表
指数退避重试（最多 5 次）
上传成功后更新 album_metadata.json
2.3 修改 src/utils/album_store.py
新增函数：

update_photo_sync_status(photo_path, sync_status, image_reference_id=None, ...) - 更新同步状态
get_uploaded_photos_by_owner(owner_id) - 查询已上传照片
migrate_meta_v1_to_v2(meta) - 元数据迁移
record_photo_owner() 增加 width, height 参数
2.4 修改 src/tools/compose/photography.py
在 _capture_photo() 拍照完成后：


photo_sync = self.game_state.photo_sync_system
photo_sync.enqueue_photo(Path(file_path), self.agent.id)
2.5 修改 src/skills/catbook/tools/post.py
image_reference_id 改为 required
get_schema() 动态生成已上传照片的 enum 列表
start() 验证 image_reference_id 有效性
2.6 修改 src/game_state.py
初始化 PhotoSyncSystem
序列化/反序列化 photo_sync_state
2.7 修改 frontend/ui/album_ui.py
渲染同步状态徽章（✓ 绿色已上传 / ⏳ 黄色待上传 / ↑ 蓝色上传中 / ✗ 红色失败）
显示图片尺寸和拍摄时间元数据
三、服务器端实现
3.1 新建 app/api/routes/photos.py
新增 R2 集成：


import boto3
s3_client = boto3.client('s3', endpoint_url=R2_ENDPOINT, ...)
新增端点：


@router.post("/api/photos/upload")
async def upload_photo(file: UploadFile, request: Request):
    # 1. 验证 X-User-ID
    # 2. 生成 image_reference_id = f"img_{user_id[:8]}_{uuid.uuid4().hex[:12]}"
    # 3. 上传到 R2: photos/{user_id}/{image_reference_id}.png
    # 4. 返回 {image_reference_id, image_url, uploaded_at}
并在 app/main.py 注册路由：
app.include_router(photos.router, prefix="/api", tags=["photos"])
3.2 环境变量
R2_ENDPOINT - Cloudflare R2 端点
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
R2_BUCKET - 默认 "catbook-photos"
R2_PUBLIC_URL - 公开访问 URL 前缀
PHOTO_UPLOAD_DIR - 本地保存目录（默认 /app/data/photos）
3.3 依赖更新
requirements.txt 增加：
- boto3
- python-multipart （UploadFile 处理 multipart/form-data）
四、文件修改清单
文件	操作
src/schemas/photo_sync.py	新建
src/systems/photo_api_client.py	新建
src/systems/photo_sync_system.py	新建
src/utils/album_store.py	修改：新增同步状态函数
src/tools/compose/photography.py	修改：拍照后入队
src/skills/catbook/tools/post.py	修改：image_reference_id 必填
src/game_state.py	修改：注册 PhotoSyncSystem
frontend/ui/album_ui.py	修改：同步状态徽章
app/api/routes/photos.py	新增：R2 集成和上传端点
app/main.py	修改：注册 photos 路由
五、实现顺序
Phase 1 - 数据层

src/schemas/photo_sync.py
src/utils/album_store.py 扩展
Phase 2 - 服务器端

app/api/routes/photos.py R2 集成
Phase 3 - 客户端同步

src/systems/photo_api_client.py
src/systems/photo_sync_system.py
src/game_state.py 注册
Phase 4 - 业务集成

src/tools/compose/photography.py
src/skills/catbook/tools/post.py
Phase 5 - UI

frontend/ui/album_ui.py
六、验证方案
拍照流程：拍照后检查 album_metadata.json，新记录 sync_status="pending"
上传成功：等待上传，sync_status 变为 "uploaded"，有 image_reference_id
断网重试：模拟断网，恢复后自动重试上传
相册 UI：显示正确的同步状态徽章和元数据
发帖验证：catbook_post 只能选择已上传照片，缺少 image_reference_id 报错
