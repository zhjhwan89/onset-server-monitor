import json
import http.client
import os
import sqlite3
import ssl
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode


TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])
MONITOR_DB = Path(os.getenv("MONITOR_DB", "/monitor-data/vps-monitor.db"))
HOST_STATUS_PATH = Path(os.getenv("HOST_STATUS_PATH", "/host-monitor/status.json"))
API_URL = f"https://api.telegram.org/bot{TOKEN}/"
CHINA_TZ = timezone(timedelta(hours=8))

BOT_COMMANDS = [
    {"command": "start", "description": "打开监控主菜单"},
    {"command": "status", "description": "查看全部状态"},
    {"command": "vps", "description": "查看 VPS 列表"},
    {"command": "expiry", "description": "查看即将到期"},
    {"command": "traffic", "description": "查看流量情况"},
    {"command": "pve", "description": "查看 PVE 主机"},
    {"command": "vms", "description": "查看虚拟机"},
    {"command": "docker", "description": "查看各虚拟机容器"},
    {"command": "alerts", "description": "查看异常节点"},
    {"command": "id", "description": "查看 Telegram Chat ID"},
]

SSL_CONTEXT = ssl.create_default_context()
if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
    SSL_CONTEXT.options |= ssl.OP_IGNORE_UNEXPECTED_EOF

MENU = {
    "inline_keyboard": [
        [
            {"text": "📊 全部状态", "callback_data": "summary"},
            {"text": "🖥 VPS列表", "callback_data": "vps"},
        ],
        [
            {"text": "📅 即将到期", "callback_data": "expiry"},
            {"text": "📈 流量情况", "callback_data": "traffic"},
        ],
        [
            {"text": "🏠 PVE主机", "callback_data": "host"},
            {"text": "🧩 虚拟机", "callback_data": "vms"},
        ],
        [
            {"text": "🐳 Docker容器", "callback_data": "docker"},
            {"text": "⚠️ 异常节点", "callback_data": "problems"},
        ],
        [
            {"text": "🔄 刷新", "callback_data": "summary"},
        ],
    ]
}


class TelegramClient:
    def __init__(self):
        self.connection = None

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
        self.connection = None

    def call(self, method, data=None):
        body = urlencode(data or {}).encode("utf-8")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
            "Connection": "keep-alive",
        }
        last_error = None
        for attempt in range(2):
            try:
                if self.connection is None:
                    self.connection = http.client.HTTPSConnection(
                        "api.telegram.org",
                        timeout=30,
                        context=SSL_CONTEXT,
                    )
                self.connection.request(
                    "POST",
                    f"/bot{TOKEN}/{method}",
                    body=body,
                    headers=headers,
                )
                response = self.connection.getresponse()
                raw = response.read()
                will_close = response.will_close
                status = response.status
                if will_close:
                    self.close()
                if status != 200:
                    detail = raw.decode("utf-8", errors="replace")
                    raise RuntimeError(f"Telegram HTTP {status}: {detail[:300]}")
                payload = json.loads(raw)
                if not payload.get("ok"):
                    raise RuntimeError(payload.get("description") or "Telegram API 调用失败")
                return payload.get("result")
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
                self.close()
                if attempt == 0:
                    continue
        raise RuntimeError(f"Telegram 连接失败：{last_error}")


TELEGRAM = TelegramClient()


def call_api(method, data=None):
    return TELEGRAM.call(method, data)


def menu_json():
    return json.dumps(MENU, ensure_ascii=False, separators=(",", ":"))


def send_menu(chat_id, text, reply_markup=None):
    return call_api(
        "sendMessage",
        {"chat_id": chat_id, "text": text, "reply_markup": reply_markup or menu_json()},
    )


def register_commands():
    call_api(
        "setMyCommands",
        {"commands": json.dumps(BOT_COMMANDS, ensure_ascii=False, separators=(",", ":"))},
    )
    call_api(
        "setChatMenuButton",
        {
            "chat_id": ALLOWED_CHAT_ID,
            "menu_button": json.dumps({"type": "commands"}, separators=(",", ":")),
        },
    )


