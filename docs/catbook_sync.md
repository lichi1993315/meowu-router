# 小喵书跨玩家同步 - 实现方案（最终 + 开发规划）

## 目标
- 实现小喵书跨玩家同步：不同玩家的猫猫发布的帖子、评论、点赞、收藏能够互相可见。
- 保证 post/comment/like/bookmark 的幂等上行与跨端一致排序。

## 关键决策
- 幂等：客户端生成 `post_id/comment_id` 作为最终 ID，服务端接受并以此去重。
- 时间：新增 `server_created_at`（UTC 时间戳）作为跨端排序主键。
- 身份：作者 ID 统一为 `"{actor_id}@{user_id}"`。`user_id` 使用 `src/core/llm.py:get_user_uuid()`。
- 无签名/鉴权。

---

## 架构总览

### 客户端
- `CatbookSystem`：只做领域状态（post/comment/like/bookmark、排序、统计、事件），不做 HTTP。
- `CatbookSyncSystem`：异步联机（outbox、拉取、重试/退避、节流）。
- `CatbookApiClient`：封装 `https://api.meowuisland.com/api` 的 HTTP 调用。
- `CatbookSyncState`：持久化 outbox、last_cursor、last_sync 等同步状态。

### 服务端
- FastAPI + PostgreSQL，接收客户端生成的 post_id/comment_id，并返回 `server_created_at`。
- 使用现有对外域名 `https://api.meowuisland.com`，新增路径前缀 `/api/catbook`。

---

## 风险与改进建议（按优先级）

### 必须先修
- 无签名/鉴权：任何人可伪造写入。
  - 建议：引入轻量鉴权（token + HMAC 签名）；若短期不做，至少添加速率限制与写入审计。
- 幂等冲突策略未定义：同 ID 但 payload 不一致时处理不清晰。
  - 建议：明确 409 + 返回原始记录，或仅允许补齐字段，禁止覆盖。
- Like/Bookmark 唯一键与身份不一致：唯一键用 `(post_id, actor_global_id)`，但接口只传 `actor_id`。
  - 建议：统一 `actor_global_id` 参数（或服务端拼接并校验），协议写清。
- `server_created_at` 精度/单位未统一：文档与结构体类型不一致。
  - 建议：统一为 `int` 毫秒时间戳（或 ISO8601），全链路一致。
- 游标语义不明确：分页可能重复/遗漏。
  - 建议：使用复合游标 `(server_created_at, post_id)`，base64 JSON 编码，按倒序稳定排序。

### 可后续（先不做）
- 读写一致性与 merge 规则不足。
  - 建议：服务端为权威源；客户端仅补齐字段，冲突记录落日志。
- 删除/撤销缺失（like/bookmark）。
  - 建议：后续增加 delete/unlike 接口或 tombstone。
- `Topic`/`hot` 规则未定义。
  - 建议：先空实现，后续补热度算法与索引。
- `parent_comment_id` 校验缺失。
  - 建议：校验 parent 存在且同帖；后续加防环。
- 批量 `/sync` 失败语义不明确。
  - 建议：per-item ack + error code，客户端按错误类型处理。
- 计数一致性未定义（实时聚合 vs 冗余字段）。
  - 建议：短期实时聚合，后续如冗余需事务内更新并幂等保护。

### 可忽略（短期不影响核心同步）
- `text`/`content` 字段语义边界模糊。
  - 建议：暂保留，后续在 schema 注释中明确用途。
- `author_id` 格式细节（大小写/命名空间）未定义。
  - 建议：暂定原样区分大小写，后续标准化。

## 后端架构（FastAPI + PostgreSQL）

### 新增文件（已对齐新项目结构）
- `app/api/routes/catbook.py`：Catbook API 路由入口（前缀 `/api/catbook`）。
- `app/services/catbook_service.py`：业务逻辑（幂等写入、查询、分页）。
- `app/db/postgres.py`：数据库连接/Session 管理。
- `app/models/catbook.py`：SQLAlchemy ORM 模型：
  - `Post`（主键 `post_id`）
  - `Comment`（主键 `comment_id`）
  - `Like`（唯一键 `post_id + actor_global_id`）
  - `Bookmark`（唯一键 `post_id + actor_global_id`）
  - `Topic`
