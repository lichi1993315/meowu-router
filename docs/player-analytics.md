# 玩家分析报表与采集合同

入口 `/d/gameplay-overview`（总览）、`/d/gameplay-players`（玩家一览）。列表链接到 `/d/gameplay-player-detail`，详情导航提供基础数据、游戏日和完整事件。采用 Grafana 原生页面，不创建另一套管理后台。

## 口径

- 总金币为最新余额；零值有效，不累加快照。猫数量只来自明确归属的最新完整快照，包括 UID 0、初始猫和猫舍内猫。缺失不从对话或工具事件推算。
- 时间为北京时间；新增取全局首次登录，昨日及 7/30 个完整自然日不包含今天。时长先按玩家累计去重会话，再计算平均数/中位数。
- 游戏日按玩家、稳定存档 `archive_id` 和 `game_day` 去重。缺存档标识的旧数据不能可靠合并，原始日 metadata 保留会话来源。
- 行为默认历史累计，可切换 Grafana 时间范围。当前状态有独立快照时间。默认包含 Windows/WebGL/unknown；测试数据沿用现有筛选。
- Top 5 按成功操作次数排序，购买另外显示件数；同名商品按稳定商品 ID 分组。做菜同时兼容 `dish_name` 和 `recipe_name`。服装排行按当前穿戴玩家数，不按换装次数。
- 体力新版只累计 `stamina_spent`；旧版业务事件标注旧版样本数，只有同会话/游戏日、相邻序号、相同位置和消耗的开垦双事件才去重。旧版失败钓鱼无法补采。
- 钓鱼记录实际水域、鱼影/普通水域和成功/失败/取消；抛竿数与已观察到结算数并列，未收到结算不能推断为失败。
- 猫升级剩余点数取新奖励之前，另保存奖励后值。工作投入点数与当前工作等级分布分开。
- 当前游戏每玩家一个个人猫舍，升级增加容量。使用率是有有效快照玩家中，从采集开始观察到主动放猫或当前已放猫的比例；未观察到不代表旧版本从未放过。
- 开始输入在首次非空输入时记录；上下文只取最近 60 秒、最多 32 条客户端视口内观察，并独立保存最近台词。明确区分发送与取消，不将模型草稿当作展示。
- 小剧场复用原邀请去重与分类规则，Top 5 默认至少 10 次邀请。Esc 为本地退出，未选择不推断为拒绝。

## 数据与发布

`analytics_event_facts` 是按事件 ID 增量维护的规范事件表，`gameplay_events` / `gameplay_live_events` 保留原始证据。最终会话快照、重传和不同文件的同一事件只计一次；旧事件没有 ID 时使用玩家、会话、游戏日和事件序号组合键。metadata 和 payload 分开保存。迁移标记 `event_facts_v1` 确保旧数据只回填一次。

先备份数据库，停止旧 importer，部署包含新 projection 的 router-api；运行新 importer 的 schema 迁移后核查真实表和视图，再启用新 dashboard。Grafana 配置刷新期间必须避免新查询早于迁移可见。回滚时保留原始表与备份，不删除已新增的原始埋点。

新增 Unity 采集需要客户端和服务器包含同一网络程序集版本；历史缺字段显示“未采集”。本次不修改 CatAgent LLM 请求和 prompt。

## 验证

`python -m unittest tests.test_player_analytics tests.test_telemetry_analytics tests.test_theater_analytics tests.test_incremental_api tests.test_import_gameplay_telemetry -q`

覆盖：实际零余额、猫 UID 0、跨存档游戏日、全量排序后分页、空筛选框、详情未选玩家、用户切回总览、重复批次/退出快照、完整 metadata、失败钓鱼体力、旧开垦双计、菜名字段、冻结/解冻、升级前点数和小剧场继续率分母。

本地 50 万事件/1000 玩家、每条普通 AI payload 约 1 KiB、每查询 5 次：总览玩家数 max 382ms，购买排行 max 95ms，采集覆盖 max 547ms，玩家排序列表 max 353ms，玩家事件分页 max 198ms。P95 使用最近秩法，在 5 次样本下等于最大值；这是本地 SQLite 数据查询，不代表线上浏览器或网络耗时。原始证据 `/tmp/paw-analytics-query-performance.json`。
