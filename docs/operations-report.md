# 飞书游戏运营报表

独立 `operations-report` 服务每次同步结束后等待 600 秒，直接只读访问分析库，不依赖公网 Grafana。复用打包时的已提交 `gameplay-overview.json` 统计 SQL，同步 19 项核心指标，分别展示 WebGL+Windows 跨平台去重汇总、WebGL、Windows，共 57 条固定记录。使用与用户分享链接一致的过滤：全部渠道、全部版本、全部批次、测试数据 auto、行为累计、渠道会话窗口近 7 天。日期为北京时间。各平台/渠道人数不可相加；累计值不能按刷新时间解释成当日新增。

每行标注更新时间、统计口径和可用的有效玩家数。未提供独立样本数的指标明确标注，不猜测。空值保持为空，并显示无有效样本；零仍为零。按指标键更新，写入后重新读取校验。数据库查询失败时不写飞书，飞书部分更新失败时各行时间可辨别新旧；下轮重试。更新不是跨飞书/SQLite 的原子事务。`output/operations-report/state.json` 保存新建资源 ID 和最近成功同步记录，不含凭证。不要删除该文件后重新 provision，否则会新建报表。

核心指标表有三个平台视图；每日趋势表固定270行，展示最近30天×3个范围×每日活跃/AI Tokens/AI费用，滚动更新日期与数值，不累积旧快照。复用已发布的活跃和AI每日明细查询，不把累计指标当成每日数据；AI缺失仍为空，今日明确标注未结束。全量玩家明细和自由组合版本/批次分析继续在 Grafana 查询。刷新 dashboard 口径后须重新构建本服务才能更新 SQL。

部署按 AGENTS.md 先提交推送，再让服务器 main 快进。仅部署独立服务，不重启已有游戏、采集或告警服务：

```bash
docker compose -f docker-compose.operations-report.yml build operations-report
docker compose -f docker-compose.operations-report.yml run --rm --no-deps operations-report --provision
docker compose -f docker-compose.operations-report.yml up -d --no-deps operations-report
```

凭证复用服务器 `.env` 中的 `FEISHU_BOT_API_KEY`、`FEISHU_BOT_API_SECRET`、`FEISHU_ADMIN_ID`（可选 `FEISHU_ADMIN_MEMBER_TYPE`，`FEISHU_OPERATIONS_MEMBER_ID` 可指定报表协作者邮箱）。只为指定报表协作者或现有管理员授予新报表编辑权限，关闭飞书授权通知，不发送群消息。SQL 按文本缓存，一轮 3 个范围、固定 19 指标，每个范围重复 SQL 只执行一次；整轮查询 90 秒超时，单服务最多 0.5 CPU、512 MB 内存，失败等待下个周期，锁防止重入。

检查：

```bash
python -m unittest discover -s tests -p test_operations_report.py -v
docker compose -f docker-compose.operations-report.yml logs --tail=10 operations-report
```

每日趋势另设60秒SQL预算，每个范围两条查询，共六条；最多270条记录，飞书批量同步。运行中的旧服务在 trend_table 未创建前继续只同步核心指标。

## 原生仪表盘

`tools/setup_operations_dashboard.py --plan-only` 生成26个组件配置：19指标卡、2平台对比、1渠道对比、3每日趋势、1说明。使用官方 Lark CLI 1.0.96，通过应用身份操作已经创建的 Base。实际搭建需应用发布 `base:dashboard:read`、`base:dashboard:create`、`base:dashboard:update`、`base:table:read`、`base:field:read` 权限。当前数据同步沿用既有 bitable v1 权限，仪表盘授权是独立前置条件。令牌仅经环境注入CLI，不打印或落盘。

```bash
python3 tools/setup_operations_dashboard.py --cli /tmp/operations-lark-cli
```

组件配置已准备不等于仪表盘已创建；必须确认命令成功并核验计算数据及实际页面后交付。
