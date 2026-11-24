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

4. 按受影响服务执行对应重建。数据库结构变化仍须遵守 `AGENTS.md` 的真实落库验证要求。
5. 记录提交号、容器镜像 ID、健康检查和必要的功能验证结果。修改源码不等于运行中的镜像已经更新。

`.env`、`.secrets/`、运行数据和备份不提交。服务器备份放在 `/root/router-backups/`，本地工具私有配置使用 `.git/info/exclude` 排除；不要用忽略规则隐藏尚未合并的源码。

Router 启动前会确认共享 SQLite 为 WAL 模式，使 Grafana 的长查询不阻塞心跳和事件提交。
首次从 DELETE 切换需要短暂取得数据库锁；切换失败会阻止启动，不能带着错误模式接收请求。
验收时回读 `PRAGMA journal_mode` 应为 `wal`，并验证 Agent 的只读 SQLite 查询。
Router 生命周期内保留一个已完成初始化、没有活动事务的 SQLite 连接，防止最后一个短连接关闭时删除 `-wal` / `-shm`。Grafana 的只读挂载依赖这两个文件已存在；仅启用 WAL 后立即关闭连接，可能导致所有面板报 `unable to open database file (14)`。该空闲连接不持有读快照，允许正常写入和 checkpoint；修改此逻辑后须重建 `router-api`，再通过 Grafana 数据源实际查询验收。
在线备份使用 SQLite backup API；运行中不能只复制 `.db` 而忽略尚未 checkpoint 的 WAL。

## 查询与旧监控栈

2026-11-24 生产部署停用 Prometheus、Grafana、截图渲染器和 `grafana-report` 每日推送。玩家数据仍由 Router 写入 SQLite，Agent 直接只读查询。独立的 [飞书运营报表](operations-report.md) 继续通过 SQLite 同步。后续部署时只启动当前 Compose 文件列出的服务。

## 问卷、愿望单与大佬猫报表

`/d/gameplay-campaigns` 展示问卷点击、领猫入口、前往 Steam、点击领取、服务端发奖成功/失败，以及曝光、关闭、客户端回执。支持时间、平台、版本、批次、渠道和测试数据筛选；默认全部批次，保留尚未入岛的主菜单事件。其他分析页的导航中可直接进入。

次数按 `event_id` 去重，已登录玩家按 `user_id` 去重，`anon:` 匿名身份及缺失身份事件单列，不推断跨身份合并。前往 Steam 不等于实际加入愿望单，问卷点击不等于提交。步骤是独立覆盖数，不计算严格漏斗转化率；新埋点上线时间不同，领取成功人数可能多于已采集的领取点击人数。客户端 `claimed` 回执和服务端 `wishlist_reward_claimed` 发奖分别展示，不能相加。历史缺时间/未采集事件不补算。

生成：`python3 generate_analytics_dashboards.py`；回归：`python3 -m unittest tests.test_campaign_dashboards`。不修改数据库结构或采集端。旧 Grafana 面板在 2026-09-27 发布前全部 9 个新面板查询在生产只读库执行成功，单次耗时约 0.4～509 ms；81 项相关回归通过。此为当前数据规模的数据库查询证据，不代表浏览器渲染验证。
