# Gameplay AI Token / Cost Metrics

## 数据来源

Gameplay logoff telemetry 中的 AI 消耗优先使用：

1. `gameplay_telemetry.session_meta.ai_token_usage`
2. 若 session 汇总缺失，则回退聚合 `cat_agent_response.payload.response_stats`

事件级明细会写入 `gameplay_ai_calls`，session 汇总会写入 `gameplay_sessions.ai_*` 字段。

## 关键字段

- `ai_request_count`: `cat_agent_request` 事件数
- `ai_response_count`: `cat_agent_response` 事件数
- `ai_token_record_count`: 包含 token 数据的 AI response 记录数
- `ai_input_tokens` / `ai_output_tokens` / `ai_total_tokens`
- `ai_cached_input_tokens`: 命中缓存的 input token
- `ai_billable_uncached_input_tokens` / `ai_billable_cached_input_tokens`
- `ai_cache_hit_ratio`: `cached_input_tokens / input_tokens`
- `ai_estimated_cost_usd`: 按 telemetry 上报的 USD 单价估算
- `ai_archive_total_consumed_tokens`: 客户端归档累计消耗 token
- `ai_models`: session 中出现过的模型名

## 成本公式

```text
cost_usd =
  billable_uncached_input_tokens * input_usd_per_million_tokens / 1_000_000
  + billable_cached_input_tokens * cached_input_usd_per_million_tokens / 1_000_000
  + output_tokens * output_usd_per_million_tokens / 1_000_000
```

若 telemetry 没有上报单价，成本显示为 `0`，但 token 数仍保留。

## Grafana 面板

`Gameplay 数据总览` 增加 `AI Token / 成本` 区块：

- 汇总：AI 调用次数、Total Tokens、成本 USD、缓存命中率
- 用户维度：AI 成本 Top 用户
- Session 维度：AI 成本 Top Session
- 模型/版本维度：AI 模型/客户端版本成本
- 日期维度：AI 每日消耗

`Gameplay 玩家详情` 增加同名区块：

- 玩家汇总：AI 调用次数、Total Tokens、成本 USD、缓存命中率
- `AI Session 明细`
- `AI Call 明细`

## 飞书 Logoff 汇总

飞书 logoff 消息会展示当前 session 的 token 信息，并额外展示用户维度历史汇总。历史汇总来自 `gameplay_sessions`，如果当前 logoff 还没被 importer 写入 SQLite，会把当前 payload 临时合并进去，避免最新 session 漏算。

- `user_total_sessions`: 用户总 session 数
- `user_total_playtime`: 用户累计游玩时长
- `user_total_ai_tokens`: 用户所有 session 的 AI token 总和
- `user_total_ai_cost_est_usd`: 用户累计 AI 成本估算
- `user_total_ai_calls`: 用户累计 AI request/response/token record 数
- `user_ai_cache_hit`: 用户累计 input token 的缓存命中率
- `user_progress_max`: 用户历史最高游戏天数和岛屿等级
- `user_session_window`: 用户首个/最新 telemetry session 时间
