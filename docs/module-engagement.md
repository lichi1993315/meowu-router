# 玩法模块体验报表

Grafana UID：`gameplay-module-engagement`（导航「玩法模块体验」）。客户端口径见 PawFishing 仓库
`Docs/Design/AI/ModuleEngagementTelemetry.md`、`Docs/Design/AI/ModuleDesignQuestions.md`。

每个模块回答「玩家喜不喜欢、为什么」：看到入口 → 打开 → 尝试 → 成功使用 → 第 2–7 天回来再用；
不喜欢的原因看「点开没操作就关」「被拒/失败/放弃原因」「退出时所在界面」。

## 数据与投影

入库时 `analytics_facts.upsert_fact` 调用 `module_analytics.project_module_event`，每条事件 O(1) 写一行，不回放历史：

| 表 | 来源 | 内容 |
|---|---|---|
| `module_events` | 服务端信封带 `module_id` 的事件（`feature_action`、`currency_delta`、`offer_shown`、餐厅、旧业务事件等）；客户端 `ui_view_opened/closed`、`feature_entry_exposed` | 模块、动作、结果、原因、来源、是否玩家、是否引导中、餐厅营业阶段、闭店后分钟、内容 id、窗口停留和动作数、货币变动 |
| `module_time_points` | 旅程检查点里的 `participation.modules`（modules-v1） | 每个 run 的单调秒数 + 各模块累计主动秒数 |
| `module_exit_points` | 带 `view_stack` / `last_action` 的检查点、退出、切后台 | 当时最上层窗口、窗口层数、最后一个模块动作 |

- 客户端事实按 run id 入库；投影把 `session_id` 换成负载里的 `server_session_id`，平台/版本/批次/渠道筛选与服务端事件一致。
- 只有新版客户端（打了模块戳）才进投影；旧数据不回填，报表为空而不是 0。
- 菜单上架、货架曝光、小剧场退出这类低频事件直接读 `analytics_event_facts`（`event_type, occurred_at` 索引）。

## 口径

- **玩家动作**：`is_player=1`、`source` 不是 cat/system/automatic，且不是曝光、流水、窗口进出、系统事件。成功使用 = 结果为 success/started。
- **新手引导**：默认「去掉引导」，排除 `guided=1` 的动作；可切换「包含引导」。
- **主动分钟**：同一 run 相邻检查点差值之和（差值在整段 run 上算再按时间裁剪；负差记 0）。是 15 秒操作窗口估算，不是注意力时长。
- **复用/一次性**：按首次使用落在所选时间内的玩家；后续使用读到现在；首次使用后未满 7 天不进分母。
- **退出点**：每个 run 最后一个带窗口栈的检查点；「最后一次且已 3 天未回」= 该玩家之后再没有检查点，且距今 ≥3 天。不是退出原因。
- **闭店后**：玩家动作带 `restaurant_phase=closed` 和 `minutes_since_close`，按 0–10/10–30/30–60/60+ 分钟分段。营业阶段是全岛共享状态。

## 性能

本地合成 500 玩家、15 万事件：入库 0.05 ms/事件；最慢面板（喜欢信号，读全部历史）约 2.5 秒，其余 <1 秒。

## 验证与发布

```bash
python3 -m pytest tests/test_module_analytics.py
python3 generate_analytics_dashboards.py
```

新表只在维护窗口由 `python -m tools.telemetry_database --db /app/data/conversations.db --apply` 创建
（经 `ensure_schema → ensure_facts → ensure_module_schema`）；实时上报不建表。维护前实时入库遇到缺表会**跳过模块投影**，
原始事实照常入库、上报不受影响，看板在建表前显示缺表。维护流程见 `docs/telemetry-database-maintenance.md`，
完成后用 `sqlite3 ./data/conversations.db "PRAGMA table_info(module_events);"` 核对。
需要配套发布 PawFishing 分支 `claude/module-telemetry` 的客户端和服务器，否则报表没有数据；
建表前已经入库的新版事件不会自动补投影，需要时按原始事实重放 `project_module_event`。

2026-11-17 已部署代码（router-api、gameplay-importer、metrics-exporter），**尚未执行维护建表**。
