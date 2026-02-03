# Router 项目重构方案（API 聚合 + Catbook 接入）

## 目标
- 将现有 `router.py` 逻辑拆分为清晰的分层结构，便于维护与扩展。
- 保持现有域名与入口不变，新增 `https://api.meowuisland.com/api/catbook/*` 路由。
- 兼容现有监控栈与 Docker Compose 部署方式。
- 为后续模块（认证、管理、扩展服务）预留结构。

## 目录结构（重构版）
```
router/
├── app/                             # 应用主目录（统一 API 入口）
│   ├── __init__.py
│   ├── main.py                      # FastAPI 应用入口（聚合所有子路由）
│   │
│   ├── api/                         # API 路由层
│   │   └── routes/
│   │       ├── gemini.py            # 代理 Gemini 路由（原 router.py 核心逻辑）
│   │       ├── embeddings.py        # embeddings 路由
│   │       ├── auth.py              # 未来预留（如需）
│   │       ├── system.py            # /health /leaderboard 等系统路由
│   │       └── catbook.py           # /api/catbook/* 路由入口
│   │
│   ├── core/                        # 配置 & 生命周期
│   │   ├── config.py                # Pydantic Settings
│   │   ├── lifespan.py              # httpx client / blacklist loop
│   │   └── logging.py               # 统一日志工具（_log）
│   │
│   ├── services/                    # 业务服务层
│   │   ├── gemini_proxy.py          # Gemini 转发 + 加密/解密
│   │   ├── sessions.py              # 会话日志逻辑
│   │   ├── blacklist.py             # 黑名单读取与同步
│   │   └── catbook_service.py       # catbook 业务逻辑
│   │
│   ├── db/                          # 数据库层
│   │   ├── __init__.py
│   │   ├── sqlite.py                # 现有 SQLite 连接（黑名单、leaderboard）
│   │   └── postgres.py              # Catbook / 未来业务 Postgres Session
│   │
│   ├── models/                      # ORM 模型
│   │   └── catbook.py               # Post/Comment/Like/Bookmark/Topic
│   │
│   ├── schemas/                     # Pydantic 模型
│   │   ├── catbook.py               # 请求/响应
│   │   └── system.py                # leaderboard/health
│   │
│   └── utils/                       # 工具函数
│       ├── crypto.py                # 解密逻辑包装
│       └── ids.py                   # 生成 id / actor_id 格式
│
├── scripts/                         # 维护脚本（迁移 / 运维）
│   ├── migrate_sqlite_to_postgres.py
│   └── send_report.py
│
├── docker/                          # Docker 相关
│   ├── Dockerfile.metrics
│   ├── Dockerfile.report
│   └── docker-compose.monitoring.yml
│
├── grafana/                         # Grafana provisioning
├── data/                            # 共享 volume
├── output/                          # 请求日志
├── README.md
├── requirements.txt
└── start.sh
```

## 路由层规划（API 入口）
- `app/main.py`
  - 创建 FastAPI 实例
  - 统一挂载所有子路由
  - 路由前缀统一为 `/api`（如需保持旧路径，可在路由层同时保留无前缀别名）

- `app/api/routes/gemini.py`
  - `/api/chat/completions`
  - `/api/v1/chat/completions`

- `app/api/routes/embeddings.py`
  - `/api/embeddings`
  - `/api/v1/embeddings`

- `app/api/routes/system.py`
  - `/api/health`
  - `/api/leaderboard`
  - `/api/v1/leaderboard`

- `app/api/routes/catbook.py`
  - `/api/catbook/posts`
  - `/api/catbook/posts/{post_id}`
  - `/api/catbook/posts/{post_id}/comments`
  - `/api/catbook/posts/{post_id}/like`
  - `/api/catbook/posts/{post_id}/bookmark`
  - `/api/catbook/sync`
  - `/api/catbook/topics/hot`

## 服务层拆分（业务逻辑归属）
- `services/gemini_proxy.py`
  - 负责 Gemini 转发、加密解密处理
  - 负责记录 request / response（保留现有逻辑）

- `services/sessions.py`
  - `login` / `logoff` / session event 记录
  - session 文件组织与索引

- `services/blacklist.py`
  - 从 SQLite 读取黑名单
  - 后台循环同步（替代 router.py 内部 loop）

- `services/catbook_service.py`
  - post / comment / like / bookmark 业务逻辑
  - 幂等写入、server_created_at 生成
  - feed / sync / topics 逻辑

## 数据库层规划
- `db/sqlite.py`
  - 读取 `conversations.db`（黑名单、leaderboard）
  - 保持现有 `DB_PATH` 环境变量

- `db/postgres.py`
  - 读取 `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD` 或 `DATABASE_URL`
  - 作为 Catbook 主数据库

## Catbook 集成方式
- Catbook API 作为 **子路由模块** 挂载到现有 FastAPI 入口。
- 复用同域名与同端口，路径前缀为 `/api/catbook`。

## 迁移映射（旧文件 -> 新位置）
- `router.py`
  - 路由定义迁移到 `app/api/routes/*`
  - 业务逻辑迁移到 `app/services/*`
  - 共享工具迁移到 `app/utils/*`

- `developer_admin.py`
  - 保持独立（写管理功能，不进行分析）

- `metrics_exporter.py`
  - 保持独立

## 兼容性与过渡策略
- 在新入口中保留原路径兼容（如 `/chat/completions` 无前缀）
- 新增 `/api/catbook/*` 不影响现有客户端
- 迁移可先做“模块拆分 + 路由挂载”，后续再重构细节

## 后续实施步骤（可选）
1. 创建 `app/` 目录结构与 `main.py`
2. 将 `router.py` 中的路由逐步迁移到 `app/api/routes/*`
3. 将核心逻辑拆到 `app/services/*`
4. 添加 `catbook` 路由与服务（接入数据库）
5. 更新 Dockerfile / 启动入口指向 `app.main:app`


## 已拆分完成的模块（当前状态）
- `app/core/config.py`：环境配置与路径常量
- `app/core/lifespan.py`：httpx client + 黑名单同步生命周期
- `app/services/sessions.py`：请求/响应日志、会话事件、错误日志
- `app/services/blacklist.py`：黑名单读取与后台同步
- `app/services/gemini_proxy.py`：Gemini 转发与 Embeddings 处理
- `app/services/leaderboard.py`：排行榜查询
- `app/api/routes/*`：路由已接入以上服务

## 入口切换指引（保持 router.py 不动）
- 旧入口：`python router.py` 或原有容器入口（保持可用）
- 新入口：`uvicorn app.main:app --host 0.0.0.0 --port 8000`

在确认新入口稳定后，再决定是否下线 `router.py`。

## Docker / Compose 切换
- 新的 Dockerfile：`docker/Dockerfile.router`
- Compose 已新增 `router-api` 服务，端口 `9000:8000`
- 若需本机启动：
  - 旧入口：`./start.sh`
  - 新入口：`USE_NEW_APP=1 ./start.sh`