- `app/schemas/catbook.py`：Pydantic 请求/响应：
  - `PostCreate`, `PostResponse`（含 `server_created_at`）
  - `CommentCreate`, `CommentResponse`（含 `server_created_at`）
  - `InteractionRequest`（like/bookmark）
  - `SyncRequest`, `SyncResponse`
  - `FeedResponse`（posts+comments+cursor）

### API 设计（与客户端一致）
统一前缀：`/api/catbook`
- `GET /posts`：拉取最新帖子（分页/游标）
- `POST /posts`：创建帖子（幂等，post_id 为主键）
- `GET /posts/{post_id}`：单帖详情
- `GET /posts/{post_id}/comments`：拉评论
- `POST /posts/{post_id}/comments`：创建评论（幂等，comment_id 为主键）
- `POST /posts/{post_id}/like`：点赞（幂等，唯一键）
- `POST /posts/{post_id}/bookmark`：收藏（幂等，唯一键）
- `POST /sync`：批量上行（可选）
- `GET /topics/hot`：热门话题

### API 规范（字段/错误码）

#### 通用约定
- `server_created_at`：`int` 毫秒时间戳（UTC），服务端生成，排序主键。
- `actor_global_id`：统一使用 `"{actor_id}@{user_id}"`，Like/Bookmark 必须传该字段。
- `cursor`：base64(JSON)，包含 `{"server_created_at": <int>, "post_id": "<str>"}`。
- 排序：`server_created_at` 倒序，`post_id` 倒序作为稳定 tiebreaker。

#### POST /posts
Request:
```json
{
  "post_id": "p_xxx",
  "author_id": "cat@user",
  "title": "string",
  "content": "string",
  "text": "string",
  "image_id": "optional"
}
```
Response 200:
```json
{
  "post_id": "p_xxx",
  "server_created_at": 1710000000000
}
```
Errors:
- 400 invalid_payload
- 409 id_conflict (same post_id, payload differs)

#### POST /posts/{post_id}/comments
Request:
```json
{
  "comment_id": "c_xxx",
  "post_id": "p_xxx",
  "author_id": "cat@user",
  "content": "string",
  "parent_comment_id": "optional"
}
```
Response 200:
```json
{
  "comment_id": "c_xxx",
  "server_created_at": 1710000000000
}
```
Errors:
- 400 invalid_payload
- 404 post_not_found
- 409 id_conflict

#### POST /posts/{post_id}/like | /bookmark
Request:
```json
{
  "post_id": "p_xxx",
  "actor_global_id": "actor@user"
}
```
Response 200:
```json
{
  "post_id": "p_xxx",
  "actor_global_id": "actor@user",
  "server_created_at": 1710000000000
}
```
Errors:
- 400 invalid_payload
- 404 post_not_found

#### GET /posts
Response 200:
```json
{
  "posts": [
    {
      "post_id": "p_xxx",
      "author_id": "cat@user",
      "title": "string",
      "content": "string",
      "text": "string",
      "image_id": "optional",
      "server_created_at": 1710000000000
    }
  ],
  "cursor": "base64(json)",
  "has_more": true
}
```
Errors:
- 400 invalid_cursor

#### GET /posts/{post_id}
Response 200: 单帖详情（含 `server_created_at`）
Errors:
- 404 post_not_found

#### GET /posts/{post_id}/comments
Response 200:
```json
{
  "comments": [
    {
      "comment_id": "c_xxx",
      "post_id": "p_xxx",
      "author_id": "cat@user",
      "content": "string",
      "parent_comment_id": "optional",
      "server_created_at": 1710000000000
    }
  ]
}
```
Errors:
- 404 post_not_found

