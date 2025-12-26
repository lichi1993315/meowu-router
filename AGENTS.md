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
