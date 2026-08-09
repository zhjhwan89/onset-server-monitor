# Onset Server Monitor

一套面向家庭服务器、小型 Proxmox VE 环境和个人 VPS 的轻量监控方案。它把 VPS、PVE 虚拟机、Docker 容器与外贸网站询盘集中到 Telegram 菜单，并将重要报警同步到钉钉，方便在国内移动网络下及时接收。

## 功能

- Telegram 交互菜单：总览、VPS、到期、流量、PVE、虚拟机、Docker 和异常节点
- 钉钉双通道报警：主机、PVE、虚拟机、Docker、VPS、流量及代理服务状态
- PVE 只读 API 采集：节点资源、虚拟机配置、启动/关机、新增/删除和配置变化
- Docker 分虚拟机展示：容器新增、删除、异常与恢复通知
- Debian/Python Docker Agent 与 OpenWrt/BusyBox 轻量 Agent
- Contact Form 7 询盘同步到 Telegram 和钉钉
- 默认 30 秒检测，状态变化才发送通知，避免消息轰炸

## 目录

| 目录 | 作用 |
| --- | --- |
| `telegram-bot/` | Telegram Long Polling 菜单机器人 |
| `host-monitor/` | Debian 主机、PVE、Docker 汇总与钉钉通知 |
| `docker-agent/` | 普通 Linux 虚拟机的 Docker Agent |
| `openwrt-agent/` | OpenWrt/ImmortalWrt 轻量 Agent |
| `vps-monitor/` | VPS 监控项目的钉钉双通道集成文件 |
| `wordpress-plugin/` | FPCFAB Contact Form 7 询盘通知插件 |
| `docs/` | 部署与安全说明 |

## 快速开始

1. 复制所有 `*.env.example` 为对应的 `.env` 文件。
2. 填写自己的 Telegram、钉钉和 PVE 只读凭据。
3. 先部署 `host-monitor/`，再为各虚拟机部署 Agent。
4. 部署 Telegram Bot，并只允许自己的 Chat ID。
5. 根据需要安装 WordPress 插件或集成现有 VPS 监控面板。

详细步骤见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)。

## 安全

- 仓库不包含任何真实 Token、Webhook、Chat ID、PVE 密钥、数据库或内网地址。
- PVE 账号应只授予 `PVEAuditor` 等只读权限。
- `.env` 文件权限建议设为 `600`，不要提交到 Git。
- Docker Socket 具有较高权限，Agent 仅应运行在可信局域网中。
- Telegram 和钉钉任一通道故障都不会阻止另一通道发送。

## 说明

这是从真实家庭服务器环境整理出的可复用版本。不同系统、网络和 Compose 布局可能需要调整路径。部署前请先备份现有配置和数据库。