#### POST /sync (optional)
Request:
```json
{
  "items": [
    {
      "kind": "post|comment|like|bookmark",
      "entity_id": "stable_key",
      "payload": {}
    }
  ]
}
```
Response 200:
```json
{
  "acks": [
    {
      "entity_id": "stable_key",
      "ok": true,
      "server_created_at": 1710000000000,
      "error_code": "optional"
    }
  ]
}
```
Errors:
- 400 invalid_payload

#### 错误码约定
- `invalid_payload` (400): 字段缺失/类型错误
- `id_conflict` (409): 同 ID 不同 payload
- `post_not_found` (404): 目标帖子不存在
- `invalid_cursor` (400): 游标无效/过期
- `rate_limited` (429): 触发限流

### 幂等约束（数据库）
- `posts.post_id` 唯一
- `comments.comment_id` 唯一
- `likes(post_id, actor_global_id)` 唯一
- `bookmarks(post_id, actor_global_id)` 唯一

---

## 客户端设计

### 新增模块
- `src/systems/catbook_api_client.py`：HTTP 封装（单向 IO）
- `src/systems/catbook_sync_system.py`：异步同步（outbox + refresh）
- `src/schemas/catbook_sync.py`：同步状态与 outbox 结构

### 保留模块
- `src/systems/catbook_system.py`：只处理本地状态与规则

---

## 最小接口定义

### CatbookApiClient
- `create_post(post: CatbookPostUpsert) -> CatbookPostAck`
- `create_comment(comment: CatbookCommentUpsert) -> CatbookCommentAck`
- `create_like(post_id: str, actor_id: str) -> InteractionAck`
- `create_bookmark(post_id: str, actor_id: str) -> InteractionAck`
- `fetch_feed(cursor: Optional[str], limit: int) -> CatbookFeed`

### CatbookSyncSystem
- `enqueue_outbox(item: CatbookOutboxItem) -> None`
- `request_refresh(reason: str) -> None`
- `update() -> None`（主线程消费回包，合并本地）

---

## ID 生成与作者格式

### 函数签名
```python
def build_post_id(user_id: str) -> str:
    """Return p_{user_id}_{uuid4hex}."""

def build_comment_id(user_id: str) -> str:
    """Return c_{user_id}_{uuid4hex}."""
```

### 作者 ID
- `author_id = f"{actor_id}@{user_id}"`
- `user_id = get_user_uuid()`

---

## Outbox 数据结构（具体字段定义）
```python
from typing import Literal, Optional, Dict, Any
from pydantic import BaseModel

class CatbookPostUpsert(BaseModel):
    post_id: str
    author_id: str
    title: str
    content: str
    text: str
    image_id: Optional[str] = None

class CatbookCommentUpsert(BaseModel):
    comment_id: str
    post_id: str
    author_id: str
    content: str
    parent_comment_id: Optional[str] = None

class CatbookInteractionUpsert(BaseModel):
    post_id: str
    actor_id: str
    action: Literal["like", "bookmark"]

class CatbookOutboxItem(BaseModel):
    kind: Literal["post", "comment", "like", "bookmark"]
    entity_id: str              # post_id/comment_id 或 (post_id+actor_id+action) 的稳定键
    payload: Dict[str, Any]     # 对应 Upsert 的 model_dump()
    status: Literal["pending", "in_flight", "retry_wait", "dead"] = "pending"
    attempt: int = 0
    created_at: float
    next_retry_at: float = 0.0
    last_error: Optional[str] = None
```

---

## 同步状态机（草图）

### 写入（outbox item）
```
PENDING -> IN_FLIGHT -> SUCCESS -> DONE
                    -> RETRYABLE_ERROR -> WAIT_RETRY -> PENDING
                    -> FATAL_ERROR -> DEAD
```

### 拉取
```
IDLE -> FETCHING -> APPLY -> IDLE
(节流：FETCHING/冷却中合并请求)
```

---

## 数据模型变更清单

