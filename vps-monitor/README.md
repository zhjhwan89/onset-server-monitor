# VPS 卡片监控面板

这一目录用于集成现有的 VPS Monitor 项目，不是独立的完整 Web 项目。部署时需要保留原项目的依赖、数据库、其他模板和静态文件。

## 新版首页

`templates/dashboard.html` 提供响应式卡片首页，集中展示：

- 在线、离线和到期状态
- CPU、内存、Swap 与系统盘使用率
- 实时上传、下载及网卡累计流量
- 当前流量周期使用比例
- 监控端延迟、丢包率，以及指定地区的移动/联通/电信三网延迟
- 实际出口公网 IP
- Linux 发行版、版本、内核与架构

## 数据来源

在线状态、监控端延迟和丢包由监控容器主动检测。系统资源、公网 IP、网卡数据和三网延迟通过面板中保存的 SSH 账号从 VPS 只读采集。三网延迟采用三次 TCP 建连检测，并显示平均延迟和连接失败率，避免部分运营商目标禁止 ICMP 后产生误判。

实际出口 IP 优先通过 HTTPS 公网查询服务获得；查询失败时，页面回退显示面板中保存的监控地址。服务商后台的计费流量可能因统计方向、重置时间和单位不同而与网卡数据略有差异。

## Compose 挂载

在原项目的 `volumes` 中保留后端挂载，并增加首页模板：

```yaml
volumes:
  - ./app.py:/app/app.py:ro
  - ./templates/dashboard.html:/app/templates/dashboard.html:ro
  - ./templates/proxy_nodes.html:/app/templates/proxy_nodes.html:ro
  - ./templates/settings.html:/app/templates/settings.html:ro
  - ./data:/app/data
```

首页红框中的实时上传、实时下载、累计上传和累计下载，通过每台 VPS 各自的一条持久 SSH 通道约每 2 秒采样并自动更新。CPU、内存、Swap 和硬盘仍默认每 30 秒采样，避免给 VPS 增加不必要的负担。
累计上传和下载保留 5 位小数，避免低流量服务器长时间看起来完全不变。

可通过环境变量调整系统指标采集间隔，单位为秒，最小值为 30 秒：

```env
METRICS_CHECK_INTERVAL=30
```

实时网卡采样间隔也可调整，单位为秒，最小值为 2 秒：

```env
LIVE_NETWORK_INTERVAL=2
```

## 三网延迟地点

默认显示浙江移动、浙江联通、浙江电信。登录面板后打开“设置”，可直接修改显示地点和三个检测目标，不需要改代码或重装容器。检测目标格式为 `域名或IPv4:端口`。

默认浙江目标：

- 移动：`zj-cm-v4.ip.zstaticcdn.com:80`
- 联通：`zj-cu-v4.ip.zstaticcdn.com:80`
- 电信：`zj-ct-v4.ip.zstaticcdn.com:80`

三网结果随系统指标默认每 30 秒采集一次，页面会自动读取最新结果。更换地区后，保存设置并等待下一轮采集即可。

SOCKS5 落地列表默认显示地址和端口，账号与密码均以星号遮挡。管理员点击“显示”时，页面才通过已登录的后台临时读取真实账号和密码；复制完整地址和 CFnew 格式仍会复制完整认证信息。

部署前请备份 `app.py`、`docker-compose.yml`、模板和数据库。真实的 SSH 密码、Telegram Token、钉钉 Webhook 与数据库不得提交到 GitHub。
