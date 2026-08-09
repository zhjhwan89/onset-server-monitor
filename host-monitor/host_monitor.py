#!/usr/bin/env python3
import base64
import hashlib
import hmac
import http.client
import json
import os
import platform
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus, urlencode, urlsplit


BASE_DIR = Path(__file__).resolve().parent
STATUS_PATH = BASE_DIR / "status.json"
STATE_PATH = BASE_DIR / "state.json"
REPORTS_DIR = BASE_DIR / "docker-reports"
BOT_ENV_PATH = Path(os.getenv("BOT_ENV_PATH", "/opt/telegram-bot/.env"))
PVE_ENV_PATH = Path(os.getenv("PVE_ENV_PATH", str(BASE_DIR / "pve.env")))
DINGTALK_ENV_PATH = Path(os.getenv("DINGTALK_ENV_PATH", str(BASE_DIR / "dingtalk.env")))
CHECK_INTERVAL = max(30, int(os.getenv("CHECK_INTERVAL", "30")))
CPU_WARN = float(os.getenv("CPU_WARN", "90"))
MEMORY_WARN = float(os.getenv("MEMORY_WARN", "90"))
DISK_WARN = float(os.getenv("DISK_WARN", "80"))
DISK_CRITICAL = float(os.getenv("DISK_CRITICAL", "90"))
CHINA_TZ = timezone(timedelta(hours=8))
STOP = False


def now_text():
    return datetime.now(CHINA_TZ).isoformat(timespec="seconds")


def load_env(path):
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def save_json(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class TelegramSender:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.connection = None
        self.context = ssl.create_default_context()
        if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
            self.context.options |= ssl.OP_IGNORE_UNEXPECTED_EOF

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
        self.connection = None

    def send(self, text):
        if not self.token or not self.chat_id:
            print("Telegram 配置缺失，无法发送通知", flush=True)
            return False
        body = urlencode({"chat_id": self.chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body)), "Connection": "keep-alive"}
        for attempt in range(3):
            try:
                if self.connection is None:
                    self.connection = http.client.HTTPSConnection("api.telegram.org", timeout=15, context=self.context)
                self.connection.request("POST", f"/bot{self.token}/sendMessage", body=body, headers=headers)
                response = self.connection.getresponse()
                raw = response.read()
                if response.will_close:
                    self.close()
                payload = json.loads(raw)
                if response.status == 200 and payload.get("ok"):
                    return True
            except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
                print(f"Telegram 发送失败（第 {attempt + 1} 次）：{exc}", flush=True)
                self.close()
            time.sleep(2)
        return False


