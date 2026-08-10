# VPS 卡片监控面板

这一目录用于集成现有的 VPS Monitor 项目，不是独立的完整 Web 项目。部署时需要保留原项目的依赖、数据库、其他模板和静态文件。

## 新版首页

`templates/dashboard.html` 提供响应式卡片首页，集中展示：

- 在线、离线和到期状态
- CPU、内存、Swap 与系统盘使用率
- 实时上传、下载及网卡累计流量
- 当前流量周期使用比例
- 延迟与丢包率
- 实际出口公网 IP
- Linux 发行版、版本、内核与架构

## 数据来源

在线状态、延迟和丢包由监控容器主动检测。系统资源、公网 IP 和网卡数据通过面板中保存的 SSH 账号从 VPS 只读采集。

实际出口 IP 优先通过 HTTPS 公网查询服务获得；查询失败时，页面回退显示面板中保存的监控地址。服务商后台的计费流量可能因统计方向、重置时间和单位不同而与网卡数据略有差异。

## Compose 挂载

在原项目的 `volumes` 中保留后端挂载，并增加首页模板：

```yaml
volumes:
  - ./app.py:/app/app.py:ro
  - ./templates/dashboard.html:/app/templates/dashboard.html:ro
  - ./data:/app/data
```

可通过环境变量调整系统指标采集间隔，单位为秒，最小值为 30 秒：

```env
METRICS_CHECK_INTERVAL=60
```

部署前请备份 `app.py`、`docker-compose.yml`、模板和数据库。真实的 SSH 密码、Telegram Token、钉钉 Webhook 与数据库不得提交到 GitHub。
