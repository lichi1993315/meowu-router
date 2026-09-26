# 玩家行为时间线与画像实现

完整产品设计位于 PawFishing 仓库 `Docs/Design/AI/PlayerBehaviorPortraits.md`。本页记录 router 消费合同及发布验证。

## 入口与筛选

- `/d/gameplay-behavior-timeline`：填写玩家 ID 查看按时间排序的行为、原始证据、参与区间、早期及最近 7 天画像。事件时间线按事件会话和发生时间筛选，不因缺新版画像隐藏旧行为。
- `/d/gameplay-behavior-opening`：首个主动业务行为、前 5/15/30 分钟玩法、连续同类行为折叠。发起与结果不重复计数，提前离开者仍在分母。
- `/d/gameplay-behavior-cohorts`：24 小时早期画像、偏好 × 参与方式、后续时长/回访、Wilson 区间。默认 30 天 cohort，避免默认 7 天范围永远没有成熟 D7。
- `/d/gameplay-behavior-quality`：缺早期、无时钟、丢失尾段、无效区间、并发、待处理/超限。投影队列面板明确为全局运维指标。

画像报表按首次观察入岛会话选择平台/版本/渠道/批次，后续可跨平台回访；生产 cohort 不将 Editor/开发版回访算入正式留存。事件列表按事件自身会话筛选。`new_observed` 仅表示亲历创建角色，不证明全局新客；已有更早行为时早期证据标记缺失，不能把新版本上线当新玩家。

## 存储与处理

- `analytics_event_facts` 保留唯一事件及完整 payload/metadata。
- `behavior_actions` 在入库时按单事件 O(1) 更新，时间线不等待后台画像重放。只计明确玩家业务，猫自主动作/快照不计主动；`initiated` 区分发起与终态，`covered` 表示有参与时钟覆盖。
- `behavior_dirty` 为画像队列；`behavior_intervals` 保存同一客户端 run 相邻快照的累计差值；`behavior_players` 保存固定首 24 小时特征与 portrait-v1；`behavior_opening` 为开局汇总；`behavior_quality_points` 保存不确定性的时刻证据。
- 30 秒参与快照在 journey payload 的 `participation` 对象内，`version=participation-v1`。六状态和六主动分类必须分别守恒，单调时间与 UTC 不一致、负差、超过 90 秒缺口、并发重叠不当作完整时长。
- 匿名创建只通过明确 `journey_run_owners` 对应的同一 run 关联；不合并整个设备的用户。后到的匿名创建会重标对应已登录玩家。
- 每轮最多 8 玩家、每玩家 20000 个相关点，处理玩家之间有 2 秒软退出预算。固定事件集合走 user/kind 索引，自动事件不读入 JSON。超限撤下画像，行为日志及原始事实仍保留。
- 成熟度查询时计算，不需要等新事件才显示 D7。无回访成熟者为 0；有后续旧包行为或缺时钟证据者为 NULL；画像窗口之外的行为不会改变早期偏好。
- 第 2–7 天采用 [24h,168h)，D7 采用 [168h,192h)，时长先累计到玩家，再计算 P50/P75。检查点跨边界按比例分配，属于估算。

当前计时等待覆盖有明确 operation_id 的手动钓鱼；自由聊天尚无可贯通到客户端回复显示的请求关联时，不猜测等待区间。AI 生成的服务历时、已有原始对话和教程/解锁事实继续由原分析页展示，不把服务耗时当玩家注意力。未知触发来源保留 unknown，不能标成“自主选择”。

## 验证命令

使用已具备 FastAPI/cryptography 的测试环境：

```bash
python -m unittest tests.test_behavior_analytics tests.test_behavior_ingestion tests.test_player_analytics tests.test_telemetry_analytics tests.test_theater_analytics tests.test_incremental_api tests.test_import_gameplay_telemetry tests.test_journey_analytics -q
python tools/benchmark_behavior_analytics.py
python generate_analytics_dashboards.py
```

回归覆盖固定窗口、成熟/未成熟、没有回访、未知后续时长、跨自然日、并发、多平台、测试数据隔离、UID 0、重复/乱序、匿名关联、旧包、不可用计数、超限、SQL 空库/非空库，以及真正 `/v1/events/batch` 加密接收→规范事实→行为投影。

本地隔离 Grafana 用合成 AI/经营/挂机玩家渲染四页，验证 datasource 查询结果、浏览器错误及钻取字段。截图 `/tmp/paw-behavior-{timeline,opening,cohorts,quality}.png`，响应证据 `/tmp/paw-behavior-render.json`；合成数据不是线上玩家证据。

## 发布顺序与边界

此变更依赖同工作区的 journey 采集及投影，必须作为配套版本交付。未改 LLM prompt、MemoryPack 玩家输入结构或既有有效时长算法。

先提交并推送已验证代码，服务器快进同步；迁移真实 SQLite 并确认 behavior 表/索引，再重建 router-api 与 gameplay-importer，最后刷新 Grafana。Dockerfile 已包含 behavior_analytics.py，不能只上传 dashboard。历史事件只回填可证明的字段，无时间/无计数不补猜。

目前交付为本地开发与验证；未部署生产、未打包 Windows/WebGL、未证明真实多人或目标平台性能。最终数量与性能证据在 PawFishing 设计文档的验证记录中更新。
