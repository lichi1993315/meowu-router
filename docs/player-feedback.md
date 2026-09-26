# 玩家截图反馈

POST `/feedback` 与 `/api/feedback` 接收 multipart：`metadata` 为既有 PAW Fernet 加密 JSON，`screenshot` 为可选 JPEG。元数据字段见 `player_feedback.validate`；`X-User-ID` 必填。沿用现有客户端身份/加密契约，内置加密不等于可信玩家身份，不把它用于业务授权。

可选 `contact` 为 QQ/微信联系方式，最多100字，去除首尾空白。与其他元数据一起持久化，非空时写入飞书“联系方式”文本列并包含在通知摘要中；空值不改变旧反馈的幂等指纹。上线前运行表格初始化脚本补齐正式/测试表的“联系方式”列，再发布接收端，最后发布客户端。

返回 202 accepted 表示元数据和图片已在 SQLite 同一事务提交，尚不代表飞书接收。相同编号、相同内容重试返回原回执；不同内容复用编号返回409。限每分钟每身份/IP三个新反馈，重复回执不占名额。总请求 <=2MiB+128KiB；队列最多1000个待投递项。请求从流中限量读取后才解析 multipart。

后台单消费者每轮最多10条，失败10/20/40/80/160/300秒退避；5分钟租约恢复。写飞书前按反馈编号查重，稳定 client_token 作为额外保护。保存 file_token 防止普通重试重复传附件；跨系统崩溃仍存在孤立附件/极小重复窗口，不承诺 exactly-once。成功7天后每轮最多清理10张数据库图片，保留幂等回执。

## 配置

- 沿用 `FEISHU_BOT_API_KEY`、`FEISHU_BOT_API_SECRET`、`PAW_FERNET_KEY`，只存服务器 `.env`。
- `python tools/setup_feedback_bitable.py --test` 创建测试 Base；不加 `--test` 创建正式 Base。每一步成功后更新对应环境变量，再运行会复用已有 Base/table。管理群授权失败会明确报告，不影响 API 接入。
- `FEISHU_FEEDBACK_APP_TOKEN`、`FEISHU_FEEDBACK_TABLE_ID`：正式投递目的地；测试脚本使用 `FEISHU_FEEDBACK_TEST_*`。
- `FEEDBACK_DB_PATH` 默认 `/app/output/feedback.sqlite3`，沿用持久卷。
- `FEEDBACK_TRUSTED_PROXY_IPS`：逗号分隔的可信内部入口 IP。仅这些直连地址允许解析 X-Forwarded-For；不要配置公网客户端地址。IP 变化后需更新，否则会保守地对代理地址共享限流。
- CORS 仅对两个反馈路径开放，允许 POST/OPTIONS 及 Content-Type/X-User-ID；不使用跨站 Cookie。

应用需要多维表格创建/读写、素材上传下载和目标表访问权限。团队还需要 Base 协作者权限；若 grant 返回1063002，需要管理员在飞书补齐权限。脚本创建待处理/已解决筛选视图及按版本视图；当前开放 API 没有分组配置字段，按版本视图的分组需在飞书界面选择“游戏版本”。

## 验证和上线

Docker 内执行 `python -m unittest tests.test_player_feedback tests.test_diagnostic_reports tests.test_game_telemetry_encryption -q`。

明确 opt-in 的 `python tools/verify_feedback_delivery.py /evidence/screenshot.jpg` 向测试表写一条“验证”记录，经真实 endpoint handler、持久化队列、附件上传、记录回读及附件下载字节比对验证。重复HTTP提交应只写一条记录。

先提交推送，服务器快进相同 Git 提交，然后 `docker compose -f docker-compose.monitoring.yml up -d --build --no-deps router-api`。上线核对公开 OPTIONS/POST 和队列 sent，不以 API accepted 代替飞书实收。


## 写表后的飞书通知

新反馈写表成功时，同一SQLite事务保存record_id并入队 `feedback_notifications`，随后沿用 error 通知的 `FEISHU_CHAT_ID` / `FEISHU_RECEIVE_ID_TYPE` 发送文字消息。包括描述（最多1200字）、复现办法（最多600字）、版本、平台、玩家、场景和带记录ID的表格链接；完整内容与截图留在表格。主动反馈不受自动error告警开关或Editor过滤影响。

通知失败单独退避重试，五分钟租约恢复，不重新上传截图或创建表格记录。稳定消息uuid使用 `player-feedback:<反馈编号>` 命名空间；飞书平台去重窗口之外的崩溃恢复仍可能重复消息。新旧反馈表协议和accepted回执不变。升级只新建通知表，不给已经完成写表的历史记录批量补发通知。

每轮最多处理10次写表和10次通知，通知查询使用(state,due)索引，单次只领取一条；写表待发与通知待发合计达到1000时拒绝新的反馈，已有编号重试仍返回原回执。仅保留有界摘要，不把错误堆栈或图片字节放进通知。

显式验证消息：在原集成验证命令后加 `--notify`，将向现有error收件地址发送一条标题/描述注明验证的消息，并检查通知sent及不重复发送。正常不带此参数的测试表验证不会主动发送消息。
