# 飞书游戏运营报表

独立 `operations-report` 服务每次同步结束后等待 600 秒，直接只读访问分析库，不依赖公网 Grafana。复用打包时的已提交 `gameplay-overview.json` 统计 SQL，同步 19 项核心指标，分别展示 WebGL+Windows 跨平台去重汇总、WebGL、Windows，共 57 条固定记录。使用与用户分享链接一致的过滤：全部渠道、全部版本、全部批次、测试数据 auto、行为累计、渠道会话窗口近 7 天。日期为北京时间。各平台/渠道人数不可相加；累计值不能按刷新时间解释成当日新增。

每行标注更新时间、统计口径和可用的有效玩家数。未提供独立样本数的指标明确标注，不猜测。空值保持为空，并显示无有效样本；零仍为零。按指标键更新，写入后重新读取校验。数据库查询失败时不写飞书，飞书部分更新失败时各行时间可辨别新旧；下轮重试。更新不是跨飞书/SQLite 的原子事务。`output/operations-report/state.json` 保存新建资源 ID 和最近成功同步记录，不含凭证。不要删除该文件后重新 provision，否则会新建报表。

本版是核心指标表和三个平台视图，不含全量玩家明细、历史趋势存档或自由组合版本/批次分析；这些继续在 Grafana 查询。刷新 dashboard 口径后须重新构建本服务才能更新 SQL。

部署按 AGENTS.md 先提交推送，再让服务器 main 快进。仅部署独立服务，不重启已有游戏、采集或告警服务：

```bash
docker compose -f docker-compose.operations-report.yml build operations-report
docker compose -f docker-compose.operations-report.yml run --rm --no-deps operations-report --provision
docker compose -f docker-compose.operations-report.yml up -d --no-deps operations-report
```

凭证复用服务器 `.env` 中的 `FEISHU_BOT_API_KEY`、`FEISHU_BOT_API_SECRET`、`FEISHU_ADMIN_ID`（可选 `FEISHU_ADMIN_MEMBER_TYPE`）。只为现有管理员授予新报表编辑权限，关闭飞书授权通知，不发送群消息。SQL 按文本缓存，一轮 3 个范围、固定 19 指标，每个范围重复 SQL 只执行一次；整轮查询 90 秒超时，单服务最多 0.5 CPU、512 MB 内存，失败等待下个周期，锁防止重入。

检查：

```bash
python -m unittest discover -s tests -p test_operations_report.py -v
docker compose -f docker-compose.operations-report.yml logs --tail=10 operations-report
```
