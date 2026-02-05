# Catbook 前端对接 PRD

## 1. 目标
- 前端实现 Catbook 跨玩家同步：帖子、评论、点赞、收藏。
- 保证幂等上行、跨端统一排序、可分页拉取。

## 2. 基本信息
- Base URL（Docker 部署）：`http://localhost:9000`
- API 前缀：`/api/catbook`
- 鉴权：当前无（后续可能引入 token/HMAC）
- 时间戳：`server_created_at` 为 UTC 毫秒时间戳（int）

## 3. 关键字段约定
- `post_id` / `comment_id`：客户端生成，唯一且幂等。
- `author_id`：`"{actor_id}@{user_id}"`。
- `actor_global_id`：`"{actor_id}@{user_id}"`（用于 like/bookmark）。
- `cursor`：base64(JSON) 结构：`{"server_created_at": <int>, "post_id": "<str>"}`。
- 排序：`server_created_at` 倒序，`post_id` 倒序作为稳定 tiebreaker。

## 4. 错误码约定
- `invalid_payload` (400)
- `invalid_cursor` (400)
- `id_conflict` (409)
- `post_not_found` (404)
- `parent_comment_not_found` (404)
- `rate_limited` (429，预留)

## 5. 接口清单与字段

### GET /api/catbook/posts
用途：拉取 feed（按 `server_created_at` 倒序 + `post_id` 倒序稳定排序）  
Query：
- `cursor`（可选）
- `limit`（1-100）

Response 200:
```json
{
  "posts": [
    {
      "post_id": "p_u1_123",
      "author_id": "cat@u1",
      "title": "t",
      "content": "c",
      "text": "t",
      "image_id": null,
      "server_created_at": 1710000000000
    }
  ],
  "cursor": "base64(json)",
  "has_more": true
}
```

### POST /api/catbook/posts
用途：创建帖子（幂等）  
Request:
```json
{
  "post_id": "p_u1_123",
  "author_id": "cat@u1",
  "title": "t",
  "content": "c",
  "text": "t",
  "image_id": "optional"
}
```
Response 200:
```json
{
  "post_id": "p_u1_123",
  "server_created_at": 1710000000000
}
```
错误：
- 409 `id_conflict`（同 id 不同 payload）

### GET /api/catbook/posts/{post_id}
用途：单帖详情  
Response 200：单帖结构（同 `PostItem`）

### GET /api/catbook/posts/{post_id}/comments
用途：获取评论  
Response 200:
```json
{
  "comments": [
    {
      "comment_id": "c_u1_1",
      "post_id": "p_u1_123",
      "author_id": "cat@u1",
      "content": "hi",
      "parent_comment_id": null,
      "server_created_at": 1710000000000
    }
  ]
}
```

### POST /api/catbook/posts/{post_id}/comments
用途：创建评论（幂等）  
Request:
```json
{
  "comment_id": "c_u1_1",
  "post_id": "p_u1_123",
  "author_id": "cat@u1",
  "content": "hi",
  "parent_comment_id": "optional"
}
```
Response 200:
```json
{
  "comment_id": "c_u1_1",
  "server_created_at": 1710000000000
}
```
错误：
- 404 `post_not_found`
- 404 `parent_comment_not_found`
- 409 `id_conflict`

### POST /api/catbook/posts/{post_id}/like
### POST /api/catbook/posts/{post_id}/bookmark
用途：点赞 / 收藏（幂等）  
Request:
```json
{
  "post_id": "p_u1_123",
  "actor_global_id": "cat@u1"
}
```
Response 200:
```json
{
  "post_id": "p_u1_123",
  "actor_global_id": "cat@u1",
  "server_created_at": 1710000000000
}
```
错误：
- 404 `post_not_found`

### POST /api/catbook/sync
用途：批量上行（可选）  
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
      "server_created_at": 1710000000000
    },
    {
      "entity_id": "stable_key",
      "ok": false,
      "error_code": "parent_comment_not_found"
    }
  ]
}
```

## 6. 前端对接建议
- 本地/离线创建时先生成 `post_id/comment_id`，写入本地，再入队上行。
- 成功 ack 后以 `server_created_at` 更新本地排序字段。
- feed 拉取使用 `cursor` 循环请求直到 `has_more=false`。
- 对 `id_conflict` 记录日志并停止重试（视为不可恢复）。
- 对 `post_not_found/parent_comment_not_found` 标记为死信或提示用户。

## 7. 客户端系统划分（来自 catbook_sync.md）
- `CatbookSystem`：只处理本地状态与规则（post/comment/like/bookmark、排序、统计、事件），不做 HTTP。
- `CatbookSyncSystem`：异步同步（outbox、拉取、重试/退避、节流）。
- `CatbookApiClient`：封装 HTTP 调用。
- `CatbookSyncState`：持久化 outbox、last_cursor、last_sync 等同步状态。

### 最小接口定义
CatbookApiClient:
- `create_post(post: CatbookPostUpsert) -> CatbookPostAck`
- `create_comment(comment: CatbookCommentUpsert) -> CatbookCommentAck`
- `create_like(post_id: str, actor_id: str) -> InteractionAck`
- `create_bookmark(post_id: str, actor_id: str) -> InteractionAck`
- `fetch_feed(cursor: Optional[str], limit: int) -> CatbookFeed`

CatbookSyncSystem:
- `enqueue_outbox(item: CatbookOutboxItem) -> None`
- `request_refresh(reason: str) -> None`
- `update() -> None`（主线程消费回包，合并本地）

## 8. Outbox 结构（摘要）
- `kind`: `"post" | "comment" | "like" | "bookmark"`
- `entity_id`: post_id/comment_id 或 (post_id+actor_id+action) 稳定键
- `payload`: 对应 upsert 的字段
- `status`: `pending | in_flight | retry_wait | dead`
- `attempt`, `created_at`, `next_retry_at`, `last_error`

## 9. 前端触发点（同步拉取）
- 玩家打开小喵书 UI：`CatbookSyncSystem.request_refresh("ui_open")`
- 猫猫 `use_mode(catbook)`：进入 catbook 时拉取
- 猫猫 `catbook_browse`：执行前拉取

## 10. 合并与排序规则
- `CatbookSystem.merge_remote()`：按 `post_id/comment_id` 合并；已有则补齐 `server_created_at`，以服务端为准更新计数。
- 排序优先使用 `server_created_at`，缺失回退 `timestamp`。

## 11. 存档与恢复
- `CatbookSyncState` 需要序列化到存档（GameState 持有）。
- outbox 在重启后继续重试，确保离线期间操作最终同步。

