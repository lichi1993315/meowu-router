# 服务器源码与发布记录

正式服务使用 `/root/develop/router` 的 `main` 分支。发布必须先提交并推送源码，再让服务器快进到同一提交；不要只复制单个文件后重建容器，否则 Git 历史无法说明线上运行的代码。

## 发布

1. 本地完成测试，提交本次改动并推送 `origin/main`。不要夹带仍在编辑的其他功能。
2. 服务器检查 `git status --short`。有源码改动时，先备份并合并回主分支；不要用 `reset --hard` 或 `git clean` 清除现场。
3. 确认工作区源码干净后执行：

   ```bash
   git fetch origin
   git merge --ff-only origin/main
   git rev-parse HEAD
   docker compose -f docker-compose.monitoring.yml up -d --build --no-deps router-api
   curl -fsS http://127.0.0.1:9000/health
   ```

4. 按受影响服务执行对应重建；Grafana 面板变化后重启 Grafana。数据库结构变化仍须遵守 `AGENTS.md` 的真实落库验证要求。
5. 记录提交号、容器镜像 ID、健康检查和必要的功能验证结果。修改源码不等于运行中的镜像已经更新。

`.env`、`.secrets/`、运行数据和备份不提交。服务器备份放在 `/root/router-backups/`，本地工具私有配置使用 `.git/info/exclude` 排除；不要用忽略规则隐藏尚未合并的源码。

Router 启动前会确认共享 SQLite 为 WAL 模式，使 Grafana 的长查询不阻塞心跳和事件提交。
首次从 DELETE 切换需要短暂取得数据库锁；切换失败会阻止启动，不能带着错误模式接收请求。
验收时回读 `PRAGMA journal_mode` 应为 `wal`，并验证只读 Grafana 数据源仍能查询。
在线备份使用 SQLite backup API；运行中不能只复制 `.db` 而忽略尚未 checkpoint 的 WAL。

## 每日用户日报

`grafana-report` 容器每日北京时间 09:00 运行 `daily_user_report.py`，统计前一天完整自然日并发送到现有 `FEISHU_CHAT_ID` 报表群。09:10、09:20 为有限重试；`output/user-reports/` 保存统计结果和发送回执，成功后同一天同一群跳过重复发送。三次都失败时检查 `output/report_cron.log`，修复后用 `--date YYYY-MM-DD` 补发；不自动补发停机期间的历史日期。

数据来自只读挂载的 `data/conversations.db`，复用 Grafana 的 `analytics_activity`、`analytics_first_entry` 和 `analytics_sessions` 视图，无需通过公网 Grafana 或截图服务。日活按登录/前台心跳跨平台去重；新增按正式用户首次入岛；排除 Editor、开发包、开发者及匿名身份，保留未知平台。次日留存为统计日前一天新增用户在统计日的回访。统计仅反映已入库数据，不推断未上报行为。

验证及手动补发（均作为一次性 Docker 服务运行）：

```bash
docker compose -f docker-compose.monitoring.yml run --rm --no-deps grafana-report python /app/daily_user_report.py --dry-run
docker compose -f docker-compose.monitoring.yml run --rm --no-deps grafana-report python /app/daily_user_report.py --date YYYY-MM-DD
```

每次运行复用两张连接内临时表，历史首次入岛需要读取历史会话；查询总计设置 30 秒 SQLite 指令级超时，失败不发送半份数据。成功回执按日期和接收群持久化，飞书请求使用稳定 UUID 辅助短期重试去重。保留回执目录；跨飞书 UUID 有效期的“已发送但未落回执”故障仍须人工核对群消息，不能保证跨系统绝对仅一次投递。
