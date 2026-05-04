# Docker 代码规范

本项目使用 Docker 容器化部署。所有服务都应该在 Docker 容器内运行，以确保一致的环境和正确的资源访问。

## 核心原则

### ✅ 正确做法：容器内直接访问

所有需要访问共享资源（数据库、文件等）的服务都应该：

1. **作为独立的 Docker 服务运行**
2. **通过 volume 挂载共享数据目录**
3. **直接在容器内访问资源**

```yaml
# docker-compose.yml 示例
services:
  my-service:
    build:
      context: .
      dockerfile: Dockerfile.xxx
    volumes:
      - ./data:/app/data  # 共享数据目录
    environment:
      - DB_PATH=/app/data/database.db
```

### ❌ 避免的做法：docker exec

**不要使用 `docker exec` 从宿主机执行容器内命令**

原因：
- 网络延迟大，命令执行时间长
- 容易超时
- 增加系统复杂度
- 权限问题复杂

```python
# ❌ 错误示例 - 不要这样做
def run_sql():
    cmd = f'docker exec container_name python3 -c "..."'
    subprocess.run(cmd, shell=True)  # 太慢！
```

## 服务架构示例

当前监控系统架构：

```
┌─────────────────────────────────────────────────────────────┐
│                    Docker Compose Stack                      │
├─────────────────┬─────────────────┬─────────────────────────┤
│ metrics-exporter│ developer-admin │ grafana / prometheus    │
│     :9090       │     :9095       │   :9001 / :9091         │
├─────────────────┴─────────────────┴─────────────────────────┤
│                  共享 Volume: ./data                         │
│                  (conversations.db, exporter_state.json)     │
└─────────────────────────────────────────────────────────────┘
```

## 添加新服务的步骤

1. **创建 Python 脚本**
   - 使用环境变量配置路径：`DB_PATH = os.getenv("DB_PATH", "/app/data/xxx.db")`
   - 直接使用 sqlite3 连接数据库

2. **复用现有 Dockerfile 或创建新的**
   - 在 `Dockerfile.metrics` 中添加 `COPY your_script.py .`
   - 或创建新的 Dockerfile

3. **在 docker-compose.yml 添加服务**
```yaml
your-service:
  build:
    context: .
    dockerfile: Dockerfile.metrics
  volumes:
    - ./data:/app/data  # 关键：挂载共享数据目录
  environment:
    - DB_PATH=/app/data/conversations.db
  command: ["python", "your_script.py"]
  ports:
    - "xxxx:xxxx"
```

4. **部署服务**
```bash
sudo docker compose -f docker-compose.monitoring.yml up -d --build your-service
```

## 数据库访问模式

```python
import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "/app/data/conversations.db")

def get_db():
    return sqlite3.connect(DB_PATH)

def query_data():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM table")
    result = cursor.fetchall()
    conn.close()
    return result
```

## 常用命令

```bash
# 启动所有服务
sudo docker compose -f docker-compose.monitoring.yml up -d

# 重建并启动单个服务
sudo docker compose -f docker-compose.monitoring.yml up -d --build service-name

# 查看日志
sudo docker compose -f docker-compose.monitoring.yml logs -f service-name

# 查看服务状态
sudo docker compose -f docker-compose.monitoring.yml ps
```

## Analytics 开发规范

### ✅ 所有分析功能在 Grafana 开发

**Grafana 是数据分析的首选平台**。以下功能应在 Grafana Dashboard 中实现：

- **DAU / 留存率** - 使用 SQLite 数据源直接查询
- **用户分布 / 深度分析** - 饼图、柱状图
- **热门词汇 / Word Cloud** - 表格或柱状图展示 Top N
- **用户行为统计** - 时段分布、活跃度等

### ✅ SQLite Schema / View 迁移必须真实落库

只修改 Python 代码、SQL 字符串、Grafana Dashboard JSON **不算完成迁移**。凡是新增/修改 SQLite 的 table、column、index、view，必须确保变更已经实际落到当前 `./data/conversations.db`，否则 Grafana 线上报表可能直接不可用。

必须同时满足：

1. **代码已更新** - 例如 `SCHEMA_SQL`、`VIEW_SQL`、查询语句已修改
2. **现有 SQLite 已迁移** - 目标表/列/view 已经在当前数据库中真实存在
3. **依赖方已刷新** - Grafana 或相关 importer/service 已重启或重跑，开始读取新结构

常见风险：

- Dashboard 已引用新列，但线上 SQLite view 还是旧版本
- `CREATE VIEW IF NOT EXISTS` 不会覆盖旧 view，必须显式 `DROP VIEW IF EXISTS` 后重建
- 代码里新增了 `ensure_schema()` / migration 逻辑，但容器未执行，线上库不会自动更新

发布前至少验证：

```bash
# 查看当前线上 SQLite 中的真实 view / schema
sqlite3 ./data/conversations.db "SELECT sql FROM sqlite_master WHERE type='view' AND name='your_view';"
sqlite3 ./data/conversations.db "PRAGMA table_info(your_table);"

# 必要时执行迁移逻辑，让现有库真实更新
sudo docker compose -f docker-compose.monitoring.yml up -d --build service-name
sudo docker compose -f docker-compose.monitoring.yml restart grafana
```

如果 Grafana 面板依赖某个新字段，发布时必须先确认该字段已经存在于当前 SQLite 实体表或 view 中，再上线 Dashboard 查询。

### ❌ Developer Admin 不做数据分析

`developer_admin.py` 只做 **Grafana 无法实现的功能**：

- **用户管理操作** - 设置开发者、拉黑用户、设置昵称
- **预设对话管理** - 添加/删除预设短语（需要写操作）
- **其他写操作** - 任何需要修改数据库的管理功能

### 开发流程

1. **新增分析需求** → 在 `grafana/provisioning/dashboards/*.json` 添加 Panel
2. **涉及 SQLite schema / view 变更** → 先修改迁移代码，再确认变更已实际落到 `./data/conversations.db`
3. **新增管理功能** → 在 `developer_admin.py` 添加 action handler
4. **验证与重启服务**:
   ```bash
   # 验证 SQLite 当前结构
   sqlite3 ./data/conversations.db "SELECT sql FROM sqlite_master WHERE type='view' AND name='your_view';"
   sqlite3 ./data/conversations.db "PRAGMA table_info(your_table);"

   # Grafana Dashboard 更新
   sudo docker compose -f docker-compose.monitoring.yml restart grafana

   # Admin / Importer / Metrics 等功能更新
   sudo docker compose -f docker-compose.monitoring.yml up -d --build service-name
   ```