class DingTalkSender:
    def __init__(self, webhook, secret):
        self.webhook = webhook
        self.secret = secret
        self.context = ssl.create_default_context()

    def send(self, text):
        if not self.webhook or not self.secret:
            print("钉钉配置缺失，跳过钉钉通知", flush=True)
            return False
        timestamp = str(int(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        digest = hmac.new(self.secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
        signature = quote_plus(base64.b64encode(digest).decode("ascii"))
        separator = "&" if "?" in self.webhook else "?"
        parsed = urlsplit(f"{self.webhook}{separator}timestamp={timestamp}&sign={signature}")
        body = json.dumps({"msgtype": "text", "text": {"content": text}}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", "Content-Length": str(len(body))}
        try:
            connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=15, context=self.context)
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            if response.status == 200 and int(payload.get("errcode", -1)) == 0:
                return True
            print(f"钉钉发送失败：HTTP {response.status}，{payload}", flush=True)
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
            print(f"钉钉发送失败：{exc}", flush=True)
        return False


class MultiSender:
    def __init__(self, *senders):
        self.senders = senders

    def send(self, text):
        results = [sender.send(text) for sender in self.senders]
        return any(results)

    def close(self):
        for sender in self.senders:
            close = getattr(sender, "close", None)
            if close:
                close()


class PVEClient:
    def __init__(self, url, token_id, token_secret, verify_tls=False):
        parsed = urlsplit(url)
        self.host = parsed.hostname or ""
        self.port = parsed.port or 8006
        self.base_path = parsed.path.rstrip("/")
        self.token_id = token_id
        self.token_secret = token_secret
        self.context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()

    def get(self, path):
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=12, context=self.context)
        try:
            connection.request("GET", f"{self.base_path}/api2/json{path}", headers={"Authorization": f"PVEAPIToken={self.token_id}={self.token_secret}"})
            response = connection.getresponse()
            raw = response.read()
            if response.status != 200:
                raise RuntimeError(f"PVE HTTP {response.status}")
            return json.loads(raw).get("data")
        finally:
            connection.close()


def read_cpu_times():
    numbers = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
    return sum(numbers), idle


def cpu_percent():
    total_before, idle_before = read_cpu_times()
    time.sleep(1)
    total_after, idle_after = read_cpu_times()
    total_delta = max(total_after - total_before, 1)
    return round((1 - max(idle_after - idle_before, 0) / total_delta) * 100, 1)


def memory_info():
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.strip().split()[0]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    used = max(total - available, 0)
    return used, total, round(used / total * 100, 1) if total else 0.0


def temperature_c():
    temperatures = []
    for path in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            value = float(path.read_text().strip())
            temperatures.append(value / 1000 if value > 200 else value)
        except (OSError, ValueError):
            pass
    return round(max(temperatures), 1) if temperatures else None


def uptime_seconds():
    return int(float(Path("/proc/uptime").read_text().split()[0]))


def boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def system_info(total_memory, total_disk):
    cpu_model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    os_name = platform.platform()
    try:
        values = load_env(Path("/etc/os-release"))
        os_name = values.get("PRETTY_NAME", os_name)
    except OSError:
        pass
    try:
        virt = subprocess.run(["systemd-detect-virt"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        virt = "unknown"
    return {"hostname": socket.gethostname(), "os": os_name, "kernel": platform.release(), "architecture": platform.machine(), "virtualization": virt or "none", "cpu_model": cpu_model, "cpu_count": os.cpu_count() or 0, "memory_total_bytes": total_memory, "root_disk_total_bytes": total_disk}


def docker_containers():
    result = subprocess.run(["docker", "ps", "-a", "--format", "{{json .}}"], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Docker 无响应").strip()[:300])
    containers = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        status_text = item.get("Status") or ""
        lowered = status_text.lower()
        health = "unhealthy" if "(unhealthy)" in lowered else ("healthy" if "(healthy)" in lowered else ("starting" if "health: starting" in lowered else "none"))
        state = (item.get("State") or "unknown").lower()
        condition = "ok" if state == "running" and health != "unhealthy" else ("unhealthy" if health == "unhealthy" else state)
        containers.append({"name": item.get("Names") or "unknown", "image": item.get("Image") or "", "state": state, "health": health, "status": status_text, "ports": item.get("Ports") or "", "networks": item.get("Networks") or "", "condition": condition})
    return sorted(containers, key=lambda item: item["name"].lower())


def load_remote_docker_nodes():
    nodes = []
    try:
        paths = sorted(REPORTS_DIR.glob("*.json"))
    except OSError:
        return nodes
    now = time.time()
    for path in paths:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            normalized = []
            for item in report.get("containers") or []:
                status_text = item.get("status") or item.get("Status") or ""
                state = (item.get("state") or item.get("State") or "unknown").lower()
                lowered = status_text.lower()
                health = item.get("health") or ("unhealthy" if "(unhealthy)" in lowered else ("healthy" if "(healthy)" in lowered else "none"))
                condition = item.get("condition") or ("ok" if state == "running" and health != "unhealthy" else ("unhealthy" if health == "unhealthy" else state))
                normalized.append({"name": item.get("name") or item.get("Names") or "unknown", "image": item.get("image") or item.get("Image") or "", "state": state, "health": health, "status": status_text, "ports": item.get("ports") or item.get("Ports") or "", "condition": condition})
            report["containers"] = normalized
            age = max(0, now - path.stat().st_mtime)
            report["age_seconds"] = round(age)
            if age > max(CHECK_INTERVAL * 4, 120):
                report["error"] = f"监控端已 {round(age)} 秒未上报"
            nodes.append(report)
        except (OSError, ValueError, TypeError):
            continue
    return nodes


def human_config(vm):
    return f"{vm.get('cpu_count', 0)}核 / {round(vm.get('memory_total_bytes', 0) / 1073741824, 1)}GB内存 / {round(vm.get('disk_total_bytes', 0) / 1073741824, 1)}GB磁盘"


def pve_inventory(env):
    url = env.get("PVE_URL", "").rstrip("/")
    token_id = env.get("PVE_TOKEN_ID", "")
    token_secret = env.get("PVE_TOKEN_SECRET", "")
    if not url or not token_id or not token_secret:
        raise RuntimeError("PVE 只读接口尚未配置")
    client = PVEClient(url, token_id, token_secret, env.get("PVE_VERIFY_TLS", "false").lower() in {"1", "true", "yes"})
    resources = client.get("/cluster/resources?type=vm") or []
    nodes = client.get("/cluster/resources?type=node") or []
    node_details = []
    for node in nodes:
        name = node.get("node")
        detail = client.get(f"/nodes/{quote(str(name), safe='')}/status") or {}
        memory = detail.get("memory") or {}
        rootfs = detail.get("rootfs") or {}
        cpuinfo = detail.get("cpuinfo") or {}
        node_details.append({"node": name, "status": node.get("status") or "unknown", "cpu_percent": round(float(detail.get("cpu") or 0) * 100, 1), "cpu_count": int(cpuinfo.get("cpus") or node.get("maxcpu") or 0), "cpu_model": cpuinfo.get("model") or "", "memory_used_bytes": int(memory.get("used") or node.get("mem") or 0), "memory_total_bytes": int(memory.get("total") or node.get("maxmem") or 0), "root_used_bytes": int(rootfs.get("used") or node.get("disk") or 0), "root_total_bytes": int(rootfs.get("total") or node.get("maxdisk") or 0), "uptime_seconds": int(detail.get("uptime") or node.get("uptime") or 0), "kernel": detail.get("kversion") or "", "pve_version": detail.get("pveversion") or ""})
    vms = []
    for item in resources:
        vm_type = item.get("type") or "qemu"
        vmid = int(item.get("vmid") or 0)
        node = item.get("node") or ""
        config = client.get(f"/nodes/{quote(str(node), safe='')}/{vm_type}/{vmid}/config") or {}
        config_subset = {key: config.get(key) for key in sorted(config) if key in {"cores", "sockets", "memory", "balloon", "ostype", "bios", "machine", "onboot"} or re.match(r"^(scsi|sata|ide|virtio|net)\d+$", key)}
        signature = hashlib.sha256(json.dumps(config_subset, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        disks = [{"name": key, "config": str(value)} for key, value in config_subset.items() if re.match(r"^(scsi|sata|ide|virtio)\d+$", key) and "media=cdrom" not in str(value)]
        networks = [{"name": key, "config": str(value)} for key, value in config_subset.items() if re.match(r"^net\d+$", key)]
        vm = {"id": f"{vm_type}:{vmid}", "vmid": vmid, "name": item.get("name") or f"VM {vmid}", "type": vm_type, "node": node, "status": item.get("status") or "unknown", "cpu_percent": round(float(item.get("cpu") or 0) * 100, 1), "cpu_count": int(item.get("maxcpu") or config.get("cores") or 0), "memory_used_bytes": int(item.get("mem") or 0), "memory_total_bytes": int(item.get("maxmem") or int(config.get("memory") or 0) * 1048576), "disk_used_bytes": int(item.get("disk") or 0), "disk_total_bytes": int(item.get("maxdisk") or 0), "uptime_seconds": int(item.get("uptime") or 0), "onboot": bool(int(config.get("onboot") or 0)), "os_type": config.get("ostype") or "", "disks": disks, "networks": networks, "config_signature": signature}
        vm["config_summary"] = human_config(vm)
        vms.append(vm)
    return {"url": url, "nodes": node_details, "vms": sorted(vms, key=lambda vm: vm["vmid"]), "error": None}


def resource_level(value, warning, previous, critical=None):
    if critical is not None and value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    if previous in {"warning", "critical"} and value >= warning - 8:
        return "warning"
    return "ok"


def resource_message(name, level, value):
    if level == "critical": return f"🔴 {name}严重：{value}%"
    if level == "warning": return f"🟠 {name}过高：{value}%"
    return f"✅ {name}已恢复：{value}%"


def collect(previous_state, pve_env):
    cpu = cpu_percent()
    memory_used, memory_total, memory = memory_info()
    disk_usage = shutil.disk_usage("/")
    disk = round(disk_usage.used / disk_usage.total * 100, 1)
    load_1, load_5, load_15 = os.getloadavg()
    previous_observed = previous_state.get("observed", {})
    counters = previous_state.get("counters", {})
    counters["cpu"] = counters.get("cpu", 0) + 1 if cpu >= CPU_WARN else 0
    counters["memory"] = counters.get("memory", 0) + 1 if memory >= MEMORY_WARN else 0
    cpu_level = resource_level(cpu, CPU_WARN, previous_observed.get("cpu", "ok")) if counters["cpu"] >= 3 or previous_observed.get("cpu") != "ok" else "ok"
    memory_level = resource_level(memory, MEMORY_WARN, previous_observed.get("memory", "ok")) if counters["memory"] >= 3 or previous_observed.get("memory") != "ok" else "ok"
    disk_level = resource_level(disk, DISK_WARN, previous_observed.get("disk", "ok"), DISK_CRITICAL)
    observed = {"cpu": cpu_level, "memory": memory_level, "disk": disk_level}
    try:
        containers, docker_error = docker_containers(), None
    except Exception as exc:
        containers, docker_error = [], str(exc)[:300]
    try:
        pve = pve_inventory(pve_env)
    except Exception as exc:
        pve = {"url": pve_env.get("PVE_URL", ""), "nodes": [], "vms": [], "error": str(exc)[:300]}
    local_name = next((vm.get("name") for vm in pve.get("vms", []) if int(vm.get("vmid") or 0) == int(os.getenv("LOCAL_VM_ID", "101"))), socket.gethostname())
    docker_nodes = [{"vmid": int(os.getenv("LOCAL_VM_ID", "101")), "vm_name": local_name, "hostname": socket.gethostname(), "updated_at": now_text(), "age_seconds": 0, "docker_available": True, "error": docker_error, "containers": containers}]
    docker_nodes.extend(load_remote_docker_nodes())
    issues = []
    for key, value, level in [("CPU", cpu, cpu_level), ("内存", memory, memory_level), ("硬盘", disk, disk_level)]:
        if level != "ok": issues.append(f"{key} {value}%")
    if docker_error: issues.append(f"Docker：{docker_error}")
    issues.extend(f"容器 {item['name']}：{item['condition']}" for item in containers if item["condition"] != "ok")
    for node in docker_nodes[1:]:
        label = f"{node.get('vmid', '?')} {node.get('vm_name') or node.get('hostname') or '未知虚拟机'}"
        if node.get("error"):
            issues.append(f"Docker监控端 {label}：{node['error']}")
        issues.extend(f"容器 {label}/{item.get('name')}：{item.get('condition')}" for item in (node.get("containers") or []) if item.get("condition") != "ok")
    if pve["error"]: issues.append(f"PVE：{pve['error']}")
    status = {"updated_at": now_text(), "boot_id": boot_id(), "uptime_seconds": uptime_seconds(), "cpu_percent": cpu, "memory_percent": memory, "disk_percent": disk, "load_average": [round(load_1, 2), round(load_5, 2), round(load_15, 2)], "temperature_c": temperature_c(), "resource_levels": observed, "system": system_info(memory_total, disk_usage.total), "docker_error": docker_error, "containers": containers, "docker_nodes": docker_nodes, "pve": pve, "issues": issues}
    return status, observed, counters


def container_snapshot(docker_nodes):
    snapshot = {}
    for node in docker_nodes:
        vmid = str(node.get("vmid") or "?")
        vm_name = node.get("vm_name") or node.get("hostname") or "未知虚拟机"
        for item in node.get("containers") or []:
            key = f"{vmid}:{item.get('name')}"
            snapshot[key] = {"vmid": vmid, "vm_name": vm_name, "name": item.get("name") or "unknown", "condition": item.get("condition") or "unknown", "image": item.get("image", ""), "status": item.get("status", "")}
    return snapshot


def vm_snapshot(vms):
    return {item["id"]: {"vmid": item["vmid"], "name": item["name"], "type": item["type"], "status": item["status"], "config_signature": item["config_signature"], "config_summary": item["config_summary"]} for item in vms}


def notify_transitions(sender, status, state, observed, counters):
    current_containers = container_snapshot(status.get("docker_nodes") or [{"vmid": 101, "vm_name": "Debian13-Tools", "containers": status["containers"]}])
    current_vms = vm_snapshot(status.get("pve", {}).get("vms", []))
    previous_containers = state.get("containers")
    previous_vms = state.get("vms")
    changes = []
    first_boot = state.get("boot_id") != status["boot_id"]
    if first_boot:
        abnormal = [name for name, item in current_containers.items() if item["condition"] != "ok"]
        changes.append("✅ 小主机监控已启动")
        changes.append(f"CPU：{status['cpu_percent']}%｜内存：{status['memory_percent']}%｜硬盘：{status['disk_percent']}%")
        changes.append(f"Docker：{len(current_containers)} 个｜异常：{len(abnormal)} 个")
    else:
        notified_resources = state.get("notified_resources", {})
        for key, current in observed.items():
            previous = notified_resources.get(key, "ok")
            if current != previous:
                name = {"cpu": "CPU", "memory": "内存", "disk": "系统盘"}[key]
                changes.append(resource_message(name, current, status[f"{key}_percent"]))
        previous_docker_error = state.get("docker_error")
        current_docker_error = status.get("docker_error")
        if bool(previous_docker_error) != bool(current_docker_error):
            changes.append(f"🔴 Docker服务异常：{current_docker_error}" if current_docker_error else "✅ Docker服务已恢复")
        if previous_containers is not None:
            for key in sorted(set(current_containers) - set(previous_containers)):
                item = current_containers[key]
                changes.append(f"🆕 Docker容器已创建：[{item['vmid']} {item['vm_name']}] {item['name']}｜{item['image']}")
            for key in sorted(set(previous_containers) - set(current_containers)):
                item = previous_containers[key]
                changes.append(f"🗑️ Docker容器已删除：[{item['vmid']} {item['vm_name']}] {item['name']}｜{item['image']}")
            for key in sorted(set(current_containers) & set(previous_containers)):
                item = current_containers[key]
                current = item["condition"]
                previous = previous_containers[key]["condition"]
                if current != previous:
                    label = f"[{item['vmid']} {item['vm_name']}] {item['name']}"
                    changes.append(f"✅ Docker容器已恢复：{label}" if current == "ok" else f"🔴 Docker容器异常：{label}（{current}）")
        previous_pve_error = state.get("pve_error")
        current_pve_error = status.get("pve", {}).get("error")
        if bool(previous_pve_error) != bool(current_pve_error):
            changes.append(f"🔴 PVE连接异常：{current_pve_error}" if current_pve_error else "✅ PVE连接已恢复")
        if previous_vms is None and not current_pve_error:
            running = sum(item["status"] == "running" for item in current_vms.values())
            changes.append(f"✅ PVE虚拟机监控已接入：{len(current_vms)} 台｜{running} 台运行")
        elif previous_vms is not None and not current_pve_error:
            for vm_id in sorted(set(current_vms) - set(previous_vms)):
                vm = current_vms[vm_id]
                changes.append(f"🆕 虚拟机已创建：{vm['vmid']} {vm['name']}｜{vm['config_summary']}")
            for vm_id in sorted(set(previous_vms) - set(current_vms)):
                vm = previous_vms[vm_id]
                changes.append(f"🗑️ 虚拟机已删除：{vm['vmid']} {vm['name']}")
            for vm_id in sorted(set(current_vms) & set(previous_vms)):
                current, previous = current_vms[vm_id], previous_vms[vm_id]
                if current["status"] != previous["status"]:
                    icon = "🟢" if current["status"] == "running" else "🔴"
                    changes.append(f"{icon} 虚拟机状态变化：{current['vmid']} {current['name']}｜{previous['status']} → {current['status']}")
                if current["config_signature"] != previous["config_signature"]:
                    changes.append(f"⚙️ 虚拟机配置已修改：{current['vmid']} {current['name']}\n原：{previous['config_summary']}\n现：{current['config_summary']}")
    if changes:
        message = "🏠 小主机状态变化\n\n" + "\n".join(changes[:30]) + f"\n\n时间：{datetime.now(CHINA_TZ):%Y-%m-%d %H:%M:%S}"
        if not sender.send(message):
            state["observed"] = observed
            state["counters"] = counters
            return state
    state.update({"boot_id": status["boot_id"], "observed": observed, "counters": counters, "notified_resources": observed, "docker_error": status.get("docker_error"), "containers": current_containers, "pve_error": status.get("pve", {}).get("error"), "vms": current_vms if not status.get("pve", {}).get("error") else previous_vms})
    return state


def stop_handler(_signum, _frame):
    global STOP
    STOP = True


def main():
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    bot_env = load_env(BOT_ENV_PATH)
    pve_env = load_env(PVE_ENV_PATH)
    dingtalk_env = load_env(DINGTALK_ENV_PATH)
    sender = MultiSender(
        TelegramSender(bot_env.get("BOT_TOKEN", ""), bot_env.get("ALLOWED_CHAT_ID") or bot_env.get("TELEGRAM_CHAT_ID", "")),
        DingTalkSender(dingtalk_env.get("DINGTALK_WEBHOOK", ""), dingtalk_env.get("DINGTALK_SECRET", "")),
    )
    print("小主机、Docker 与 PVE 监控已启动", flush=True)
    while not STOP:
        state = load_json(STATE_PATH, {})
        try:
            status, observed, counters = collect(state, pve_env)
            save_json(STATUS_PATH, status)
            save_json(STATE_PATH, notify_transitions(sender, status, state, observed, counters))
        except Exception as exc:
            print(f"监控循环异常：{exc}", flush=True)
        for _ in range(CHECK_INTERVAL):
            if STOP: break
            time.sleep(1)
    sender.close()


if __name__ == "__main__":
    if "--test-dingtalk" in sys.argv:
        env = load_env(DINGTALK_ENV_PATH)
        ok = DingTalkSender(env.get("DINGTALK_WEBHOOK", ""), env.get("DINGTALK_SECRET", "")).send(
            f"✅ 钉钉监控通知测试成功\n时间：{datetime.now(CHINA_TZ):%Y-%m-%d %H:%M:%S}"
        )
        print("钉钉测试消息发送成功" if ok else "钉钉测试消息发送失败", flush=True)
        raise SystemExit(0 if ok else 1)
    main()
