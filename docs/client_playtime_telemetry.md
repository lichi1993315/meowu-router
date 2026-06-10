# 客户端游玩时长 Telemetry 接入说明

目标：即使玩家没有成功调用 `/logoff`，服务端也能通过登录和心跳完整记录本次游玩时长。

## Endpoint

推荐使用 v1 路径：

- `POST /v1/login`
- `POST /v1/session_heartbeat`
- `POST /v1/logoff`

当前服务端也保留无 `/v1` 的兼容路径。

所有请求都沿用现有加密协议：

- Body: Fernet 加密后的 JSON 字符串，`Content-Type: text/plain`
- Header: `X-Encrypted: true`

## 必填 Headers

每个 login、heartbeat、logoff 都必须带：

```http
X-Encrypted: true
X-User-ID: <account/user uuid>
X-Session-ID: <server/api session uuid>
X-Player-ID: <player id, usually same as user id>
X-Player-Session-ID: <gameplay session uuid>
X-Client-Version: <client version>
```

强烈建议每个待发送事件都带：

```http
X-Outbox-ID: <locally persistent event uuid>
```

`X-Outbox-ID` 用于服务端幂等。客户端重试同一个事件时必须复用同一个 `X-Outbox-ID`，不要重新生成。

## Session ID 规则

- `X-Session-ID`: 每次客户端启动/登录生成一个新的 UUID，不要跨启动复用。
- `X-Player-Session-ID`: 每次玩家进入一局/一次游戏会话生成一个新的 UUID。
- 同一次会话内，login、heartbeat、logoff 必须使用完全相同的两个 session id。

## /v1/login Payload

登录成功后立即发送。建议写入本地 outbox，收到 HTTP 200 后再删除。

```json
{
  "session_id": "api-session-uuid",
  "player_session_id": "player-session-uuid",
  "player_id": "player-uuid",
  "user_id": "user-uuid",
  "client_version": "unity-taptap-0.1.26.6.1.8d0bdf6d",
  "timestamp": "2026-06-10T12:00:00Z",
  "game_duration_sec": 0,
  "foreground_duration_sec": 0,
  "active_duration_sec": 0,
  "app_state": "foreground"
}
```

## /v1/session_heartbeat Payload

心跳用于补齐缺失 logoff 的时长。建议：

- 正常前台游玩时每 30 秒发送一次。
- 进入后台、暂停、恢复前台时立即发送一次。
- logoff 前先发送一次最后 heartbeat，再发送 logoff。
- 如果网络失败，进入本地 outbox 后台重试。

```json
{
  "session_id": "api-session-uuid",
  "player_session_id": "player-session-uuid",
  "player_id": "player-uuid",
  "user_id": "user-uuid",
  "client_version": "unity-taptap-0.1.26.6.1.8d0bdf6d",
  "timestamp": "2026-06-10T12:03:00Z",
  "sequence": 6,
  "game_duration_sec": 180,
  "foreground_duration_sec": 180,
  "active_duration_sec": 120,
  "app_state": "foreground",
  "last_gameplay_event_at": "2026-06-10T12:02:58Z"
}
```

字段语义：

- `sequence`: 本 session 内单调递增，从 1 开始。重试同一 heartbeat 时保持不变。
- `game_duration_sec`: 主口径，玩家本 session 累计有效游玩秒数，必须单调不减。服务端缺少 logoff 时优先采用这个值。
- `foreground_duration_sec`: App 在前台的累计秒数。
- `active_duration_sec`: 玩家有输入或游戏内有效活动的累计秒数，可选但建议上报。
- `app_state`: `foreground` / `background` / `paused` / `quitting`。

服务端成功响应示例：

```json
{
  "status": "ok",
  "duration_source": "heartbeat_client",
  "final_duration_sec": 180,
  "session_status": "open",
  "confidence": "high"
}
```

## /v1/logoff Payload

退出时继续发送现有完整 gameplay telemetry。`session_meta` 中必须包含最终时长和 session id。

```json
{
  "session_id": "api-session-uuid",
  "player_session_id": "player-session-uuid",
  "player_id": "player-uuid",
  "user_id": "user-uuid",
  "client_version": "unity-taptap-0.1.26.6.1.8d0bdf6d",
  "timestamp": "2026-06-10T12:10:00Z",
  "session_duration_sec": 600,
  "gameplay_telemetry": {
    "session_meta": {
      "session_id": "api-session-uuid",
      "player_session_id": "player-session-uuid",
      "client_version": "unity-taptap-0.1.26.6.1.8d0bdf6d",
      "real_time_started_iso": "2026-06-10T12:00:00Z",
      "real_time_ended_iso": "2026-06-10T12:10:00Z",
      "game_duration_sec": 600
    },
    "days": {}
  }
}
```

服务端最终时长优先级：

1. `/logoff` 的 `game_duration_sec` 或 `session_duration_sec`
2. 最大 heartbeat `game_duration_sec`
3. 服务端收到 login/heartbeat/logoff 的首尾时间差
4. 只有单个事件时记为 `login_only`，低可信

## 本地 Outbox 要求

客户端必须将 login、heartbeat、logoff 事件先落本地，再发送：

1. 生成事件 UUID，作为 `X-Outbox-ID`。
2. 持久化加密前 JSON、headers、endpoint、created_at。
3. 发送成功且 HTTP 200 后删除。
4. 网络失败、进程退出、非 200 响应时保留并重试。
5. 重试同一事件必须复用原始 payload、`sequence`、`X-Outbox-ID`。

## 服务端记录位置

服务端会写入 SQLite：

- `play_session_events`: 原始 login/heartbeat/logoff 事件。
- `play_session_rollups`: 按 `user_id + session_id` 聚合后的 session 时长。
- `playtime_session_detail`: Grafana 可直接查询的 session 明细 view。
- `playtime_player_summary`: Grafana 可直接查询的玩家汇总 view。

`duration_source` 和 `confidence` 必须一起看；只有 `logoff` 和 `heartbeat_client` 是高可信时长。
