# 项目迁移文档（Docker 方式）

本文档用于将当前项目从一台服务器迁移到另一台服务器，采用 `Docker Compose` 方式迁移，尽量保持运行环境一致。

适用编排文件：`docker-compose.monitoring.yml`

当前涉及的主要服务：

- `router-api`（9000）
- `metrics-exporter`（9090）
- `prometheus`（9091）
- `grafana`（9001）
- `renderer`（9002）
- `grafana-report`
- `developer-admin`（9095）

## 1. 迁移策略说明

建议优先使用以下两种方式之一：

1. 完整迁移（推荐生产）
   - 迁移代码、`.env`、`data/`、`output/`
   - 迁移 Docker named volumes：`prometheus_data`、`grafana_data`
   - 保留 Grafana 配置/用户/Token、Prometheus 历史指标

2. 轻量迁移（快速）
   - 迁移代码、`.env`、`data/`、`output/`
   - 不迁移 named volumes（Prometheus/Grafana 状态重新生成）
   - 适合测试环境或不需要历史指标时

## 2. 迁移前准备（源服务器）

在源服务器确认项目目录（示例）：

```bash
cd /home/jesse/develop/router
```

建议先记录当前运行状态：

```bash
sudo docker compose -f docker-compose.monitoring.yml ps
sudo docker compose -f docker-compose.monitoring.yml images
```

## 3. 需要迁移的数据清单

必须迁移（业务运行依赖）：

- 项目代码目录（整个仓库）
- `.env`（API Key / Grafana Token / 飞书配置等）
- `data/`（例如 `conversations.db`）
- `output/`（日志/输出文件，按需要保留）

建议迁移（保留监控历史与 Grafana 状态）：

- Docker volume `prometheus_data`
- Docker volume `grafana_data`

说明：

- `grafana/provisioning/` 和 dashboard JSON 已在代码仓库内，跟随代码迁移即可。
- `grafana_data` 中包含 Grafana 运行时数据（账号、配置、插件等）。
- `prometheus_data` 中包含历史指标数据，体积可能较大。

## 4. 源服务器导出（推荐冷迁移）

为避免 SQLite 与时序数据在写入过程中被拷贝，建议先停服务再打包。

```bash
cd /home/jesse/develop/router
sudo docker compose -f docker-compose.monitoring.yml down
```

### 4.1 打包项目目录（代码 + data + output）

在项目上级目录执行（避免 tar 包含自身）：

```bash
cd /home/jesse/develop
tar --exclude='.git' -czf router-project.tar.gz router
```

如果需要保留 Git 历史，去掉 `--exclude='.git'`。

### 4.2 导出 Docker named volumes（完整迁移需要）

先确认卷名（通常为 `<目录名>_prometheus_data`、`<目录名>_grafana_data`）：

```bash
sudo docker volume ls | grep router
```

导出 Prometheus 数据卷（把实际卷名替换进去）：

```bash
sudo docker run --rm \
  -v router_prometheus_data:/from \
  -v "$(pwd)":/backup \
  alpine sh -c 'cd /from && tar -czf /backup/prometheus_data.tar.gz .'
```

导出 Grafana 数据卷（把实际卷名替换进去）：

```bash
sudo docker run --rm \
  -v router_grafana_data:/from \
  -v "$(pwd)":/backup \
  alpine sh -c 'cd /from && tar -czf /backup/grafana_data.tar.gz .'
```

如果卷名前缀不是 `router_`，以 `docker volume ls` 的实际结果为准。

## 5. 传输到目标服务器

将以下文件传到目标服务器（示例）：

- `router-project.tar.gz`
- `prometheus_data.tar.gz`（完整迁移时）
- `grafana_data.tar.gz`（完整迁移时）

示例（在源服务器执行）：

```bash
scp router-project.tar.gz user@TARGET_HOST:/home/user/
scp prometheus_data.tar.gz grafana_data.tar.gz user@TARGET_HOST:/home/user/
```

## 6. 目标服务器准备

### 6.1 安装 Docker / Docker Compose

确保目标服务器已安装 Docker，并可执行：

```bash
sudo docker version
sudo docker compose version
```

### 6.2 解压项目

```bash
cd /home/user
tar -xzf router-project.tar.gz
cd router
```

### 6.3 检查并修改环境配置

重点检查：

- `.env` 中的所有密钥是否完整
- `docker-compose.monitoring.yml` 中 `grafana-report` 的 `GRAFANA_URL`

当前配置里 `GRAFANA_URL` 是固定 IP（示例：`http://20.198.242.101:9001/`），迁移后必须改成目标服务器地址：

