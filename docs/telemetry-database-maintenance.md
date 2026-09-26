# 遥测数据库维护与验证

实时上报、导入循环只检查 `telemetry_schema` 版本，不执行建表、视图重建或历史回填。缺少版本会阻止服务启动；先在维护窗口迁移，再启动新镜像。错误码 `telemetry_store_busy` 表示 SQLite 暂时不可写，HTTP 503 / `Retry-After: 15`；只有事务提交成功才确认接收。

## 维护窗口之外

- 可以提交、构建镜像和在独立数据目录测试。不得对线上数据库执行迁移、切 WAL、删除队列或重放积压。
- 检查命令默认只读：`python -m tools.telemetry_database --db /app/data/conversations.db`。检查不创建数据库、不主动 checkpoint。
- 用 `python -m tools.benchmark_telemetry_contention --directory <新目录> --seconds 1800 --target-gib 5.1` 运行合成历史数据的加密请求、长读快照、后台导入混合负载。输出包含数据规模、请求数、错误和延迟；不能代替线上验收。

## 已安排维护时

1. 按部署规范提交并推送，服务器检查干净后快进到同一提交。保存旧镜像 ID、Git SHA、服务配置及数据库相关文件清单。
2. 暂停所有共享数据库访问，包括 router-api、gameplay-importer、metrics-exporter、developer-admin、Grafana、日报及 operations-report。确认无其他连接；用 SQLite backup API 取得一致备份并在副本执行完整性检查。不得在 WAL 活跃时只复制主 db 文件。
3. 使用新 metrics 镜像作为一次性容器，挂载完整 data 目录，执行 `python -m tools.telemetry_database --db /app/data/conversations.db --apply --journal-mode wal`。行为画像历史玩家按每页 500 人入队并逐页提交，重复运行从已提交游标继续。回读四个迁移组件均 ready、journal_mode=wal、quick_check=ok。
4. 先启动 router-api，健康检查成功后启动只读报表和导入服务。API 持有空闲连接，保持只读容器需要的 WAL/SHM 文件；Grafana 的启动依赖 API 健康状态。保留现有只读挂载，不使用 immutable 读取活动数据库。
5. 核对镜像内源码、SHA、真实加密上报与数据库记录，再恢复游戏访问。新版游戏另按 7779 部署流程保存状态、验证隔离实例后切换。
6. 观察至少 30 分钟：锁导致的 5xx、`sqlite_transaction`、`sqlite_checkpoint`、待补传数和失败导入文件。按原 outbox ID 核对积压，不能用新心跳成功代替旧记录补齐证据。

## 工作量与失败边界

- 实时写入只等待一次，最长 10 秒；后台锁等待最长 1 秒。后台事务 SQL VM 进度与提交前检查采用 1 秒软预算，超预算回滚，源文件/旧投影保留，至少 300 秒后重试。
- 旅程每轮最多 16 个玩家、每玩家最多 100000 条事件；计算、行序列化和新旧差量比较在写事务外，发布前核对递增版本，仅提交变化行。超过历史上限保留旧结果并标记 `history_limit`，不得截断成看似完整的统计。
- 行为画像每轮最多 8 个玩家、每玩家最多 20000 个相关点。计算在写事务外完成，发布前核对递增版本；超时或锁冲突回滚并保留待处理状态，至少 300 秒后重试。超过历史上限撤下旧画像并标记 `history_limit`，原始事件与行为时间线保留。
- 文件导入按文件原子替换；文件解析及批量行构造在事务前完成。超预算的大文件不自动拆成半份统计，保留待处理并安排维护处理。
- 指标会话统计每 300 秒运行，先读与计算，再每 100 个用户独立写入。事务目标 P95≤250ms、max≤1s；提交 fsync 和单个系统调用无法被 SQLite progress handler 硬中断，必须记录实际耗时。
- `sqlite_transaction` 记录操作、请求/outbox ID、等待/事务/提交耗时及 SQLite 错误码，不包含正文或密钥。设置 `SQLITE_TRANSACTION_LOG_ALL=1` 可在隔离压测中记录所有事务，线上默认仅记录失败和 ≥250ms 操作。
- API 每 60 秒执行 PASSIVE 检查点并记录 WAL 大小/剩余页；持续增长时检查长读事务，不循环执行 TRUNCATE，也不删除 sidecar 文件。

## 回退

优先回退应用镜像，保留已接收的新数据。只有确需改回 DELETE 时，停止所有数据库访问后执行 `--apply --journal-mode delete`；命令检查 TRUNCATE checkpoint 是否完成。禁止删除 `-wal`/`-shm`，也禁止用维护前备份覆盖维护后新增记录。

## 隔离极限测试边界（2026-09-25）

10 万条旅程事件首次生成 99999 条 interval 时，写事务约 430ms；随后五轮中的四次相同历史刷新仅占用写锁 13–23ms。首次全量发布仍高于 250ms 目标、低于 1s 软上限，应在维护时完成历史预热，不能把增量刷新数据当成冷启动延迟。CPU 计算约 2.6–3.4s 在写事务外执行，不阻塞 API 写入；该数据仅来自本机合成数据库。