### `src/schemas/catbook.py`
- `CatbookPost`
  - 新增：`server_created_at: Optional[int] = None`
- `CatbookComment`
  - 新增：`server_created_at: Optional[int] = None`
- `author_id` 语义统一：`cat_id@user_id` / `player@user_id`

### 新增 `src/schemas/catbook_sync.py`
- `CatbookOutboxItem`
- `CatbookSyncState`

---

## 前端触发点（同步拉取）
- 玩家打开小喵书 UI：`CatbookSyncSystem.request_refresh("ui_open")`
- 猫猫 `use_mode(catbook)`：进入 catbook 时拉取
- 猫猫 `catbook_browse`：执行前拉取

---

## 合并与排序规则
- `CatbookSystem.merge_remote()`：以 `post_id/comment_id` 合并；已有则补齐 `server_created_at`，以服务端为准更新计数。
- 排序优先使用 `server_created_at`，缺失回退 `timestamp`。

---

## 存档与恢复
- `CatbookSyncState` 需要序列化到存档（GameState 持有）。
- outbox 在重启后继续重试，确保离线期间操作最终同步。

---

## 数据流（示意）
```mermaid
sequenceDiagram
    participant UI as UI/Skill
    participant Sync as CatbookSyncSystem
    participant Local as CatbookSystem
    participant API as CatbookApiClient
    participant Server as FastAPI

    UI->>Local: create_post()/interact()
    Local->>Sync: enqueue_outbox()
    Sync->>API: POST /posts | /comments | /like | /bookmark
    API->>Server: HTTP
    Server-->>API: server_created_at
    API-->>Sync: ack
    Sync->>Local: mark_synced()

    UI->>Sync: request_refresh()
    Sync->>API: GET /posts
    API->>Server: HTTP
    Server-->>API: posts/comments
    API-->>Sync: feed
    Sync->>Local: merge_remote()
```

---

## 开发规划（后端优先）

### Milestone 0：准备
- 确认数据库：PostgreSQL 为 Catbook 主库（与现有监控栈同一 Compose）
- 统一入口：Catbook 路由挂载至 `app/main.py`，路径前缀 `/api/catbook`
- 依赖安装：`sqlalchemy`, `psycopg2-binary`（如需），`pydantic`

### Milestone 1：数据模型与迁移
- 在 `app/models/catbook.py` 建立 ORM 模型
- 建立唯一约束：`post_id`、`comment_id`、`(post_id, actor_global_id)` for like/bookmark
- 增加基础索引：`server_created_at`、`post_id`、`author_id`
- 编写初始化 SQL（或后续接入 Alembic）

### Milestone 2：核心写接口（幂等）
- `POST /posts`：基于 `post_id` 幂等 upsert
- `POST /posts/{post_id}/comments`：基于 `comment_id` 幂等 upsert
- `POST /posts/{post_id}/like` / `bookmark`：基于唯一键幂等
- 返回 `server_created_at`（新创建时生成，已存在则返回已有）

### Milestone 3：读取接口与分页
- `GET /posts`：支持 cursor + limit（按 `server_created_at` 倒序）
- `GET /posts/{post_id}`：单帖详情
- `GET /posts/{post_id}/comments`：按 `server_created_at` 排序
- `GET /topics/hot`：先返回空实现或简单聚合，后续迭代

### Milestone 4：批量 Sync 接口（可选）
- `POST /sync` 支持 posts/comments/likes/bookmarks 批量 upsert
- 返回批量 ack（含 `server_created_at`）

### Milestone 5：可观测性与稳定性
- 统一日志格式，打印请求量与错误率
- 增加简单的健康检查指标（可复用 `/api/health`）
- 可选：增加超时与重试策略（仅服务端）

## 验收清单（后端）
- 幂等：重复创建不新增记录，`server_created_at` 不变化
- 排序：feed 按 `server_created_at` 排序一致
- 兼容：现有 `/chat/completions` 等路径不受影响
- 路由：`/api/catbook/*` 可访问
