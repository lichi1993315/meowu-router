# Lumina 网页版测试埋点

客户端合同与指标口径见 lumina_pawfishing 仓库 `docs/ANALYTICS.md`。本服务只负责接收、汇总和报表；数据与 Unity 的 `analytics_event_facts` / journey / behavior 完全分开，不影响 PawFishing 报表。

## 接收

`POST /v1/lumina/events`（`/api` 下同）。浏览器放不住 Fernet 密钥，所以是明文 JSON，`Content-Type: text/plain`（简单请求，无预检）：

```json
{ "token": "<批次令牌>", "events": [ { "event_id": "<session_id>:<seq>", "session_id": "…", "player_id": "…", "seq": 1, "type": "boot", "t": "2026-10-26T08:00:00Z", "where": {}, "data": {} } ] }
```

- 每批 1–100 条、正文 ≤ 256 KiB；全部校验通过才在一个事务里写入，提交后返回 `accepted_event_ids`。同一 `event_id` 重发不重复计数；`event_id` 必须以自己的 `session_id:` 开头。
- 401 令牌错误，429 限流，503 未配置令牌 / 表未迁移 / 库忙。

环境变量：

| 变量 | 说明 |
| --- | --- |
| `LUMINA_INGEST_TOKENS` | 逗号分隔的批次令牌；为空时入口返回 503。删掉一个并重建 `router-api` 即吊销 |
| `LUMINA_ALLOWED_ORIGINS` | 允许的游戏页源，逗号分隔（`*` 为任意） |
| `LUMINA_RATE_PER_MINUTE` | 每 IP 每分钟请求数，默认 120 |
| `LUMINA_TRUSTED_PROXY_IPS` | 信任其 `X-Forwarded-For` 的入口 IP；缺省沿用 `FEEDBACK_TRUSTED_PROXY_IPS` |

## 表与报表

- `lumina_events`：原始事件。`lumina_sessions`：每会话汇总（加载、捏人、累计时长、按 seq 最新位置、报错、是否收到 leave）。`lumina_steps`：每会话每任务步骤的开始 / 完成有效秒数。`lumina_sessions_v`：加 `ended`（最后事件后 5 分钟）与时长桶。
- 报表 `grafana/provisioning/dashboards/lumina-playtest.json` 由 `python lumina_dashboards.py` 生成，不手改。

## 上线

1. 停库客户端并备份后执行维护迁移（`ensure_schema` 已包含 `ensure_lumina_schema`）：`python -m tools.telemetry_database --apply`；回读 `PRAGMA table_info(lumina_sessions)` 和 `lumina_sessions_v` 的 SQL 确认落库。router 启动不检查 `lumina`，未迁移只影响本入口。
2. 配置上面的环境变量，`docker compose -f docker-compose.monitoring.yml up -d --build --no-deps router-api`，`/health` 通过后 curl 发一条测试事件确认 200。
3. `restart grafana`，打开「Lumina 网页版测试：时长与跳出」。

验证：`python -m pytest tests/test_lumina_analytics.py`。