def edit_menu(chat_id, message_id, text, reply_markup=None):
    try:
        return call_api(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup or menu_json(),
            },
        )
    except RuntimeError as exc:
        if "message is not modified" not in str(exc):
            raise
        return None


@contextmanager
def connect_db():
    connection = sqlite3.connect(
        f"file:{MONITOR_DB.as_posix()}?mode=ro",
        uri=True,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def money(cents, currency):
    symbols = {"CNY": "¥", "USD": "$", "EUR": "€", "HKD": "HK$", "JPY": "¥", "GBP": "£"}
    return f"{currency} {symbols.get(currency, '')}{int(cents or 0) / 100:.2f}"


def trim(text, limit=3900):
    return text if len(text) <= limit else text[: limit - 12] + "\n……内容过长"


def database_error(exc):
    return trim(f"❌ 暂时无法读取 VPS 监控数据\n原因：{str(exc)[:240]}")


def render_summary():
    try:
        with connect_db() as conn:
            vps = conn.execute(
                "SELECT status, expiry_date, sui_enabled, sui_error FROM vps"
            ).fetchall()
            proxies = conn.execute(
                "SELECT enabled, status FROM proxy_endpoints"
            ).fetchall()
            panels = conn.execute(
                "SELECT enabled, status FROM cloudflare_panels"
            ).fetchall()
        today = datetime.now(CHINA_TZ).date()
        online = sum(row["status"] == "online" for row in vps)
        offline = sum(row["status"] == "offline" for row in vps)
        unknown = len(vps) - online - offline
        due = 0
        for row in vps:
            try:
                due += (datetime.fromisoformat(row["expiry_date"]).date() - today).days <= 7
            except (TypeError, ValueError):
                pass
        sui_errors = sum(bool(row["sui_enabled"] and row["sui_error"]) for row in vps)
        proxy_errors = sum(bool(row["enabled"] and row["status"] == "offline") for row in proxies)
        panel_errors = sum(bool(row["enabled"] and row["status"] == "offline") for row in panels)
        problems = offline + sui_errors + proxy_errors + panel_errors
        return (
            "📊 VPS监控总览\n\n"
            f"VPS：{online} 在线 / {offline} 离线 / {unknown} 待检测\n"
            f"7天内到期：{due} 台\n"
            f"S-UI异常：{sui_errors} 个\n"
            f"SOCKS5异常：{proxy_errors} 个\n"
            f"Cloudflare异常：{panel_errors} 个\n\n"
            f"当前结论：{'✅ 全部正常' if problems == 0 else f'⚠️ 共发现 {problems} 项异常'}\n"
            f"更新时间：{datetime.now(CHINA_TZ):%Y-%m-%d %H:%M:%S}"
        )
    except (OSError, sqlite3.Error) as exc:
        return database_error(exc)


def render_vps():
    try:
        with connect_db() as conn:
            rows = conn.execute(
                "SELECT name, status, latency_ms, provider FROM vps "
                "ORDER BY CASE status WHEN 'offline' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END, name"
            ).fetchall()
        if not rows:
            return "🖥 VPS列表\n\n暂无 VPS 记录"
        icons = {"online": "🟢", "offline": "🔴", "unknown": "⚪"}
        lines = ["🖥 VPS列表", ""]
        for row in rows:
            latency = f" · {row['latency_ms']:.0f}ms" if row["latency_ms"] is not None else ""
            provider = f" · {row['provider']}" if row["provider"] else ""
            lines.append(f"{icons.get(row['status'], '⚪')} {row['name']}{provider}{latency}")
        lines.extend(["", f"共 {len(rows)} 台"])
        return trim("\n".join(lines))
    except (OSError, sqlite3.Error) as exc:
        return database_error(exc)


def render_expiry():
    try:
        with connect_db() as conn:
            rows = conn.execute(
                "SELECT name, provider, expiry_date, renewal_amount_cents, currency "
                "FROM vps ORDER BY expiry_date, name"
            ).fetchall()
        today = datetime.now(CHINA_TZ).date()
        items = []
        for row in rows:
            try:
                expiry = datetime.fromisoformat(row["expiry_date"]).date()
            except (TypeError, ValueError):
                continue
            days = (expiry - today).days
            if days <= 30:
                if days < 0:
                    label = f"已过期 {-days} 天"
                elif days == 0:
                    label = "今天到期"
                else:
                    label = f"还有 {days} 天"
                items.append((days, row, expiry, label))
        if not items:
            return "📅 即将到期\n\n未来30天没有到期的 VPS"
        lines = ["📅 30天内到期", ""]
        for _, row, expiry, label in items:
            icon = "🔴" if label.startswith("已过期") or label == "今天到期" else "🟠"
            lines.append(f"{icon} {row['name']}｜{label}")
            lines.append(f"   {expiry.isoformat()} · {money(row['renewal_amount_cents'], row['currency'])}")
        return trim("\n".join(lines))
    except (OSError, sqlite3.Error) as exc:
        return database_error(exc)


def render_traffic():
    try:
        with connect_db() as conn:
            rows = conn.execute(
                "SELECT name, traffic_used_bytes, traffic_total_gb, traffic_error "
                "FROM vps WHERE traffic_auto=1 ORDER BY name"
            ).fetchall()
        if not rows:
            return "📈 流量情况\n\n没有启用 SSH 自动流量监控的 VPS"
        items = []
        for row in rows:
            used = int(row["traffic_used_bytes"] or 0) / 1_000_000_000
            total = float(row["traffic_total_gb"] or 0)
            percent = used / total * 100 if total > 0 else 0
            items.append((percent, row, used, total))
        items.sort(key=lambda item: item[0], reverse=True)
        lines = ["📈 VPS流量情况", ""]
        for percent, row, used, total in items:
            icon = "🔴" if percent >= 95 else ("🟠" if percent >= 80 else "🟢")
            if row["traffic_error"]:
                lines.append(f"⚠️ {row['name']}｜采集失败")
                lines.append(f"   {str(row['traffic_error'])[:100]}")
            else:
                lines.append(f"{icon} {row['name']}｜{percent:.1f}%")
                lines.append(f"   {used:.2f} GB / {total:.2f} GB")
        return trim("\n".join(lines))
    except (OSError, sqlite3.Error) as exc:
        return database_error(exc)


def render_problems():
    try:
        with connect_db() as conn:
            vps = conn.execute(
                "SELECT name, status, sui_enabled, sui_error, traffic_error FROM vps"
            ).fetchall()
            proxies = conn.execute(
                "SELECT name, status, error FROM proxy_endpoints WHERE enabled=1"
            ).fetchall()
            panels = conn.execute(
                "SELECT name, status, error FROM cloudflare_panels WHERE enabled=1"
            ).fetchall()
        lines = ["⚠️ 异常节点", ""]
        count = 0
        for row in vps:
            if row["status"] == "offline":
                lines.append(f"🔴 VPS离线：{row['name']}")
                count += 1
            if row["sui_enabled"] and row["sui_error"]:
                lines.append(f"🔴 S-UI异常：{row['name']}｜{str(row['sui_error'])[:100]}")
                count += 1
            if row["traffic_error"]:
                lines.append(f"🟠 流量采集：{row['name']}｜{str(row['traffic_error'])[:100]}")
                count += 1
        for row in proxies:
            if row["status"] == "offline":
                lines.append(f"🔴 SOCKS5异常：{row['name']}｜{str(row['error'] or '')[:100]}")
                count += 1
        for row in panels:
            if row["status"] == "offline":
                lines.append(f"🔴 Cloudflare异常：{row['name']}｜{str(row['error'] or '')[:100]}")
                count += 1
        if count == 0:
            lines.append("✅ 当前没有发现异常")
        else:
            lines.extend(["", f"共 {count} 项异常"])
        return trim("\n".join(lines))
    except (OSError, sqlite3.Error) as exc:
        return database_error(exc)


def format_uptime(seconds):
    seconds = int(seconds or 0)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return f"{days}天 {hours}小时 {minutes}分钟"


def format_bytes(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024


def load_host_status():
    return json.loads(HOST_STATUS_PATH.read_text(encoding="utf-8"))


def docker_menu_json(data=None):
    if data is None:
        try:
            data = load_host_status()
        except (OSError, ValueError, TypeError):
            data = {}
    vms = (data.get("pve") or {}).get("vms") or []
    rows = []
    for index in range(0, len(vms), 2):
        row = []
        for vm in vms[index:index + 2]:
            icon = "🐳" if vm.get("status") == "running" else "⚫"
            row.append({"text": f"{icon} {vm.get('vmid')} {vm.get('name')}", "callback_data": f"docker_vm:{vm.get('vmid')}"})
        rows.append(row)
    rows.append([{"text": "⬅️ 返回主菜单", "callback_data": "summary"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False, separators=(",", ":"))


def stale_warning(data):
    try:
        updated = datetime.fromisoformat(data.get("updated_at", ""))
        age = (datetime.now(CHINA_TZ) - updated.astimezone(CHINA_TZ)).total_seconds()
    except (TypeError, ValueError):
        age = 999999
    return age > 120


def render_host():
    try:
        data = load_host_status()
    except (OSError, ValueError, TypeError) as exc:
        return trim(f"🏠 PVE小主机\n\n⚪ 尚未取得监控数据\n原因：{str(exc)[:180]}")
    pve = data.get("pve") or {}
    nodes = pve.get("nodes") or []
    issues = data.get("issues") or []
    lines = ["🏠 PVE小主机", ""]
    if pve.get("error"):
        lines.append(f"🔴 PVE读取失败：{pve['error']}")
    for node in nodes:
        mem_total = node.get("memory_total_bytes") or 0
        mem_pct = (node.get("memory_used_bytes") or 0) / mem_total * 100 if mem_total else 0
        disk_total = node.get("root_total_bytes") or 0
        disk_pct = (node.get("root_used_bytes") or 0) / disk_total * 100 if disk_total else 0
        lines.extend([
            f"{'🟢' if node.get('status') == 'online' else '🔴'} 节点：{node.get('node')}｜{node.get('status')}",
            f"CPU：{node.get('cpu_percent', 0)}%（{node.get('cpu_count', 0)}核）",
            f"型号：{node.get('cpu_model') or '未知'}",
            f"内存：{mem_pct:.1f}%（{format_bytes(mem_total)}）",
            f"系统盘：{disk_pct:.1f}%（{format_bytes(disk_total)}）",
            f"运行时间：{format_uptime(node.get('uptime_seconds'))}",
            f"PVE：{node.get('pve_version') or '未知'}",
        ])
    system = data.get("system") or {}
    lines.extend([
        "",
        "监控机（Debian13-Tools）：",
        f"CPU {data.get('cpu_percent', 0)}%｜内存 {data.get('memory_percent', 0)}%｜磁盘 {data.get('disk_percent', 0)}%",
        f"系统：{system.get('os') or '未知'}｜{system.get('cpu_count', 0)}核｜{format_bytes(system.get('memory_total_bytes'))}",
    ])
    lines.extend(["", f"当前结论：{'✅ 全部正常' if not issues else f'⚠️ {len(issues)} 项异常'}"])
    if stale_warning(data):
        lines.append("🔴 监控数据超过2分钟未更新")
    lines.append(f"更新时间：{data.get('updated_at', '未知')}")
    return trim("\n".join(lines))


def render_vms():
    try:
        data = load_host_status()
    except (OSError, ValueError, TypeError) as exc:
        return trim(f"🧩 PVE虚拟机\n\n⚪ 尚未取得数据\n原因：{str(exc)[:180]}")
    pve = data.get("pve") or {}
    if pve.get("error"):
        return trim(f"🧩 PVE虚拟机\n\n🔴 读取失败：{pve['error']}")
    vms = pve.get("vms") or []
    if not vms:
        return "🧩 PVE虚拟机\n\n暂无虚拟机"
    running = sum(vm.get("status") == "running" for vm in vms)
    lines = ["🧩 PVE虚拟机", "", f"共 {len(vms)} 台｜{running} 运行 / {len(vms) - running} 关机", ""]
    for vm in vms:
        icon = "🟢" if vm.get("status") == "running" else "⚫"
        mem_total = vm.get("memory_total_bytes") or 0
        mem_pct = (vm.get("memory_used_bytes") or 0) / mem_total * 100 if mem_total else 0
        lines.append(f"{icon} {vm.get('vmid')} {vm.get('name')}｜{vm.get('status')}")
        lines.append(f"   {vm.get('cpu_count', 0)}核｜{format_bytes(mem_total)}内存｜{format_bytes(vm.get('disk_total_bytes'))}磁盘")
        if vm.get("status") == "running":
            lines.append(f"   CPU {vm.get('cpu_percent', 0)}%｜内存 {mem_pct:.1f}%｜运行 {format_uptime(vm.get('uptime_seconds'))}")
        lines.append(f"   开机自启：{'是' if vm.get('onboot') else '否'}｜磁盘 {len(vm.get('disks') or [])} 块｜网卡 {len(vm.get('networks') or [])} 个")
    if stale_warning(data):
        lines.extend(["", "🔴 监控数据超过2分钟未更新"])
    lines.append(f"\n更新时间：{data.get('updated_at', '未知')}")
    return trim("\n".join(lines))


def render_docker(vmid=None):
    try:
        data = load_host_status()
    except (OSError, ValueError, TypeError) as exc:
        return trim(f"🐳 Docker容器\n\n⚪ 尚未取得数据\n原因：{str(exc)[:180]}")
    pve_vms = (data.get("pve") or {}).get("vms") or []
    docker_nodes = data.get("docker_nodes") or [{"vmid": 101, "vm_name": "Debian13-Tools", "containers": data.get("containers") or [], "error": data.get("docker_error")}]
    if vmid is None:
        lines = ["🐳 虚拟机 Docker容器", "", "请选择下面的一台虚拟机，只显示该虚拟机内部的容器：", ""]
        node_map = {int(node.get("vmid") or 0): node for node in docker_nodes}
        for vm in pve_vms:
            node = node_map.get(int(vm.get("vmid") or 0))
            if vm.get("status") != "running":
                label = "虚拟机已关机"
            elif node is None:
                label = "Docker监控端未接入"
            elif node.get("docker_available") is False:
                label = "未安装Docker"
            elif node.get("error"):
                label = f"监控异常：{node['error']}"
            else:
                containers = node.get("containers") or []
                label = f"{len(containers)} 个容器"
            lines.append(f"• {vm.get('vmid')} {vm.get('name')}｜{label}")
        return trim("\n".join(lines))
    try:
        vmid = int(vmid)
    except (TypeError, ValueError):
        return "🐳 Docker容器\n\n虚拟机编号无效"
    vm = next((item for item in pve_vms if int(item.get("vmid") or 0) == vmid), None)
    node = next((item for item in docker_nodes if int(item.get("vmid") or 0) == vmid), None)
    vm_name = (vm or {}).get("name") or (node or {}).get("vm_name") or "未知虚拟机"
    if vm and vm.get("status") != "running":
        return f"🐳 {vmid} {vm_name} Docker容器\n\n⚫ 虚拟机当前已关机，无法读取内部容器。"
    if node is None:
        return f"🐳 {vmid} {vm_name} Docker容器\n\n⚪ 这台虚拟机尚未安装 Docker 监控端。"
    if node.get("docker_available") is False:
        return f"🐳 {vmid} {vm_name} Docker容器\n\n⚪ 当前没有安装Docker。监控端仍会每30秒检查，将来安装Docker或新增容器会自动识别。"
    if node.get("error"):
        return trim(f"🐳 {vmid} {vm_name} Docker容器\n\n🔴 监控异常：{node['error']}")
    containers = node.get("containers") or []
    lines = [f"🐳 {vmid} {vm_name} Docker容器", "", f"共 {len(containers)} 个｜{sum(item.get('condition') == 'ok' for item in containers)} 正常 / {sum(item.get('condition') != 'ok' for item in containers)} 异常", ""]
    for item in containers:
        icon = "🟢" if item.get("condition") == "ok" else "🔴"
        lines.append(f"{icon} {item.get('name')}｜{item.get('state')}")
        lines.append(f"   镜像：{item.get('image') or '未知'}")
        if item.get("ports"):
            lines.append(f"   端口：{item['ports']}")
        lines.append(f"   {item.get('status') or item.get('condition')}")
    if not containers: lines.append("暂无容器，或者这台虚拟机没有安装 Docker。")
    if stale_warning(data):
        lines.extend(["", "🔴 监控数据超过2分钟未更新"])
    lines.append(f"\n更新时间：{data.get('updated_at', '未知')}")
    return trim("\n".join(lines))


RENDERERS = {
    "summary": render_summary,
    "vps": render_vps,
    "expiry": render_expiry,
    "traffic": render_traffic,
    "problems": render_problems,
    "host": render_host,
    "vms": render_vms,
    "docker": render_docker,
}


def authorized(chat_id):
    try:
        return int(chat_id) == ALLOWED_CHAT_ID
    except (TypeError, ValueError):
        return False


def handle_message(message):
    chat_id = message.get("chat", {}).get("id")
    if not authorized(chat_id):
        if chat_id is not None:
            call_api("sendMessage", {"chat_id": chat_id, "text": "无权使用这个机器人。"})
        return
    text = (message.get("text") or "").strip()
    command = text.split(None, 1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
    if command == "/id":
        call_api("sendMessage", {"chat_id": chat_id, "text": f"你的 Telegram Chat ID：{chat_id}"})
    elif command in {"/start", "/menu", "/status", "/refresh"} or text == "菜单":
        send_menu(chat_id, render_summary())
    elif command == "/vps":
        send_menu(chat_id, render_vps())
    elif command == "/expiry":
        send_menu(chat_id, render_expiry())
    elif command == "/traffic":
        send_menu(chat_id, render_traffic())
    elif command in {"/pve", "/host"}:
        send_menu(chat_id, render_host())
    elif command == "/vms":
        send_menu(chat_id, render_vms())
    elif command == "/docker":
        data = load_host_status()
        send_menu(chat_id, render_docker(), docker_menu_json(data))
    elif command in {"/alerts", "/problems"}:
        send_menu(chat_id, render_problems())
    else:
        send_menu(chat_id, "请选择下面的按钮查看监控信息。")


def handle_callback(query):
    query_id = query.get("id")
    from_id = query.get("from", {}).get("id")
    message = query.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    if not authorized(from_id) or not authorized(chat_id):
        if query_id:
            call_api("answerCallbackQuery", {"callback_query_id": query_id, "text": "无权使用"})
        return
    action = query.get("data") or "summary"
    if action == "docker":
        edit_menu(chat_id, message.get("message_id"), render_docker(), docker_menu_json())
    elif action.startswith("docker_vm:"):
        vmid = action.split(":", 1)[1]
        edit_menu(chat_id, message.get("message_id"), render_docker(vmid), docker_menu_json())
    else:
        renderer = RENDERERS.get(action, render_summary)
        edit_menu(chat_id, message.get("message_id"), renderer())
    if query_id:
        call_api("answerCallbackQuery", {"callback_query_id": query_id})


def main():
    offset = 0
    try:
        register_commands()
        print("Telegram 快捷命令已注册", flush=True)
    except RuntimeError as exc:
        print(f"快捷命令注册失败，将在下次重启时重试：{exc}", flush=True)
    print("机器人菜单已启动，正在等待消息……", flush=True)
    while True:
        try:
            updates = call_api(
                "getUpdates",
                {
                    "timeout": 20,
                    "offset": offset,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                },
            )
            for update in updates or []:
                offset = update["update_id"] + 1
                if update.get("callback_query"):
                    handle_callback(update["callback_query"])
                elif update.get("message"):
                    handle_message(update["message"])
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            print(f"运行出错：{exc}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