```yaml
GRAFANA_URL=http://<目标服务器IP或域名>:9001/
```

如目标服务器端口被占用，也需要同步调整 `ports` 映射。

## 7. 恢复 Docker named volumes（完整迁移）

如果你选择“轻量迁移”，可跳过本节，直接进入启动步骤。

### 7.1 预创建卷

在目标服务器执行：

```bash
sudo docker volume create router_prometheus_data
sudo docker volume create router_grafana_data
```

如果实际 Compose 项目名不是 `router`，卷名需与目标环境一致。也可以先启动一次再用 `docker volume ls` 查看实际卷名。

### 7.2 导入 Prometheus 卷数据

在存放 `prometheus_data.tar.gz` 的目录执行：

```bash
sudo docker run --rm \
  -v router_prometheus_data:/to \
  -v "$(pwd)":/backup \
  alpine sh -c 'cd /to && tar -xzf /backup/prometheus_data.tar.gz'
```

### 7.3 导入 Grafana 卷数据

```bash
sudo docker run --rm \
  -v router_grafana_data:/to \
  -v "$(pwd)":/backup \
  alpine sh -c 'cd /to && tar -xzf /backup/grafana_data.tar.gz'
```

## 8. 启动服务（目标服务器）

在项目目录执行：

```bash
cd /home/user/router
sudo docker compose -f docker-compose.monitoring.yml up -d --build
```

查看状态：

```bash
sudo docker compose -f docker-compose.monitoring.yml ps
```

查看关键日志（建议优先检查）：

```bash
sudo docker compose -f docker-compose.monitoring.yml logs -f router-api
sudo docker compose -f docker-compose.monitoring.yml logs -f metrics-exporter
sudo docker compose -f docker-compose.monitoring.yml logs -f grafana
sudo docker compose -f docker-compose.monitoring.yml logs -f developer-admin
```

## 9. 迁移后验证清单

访问与功能验证：

- `http://<目标IP>:9000`：`router-api` 可访问
- `http://<目标IP>:9090/metrics`：指标暴露正常
- `http://<目标IP>:9091`：Prometheus 页面正常
- `http://<目标IP>:9001`：Grafana 可登录（默认可能是 `admin/admin`，若恢复了 `grafana_data` 则以原账号为准）
- `http://<目标IP>:9095`：Developer Admin 可访问

数据验证：

- `data/conversations.db` 存在且业务查询正常
- Grafana Dashboard 已加载（`grafana/provisioning/dashboards/*.json`）
- 若恢复了 `prometheus_data`，Prometheus 中可看到历史时间序列

联动验证：

- `metrics-exporter` 能读取 `output/`、`data/`
- `grafana-report` 能访问 `GRAFANA_URL`，并使用 `.env` 中的 `GRAFANA_TOKEN`
- 飞书推送配置（如启用）正常

## 10. 切换与回滚建议

切换建议：

1. 先在目标服务器完成全部验证
2. 再切换域名 / 反向代理 / 访问入口到新服务器
3. 保留源服务器一段时间（只读或停机待命）

回滚方式：

1. 若新服务器异常，恢复流量到旧服务器
2. 在旧服务器执行：
   ```bash
   cd /home/jesse/develop/router
   sudo docker compose -f docker-compose.monitoring.yml up -d
   ```

## 11. 常见问题

### Q1：迁移后 Grafana 报表发送失败

优先检查：

- `docker-compose.monitoring.yml` 中 `GRAFANA_URL` 是否已改为新地址
- `.env` 中 `GRAFANA_TOKEN` 是否有效
- `renderer` 和 `grafana` 服务是否正常

### Q2：Developer Admin / Exporter 看不到数据

优先检查：

- `./data:/app/data` 挂载是否存在
- `data/conversations.db` 是否已迁移
- 容器内环境变量 `DB_PATH=/app/data/conversations.db` 是否生效

### Q3：Prometheus/Grafana 历史数据丢失

原因通常是未迁移 named volumes（`prometheus_data`、`grafana_data`）或卷名不一致。

## 12. 推荐的最小迁移命令（速查）

源服务器：

```bash
cd /home/jesse/develop/router
sudo docker compose -f docker-compose.monitoring.yml down
cd /home/jesse/develop
tar --exclude='.git' -czf router-project.tar.gz router
```

目标服务器：

```bash
cd /home/user
tar -xzf router-project.tar.gz
cd router
sudo docker compose -f docker-compose.monitoring.yml up -d --build
```

如果需要完整保留监控历史，再额外迁移 `prometheus_data` 和 `grafana_data` 卷。

