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
