# 部署说明

## 1. 准备通知渠道

Telegram Bot 配置：

```env
BOT_TOKEN=
ALLOWED_CHAT_ID=
```

钉钉自定义机器人配置：

```env
DINGTALK_WEBHOOK=
DINGTALK_SECRET=
```

两个文件都应设置为仅管理员可读：

```bash
chmod 600 .env dingtalk.env
```

## 2. PVE 只读接口

在 PVE 创建独立只读用户和 API Token，并只授予审计权限。复制 `host-monitor/pve.env.example` 为 `pve.env`，填写 PVE 地址、Token ID 与 Token Secret。

不要使用 PVE root 密码，也不要把 Token Secret 提交到仓库。

## 3. 主机监控

把 `host-monitor/` 部署到 Debian 监控节点的 `/opt/onset-host-monitor`，然后运行：

```bash
sh /opt/onset-host-monitor/install.sh
```

服务包括：

- `onset-host-monitor.service`：主机、PVE 和容器状态采集
- `onset-docker-collector.service`：接收虚拟机 Agent 报告

## 4. Docker Agent

普通 Linux 虚拟机使用 `docker-agent/`。复制 `agent.env.example` 为 `agent.env`，设置收集器地址、共享密钥、VM ID 和名称，然后运行：

```bash
docker compose up -d --build
```

OpenWrt/ImmortalWrt 使用 `openwrt-agent/` 中的脚本和 init 文件。它会在尚未安装 Docker 时继续上报，并在未来安装 Docker 后自动识别容器。

## 5. Telegram Bot

将 `telegram-bot/bot.py` 放入自己的 Bot 容器或 Python 运行环境，并挂载 VPS 数据库和 `host-monitor/status.json`。机器人使用 Long Polling，不需要开放公网入站端口。

## 6. VPS 与 WordPress

`vps-monitor/app.py` 是现有 VPS 监控项目的钉钉双通道集成文件，不是独立完整 Web 项目。部署时需保留原项目的模板、依赖与数据库。

`wordpress-plugin/` 可打包为 ZIP 后，通过 WordPress 后台上传。插件会在 Contact Form 7 成功提交后，把询盘摘要发送到 Telegram 和钉钉。

## 7. 建议

- 先在测试群验证通知，再启用正式报警。
- 对配置文件、数据库和照片资料分别做备份。
- 不要把监控系统当作照片、数据库或业务资料的唯一备份。
