import atexit
import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from urllib import error, parse, request
from zoneinfo import ZoneInfo

from flask import Flask, flash, redirect, render_template, request as flask_request, send_file, session, url_for
from cryptography.fernet import Fernet, InvalidToken
import paramiko
import socks

from sui_client import SUIClient, SUIError, normalize_sui_url
from proxy_tools import (
    ProxyCheckError,
    build_proxy_uri,
    check_cloudflare_panel,
    check_socks5,
    normalize_panel_url,
    panel_config_request,
    panel_engine,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "vps-monitor.db"
TIMEZONE = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))
CHECK_INTERVAL = max(30, int(os.getenv("CHECK_INTERVAL", "60")))
FAILURE_THRESHOLD = max(1, int(os.getenv("FAILURE_THRESHOLD", "3")))
PING_TIMEOUT = max(1, int(os.getenv("PING_TIMEOUT", "3")))
TCP_TIMEOUT = max(1, int(os.getenv("TCP_TIMEOUT", "5")))
TRAFFIC_CHECK_INTERVAL = max(60, int(os.getenv("TRAFFIC_CHECK_INTERVAL", "300")))
SSH_TIMEOUT = max(3, int(os.getenv("SSH_TIMEOUT", "8")))
SUI_CHECK_INTERVAL = max(60, int(os.getenv("SUI_CHECK_INTERVAL", "300")))
SUI_TIMEOUT = max(5, int(os.getenv("SUI_TIMEOUT", "15")))
PROXY_CHECK_INTERVAL = max(60, int(os.getenv("PROXY_CHECK_INTERVAL", "300")))
CF_CHECK_INTERVAL = max(60, int(os.getenv("CF_CHECK_INTERVAL", "300")))
PROXY_TIMEOUT = max(5, int(os.getenv("PROXY_TIMEOUT", "12")))
CF_TIMEOUT = max(5, int(os.getenv("CF_TIMEOUT", "15")))
EXPIRY_NOTICE_DAYS = (7, 3, 1, 0)
CURRENCIES = {
    "CNY": "¥",
    "USD": "$",
    "EUR": "€",
    "HKD": "HK$",
    "JPY": "¥",
    "GBP": "£",
}

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.secret_key = os.getenv("SECRET_KEY", "please-change-this-secret-key")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

monitor_stop = threading.Event()
monitor_thread = None
check_lock = threading.Lock()
traffic_check_lock = threading.Lock()
sui_check_lock = threading.Lock()
proxy_check_lock = threading.Lock()
cf_check_lock = threading.Lock()


def now_local():
    return datetime.now(TIMEZONE)


@contextmanager
def db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                check_type TEXT NOT NULL DEFAULT 'ping',
                port INTEGER,
                provider TEXT,
                expiry_date TEXT NOT NULL,
                renewal_url TEXT,
                renewal_amount_cents INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'CNY',
                traffic_total_gb REAL,
                traffic_remaining_gb REAL,
                bandwidth_mbps REAL,
                memory_gb REAL,
                cpu_cores INTEGER,
                disk_gb REAL,
                login_username TEXT,
                login_password_encrypted TEXT,
                ssh_port INTEGER NOT NULL DEFAULT 22,
                traffic_auto INTEGER NOT NULL DEFAULT 0,
                traffic_used_bytes INTEGER NOT NULL DEFAULT 0,
                traffic_baseline_rx_bytes INTEGER,
                traffic_baseline_tx_bytes INTEGER,
                traffic_reset_day INTEGER NOT NULL DEFAULT 1,
                traffic_direction TEXT NOT NULL DEFAULT 'both',
                traffic_cycle TEXT,
                traffic_last_check TEXT,
                traffic_error TEXT,
                traffic_alert_level INTEGER NOT NULL DEFAULT 0,
                sui_enabled INTEGER NOT NULL DEFAULT 0,
                sui_url TEXT,
                sui_token_encrypted TEXT,
                sui_verify_tls INTEGER NOT NULL DEFAULT 1,
                sui_summary_json TEXT,
                sui_last_check TEXT,
                sui_error TEXT,
                sui_alerted INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                status TEXT NOT NULL DEFAULT 'unknown',
                latency_ms REAL,
                last_check TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                outage_alerted INTEGER NOT NULL DEFAULT 0,
                expiry_notice_for TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(vps)").fetchall()}
        if "renewal_amount_cents" not in columns:
            conn.execute(
                "ALTER TABLE vps ADD COLUMN renewal_amount_cents INTEGER NOT NULL DEFAULT 0"
            )
        if "currency" not in columns:
            conn.execute(
                "ALTER TABLE vps ADD COLUMN currency TEXT NOT NULL DEFAULT 'CNY'"
            )
        optional_columns = {
            "traffic_total_gb": "REAL",
            "traffic_remaining_gb": "REAL",
            "bandwidth_mbps": "REAL",
            "memory_gb": "REAL",
            "cpu_cores": "INTEGER",
            "disk_gb": "REAL",
            "login_username": "TEXT",
            "login_password_encrypted": "TEXT",
            "ssh_port": "INTEGER NOT NULL DEFAULT 22",
            "traffic_auto": "INTEGER NOT NULL DEFAULT 0",
            "traffic_used_bytes": "INTEGER NOT NULL DEFAULT 0",
            "traffic_baseline_rx_bytes": "INTEGER",
            "traffic_baseline_tx_bytes": "INTEGER",
            "traffic_reset_day": "INTEGER NOT NULL DEFAULT 1",
            "traffic_direction": "TEXT NOT NULL DEFAULT 'both'",
            "traffic_cycle": "TEXT",
            "traffic_last_check": "TEXT",
            "traffic_error": "TEXT",
            "traffic_alert_level": "INTEGER NOT NULL DEFAULT 0",
            "sui_enabled": "INTEGER NOT NULL DEFAULT 0",
            "sui_url": "TEXT",
            "sui_token_encrypted": "TEXT",
            "sui_verify_tls": "INTEGER NOT NULL DEFAULT 1",
            "sui_summary_json": "TEXT",
            "sui_last_check": "TEXT",
            "sui_error": "TEXT",
            "sui_alerted": "INTEGER NOT NULL DEFAULT 0",
            "proxy_id": "INTEGER",
            "proxy_config_synced": "INTEGER NOT NULL DEFAULT 0",
            "proxy_config_last_check": "TEXT",
            "proxy_config_error": "TEXT",
        }
        for column, data_type in optional_columns.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE vps ADD COLUMN {column} {data_type}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proxy_endpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                username TEXT,
                password_encrypted TEXT,
                notes TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'unknown',
                latency_ms REAL,
                exit_ip TEXT,
                last_check TEXT,
                error TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                alerted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cloudflare_panels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                panel_type TEXT NOT NULL DEFAULT 'worker',
                panel_engine TEXT NOT NULL DEFAULT 'cfnew',
                panel_url TEXT NOT NULL,
                admin_password_encrypted TEXT,
                proxy_id INTEGER,
                outbound_strategy TEXT NOT NULL DEFAULT 'proxy_first',
                enabled INTEGER NOT NULL DEFAULT 1,
                verify_tls INTEGER NOT NULL DEFAULT 1,
                notes TEXT,
                status TEXT NOT NULL DEFAULT 'unknown',
                latency_ms REAL,
                http_status INTEGER,
                version TEXT,
                cf_colo TEXT,
                proxy_ip_status TEXT,
                ech_status TEXT,
                node_count INTEGER NOT NULL DEFAULT 0,
                cert_expiry TEXT,
                last_check TEXT,
                error TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                alerted INTEGER NOT NULL DEFAULT 0,
                config_synced INTEGER NOT NULL DEFAULT 0,
                config_last_check TEXT,
                config_error TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(proxy_id) REFERENCES proxy_endpoints(id)
            )
            """
        )
        cloudflare_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(cloudflare_panels)").fetchall()
        }
        if "panel_engine" not in cloudflare_columns:
            conn.execute(
                "ALTER TABLE cloudflare_panels ADD COLUMN panel_engine TEXT NOT NULL DEFAULT 'cfnew'"
            )
            conn.execute(
                "UPDATE cloudflare_panels SET panel_engine='edgetunnel' "
                "WHERE lower(rtrim(panel_url, '/')) LIKE '%/admin'"
            )
        if "admin_password_encrypted" not in cloudflare_columns:
            conn.execute(
                "ALTER TABLE cloudflare_panels ADD COLUMN admin_password_encrypted TEXT"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cloudflare_panels_proxy_id ON cloudflare_panels(proxy_id)"
        )


def get_setting(key, env_name=None):
    with db_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is not None:
        return row["value"].strip()
    return os.getenv(env_name or key.upper(), "").strip()


def set_setting(key, value):
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value.strip()),
        )


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=flask_request.path))
        return view(*args, **kwargs)

    return wrapped


def telegram_send(message):
    token = get_setting("telegram_bot_token", "TELEGRAM_BOT_TOKEN")
    chat_id = get_setting("telegram_chat_id", "TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False, "未配置 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID"

    payload = parse.urlencode(
        {"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    try:
        req = request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            method="POST",
        )
        with request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                return True, "发送成功"
            return False, f"Telegram 返回 HTTP {response.status}"
    except (error.URLError, TimeoutError, OSError) as exc:
        return False, str(exc)


def dingtalk_config():
    values = {
        "DINGTALK_WEBHOOK": os.getenv("DINGTALK_WEBHOOK", "").strip(),
        "DINGTALK_SECRET": os.getenv("DINGTALK_SECRET", "").strip(),
    }
    if values["DINGTALK_WEBHOOK"] and values["DINGTALK_SECRET"]:
        return values
    try:
        for line in Path(os.getenv("DINGTALK_ENV_FILE", "/app/dingtalk.env")).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in values:
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def dingtalk_send(message):
    config = dingtalk_config()
    webhook = config["DINGTALK_WEBHOOK"]
    secret = config["DINGTALK_SECRET"]
    if not webhook or not secret:
        return False, "未配置钉钉 Webhook 或加签密钥"
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    signature = base64.b64encode(hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()).decode("ascii")
    signed_url = f"{webhook}{'&' if '?' in webhook else '?'}timestamp={timestamp}&sign={parse.quote_plus(signature)}"
    payload = json.dumps({"msgtype": "text", "text": {"content": message}}, ensure_ascii=False).encode("utf-8")
    try:
        req = request.Request(signed_url, data=payload, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        with request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read())
            if response.status == 200 and int(result.get("errcode", -1)) == 0:
                return True, "发送成功"
            return False, f"钉钉返回：{result}"
    except (error.URLError, TimeoutError, OSError, ValueError) as exc:
        return False, str(exc)


def notification_send(message):
    telegram_ok, telegram_detail = telegram_send(message)
    dingtalk_ok, dingtalk_detail = dingtalk_send(message)
    return telegram_ok or dingtalk_ok, f"Telegram: {telegram_detail}; 钉钉: {dingtalk_detail}"


def ping_check(host):
    started = time.monotonic()
    result = subprocess.run(
        ["ping", "-c", "1", "-W", str(PING_TIMEOUT), host],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=PING_TIMEOUT + 2,
        check=False,
    )
    latency = round((time.monotonic() - started) * 1000, 1)
    return result.returncode == 0, latency


def tcp_check(host, port):
    started = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=TCP_TIMEOUT):
            latency = round((time.monotonic() - started) * 1000, 1)
            return True, latency
    except (OSError, ValueError):
        return False, None


def check_one(vps):
    try:
        if vps["check_type"] == "tcp":
            ok, latency = tcp_check(vps["host"], vps["port"])
        else:
            ok, latency = ping_check(vps["host"])
    except (OSError, subprocess.SubprocessError):
        ok, latency = False, None

    previous = vps["status"]
    failures = 0 if ok else vps["consecutive_failures"] + 1
    status = "online" if ok else ("offline" if failures >= FAILURE_THRESHOLD else previous)
    if status not in {"online", "offline"}:
        status = "unknown"

    outage_alerted = vps["outage_alerted"]
    alert_message = None
    if status == "offline" and previous != "offline" and not outage_alerted:
        outage_alerted = 1
        alert_message = (
            f"🔴 VPS 掉线提醒\n"
            f"名称：{vps['name']}\n地址：{vps['host']}\n"
            f"连续失败：{failures} 次\n时间：{now_local():%Y-%m-%d %H:%M:%S}"
        )
    elif ok and previous == "offline":
        outage_alerted = 0
        alert_message = (
            f"🟢 VPS 已恢复\n名称：{vps['name']}\n地址：{vps['host']}\n"
            f"延迟：{latency} ms\n时间：{now_local():%Y-%m-%d %H:%M:%S}"
        )
    elif ok:
        outage_alerted = 0

    with db_connection() as conn:
        conn.execute(
            """
            UPDATE vps
            SET status=?, latency_ms=?, last_check=?, consecutive_failures=?, outage_alerted=?
            WHERE id=?
            """,
            (
                status,
                latency if ok else None,
                now_local().isoformat(timespec="seconds"),
                failures,
                outage_alerted,
                vps["id"],
            ),
        )
    if alert_message:
        notification_send(alert_message)


def check_expiry_reminders():
    today = now_local().date()
    with db_connection() as conn:
        rows = conn.execute("SELECT * FROM vps ORDER BY id").fetchall()
    for vps in rows:
        try:
            expiry = date.fromisoformat(vps["expiry_date"])
        except (TypeError, ValueError):
            continue
        days = (expiry - today).days
        thresholds = [threshold for threshold in EXPIRY_NOTICE_DAYS if days <= threshold]
        notice_threshold = min(thresholds) if thresholds else None
        notice_key = (
            f"{expiry.isoformat()}:{notice_threshold}"
            if notice_threshold is not None
            else None
        )
        if notice_key and vps["expiry_notice_for"] != notice_key:
            if days < 0:
                label = f"已过期 {-days} 天"
            elif days == 0:
                label = "今天到期"
            else:
                label = f"还有 {days} 天到期"
            message = (
                f"⏰ VPS 续费提醒\n名称：{vps['name']}\n"
                f"服务商：{vps['provider'] or '未填写'}\n到期日：{expiry.isoformat()}\n"
                f"状态：{label}\n"
                f"续费金额：{format_amount(vps['renewal_amount_cents'], vps['currency'])}"
            )
            if vps["renewal_url"]:
                message += f"\n续费地址：{vps['renewal_url']}"
            sent, _ = notification_send(message)
            if sent:
                with db_connection() as conn:
                    conn.execute(
                        "UPDATE vps SET expiry_notice_for=? WHERE id=?",
                        (notice_key, vps["id"]),
                    )


def current_traffic_cycle(reset_day, today=None):
    today = today or now_local().date()
    reset_day = min(max(int(reset_day or 1), 1), 28)
    if today.day >= reset_day:
        return f"{today.year:04d}-{today.month:02d}-{reset_day:02d}"
    if today.month == 1:
        return f"{today.year - 1:04d}-12-{reset_day:02d}"
    return f"{today.year:04d}-{today.month - 1:02d}-{reset_day:02d}"


def ssh_read_counters(vps, password, transport_proxy=None):
    attempts = [transport_proxy, None] if transport_proxy else [None]
    errors = []
    for proxy in attempts:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        proxy_socket = None
        try:
            if proxy:
                proxy_socket = socks.socksocket()
                proxy_socket.set_proxy(
                    socks.SOCKS5,
                    proxy["host"],
                    int(proxy["port"]),
                    True,
                    proxy.get("username") or None,
                    proxy.get("password") or None,
                )
                proxy_socket.settimeout(SSH_TIMEOUT)
                proxy_socket.connect((vps["host"], int(vps.get("ssh_port") or 22)))
            client.connect(
                hostname=vps["host"],
                port=int(vps.get("ssh_port") or 22),
                username=vps["login_username"],
                password=password,
                timeout=SSH_TIMEOUT,
                banner_timeout=SSH_TIMEOUT,
                auth_timeout=SSH_TIMEOUT,
                allow_agent=False,
                look_for_keys=False,
                sock=proxy_socket,
            )
            command = (
                "iface=$(ip route show default 2>/dev/null | awk 'NR==1{print $5}'); "
                "[ -n \"$iface\" ] || iface=$(ip -6 route show default 2>/dev/null | awk 'NR==1{print $5}'); "
                "[ -n \"$iface\" ] && cat /sys/class/net/$iface/statistics/rx_bytes "
                "/sys/class/net/$iface/statistics/tx_bytes"
            )
            _, stdout, stderr = client.exec_command(command, timeout=SSH_TIMEOUT)
            output = stdout.read().decode("utf-8", "replace").strip().splitlines()
            error_text = stderr.read().decode("utf-8", "replace").strip()
            if stdout.channel.recv_exit_status() != 0 or len(output) < 2:
                raise RuntimeError(error_text or "无法读取默认网卡流量")
            return int(output[-2]), int(output[-1])
        except paramiko.AuthenticationException:
            raise
        except (OSError, paramiko.SSHException, RuntimeError, ValueError) as exc:
            errors.append(exc)
        finally:
            client.close()
            if proxy_socket:
                try:
                    proxy_socket.close()
                except OSError:
                    pass
    if errors:
        raise errors[-1]
    raise RuntimeError("SSH 流量读取失败")


def check_traffic_one(vps_row):
    vps = dict(vps_row)
    checked_at = now_local().isoformat(timespec="seconds")
    try:
        password = decrypt_password(vps.get("login_password_encrypted"))
        if not vps.get("login_username") or not password:
            raise ValueError("未填写 SSH 用户名或密码")
        rx_bytes, tx_bytes = ssh_read_counters(
            vps, password, transport_proxy=get_cloudflare_transport_proxy()
        )
        cycle = current_traffic_cycle(vps.get("traffic_reset_day"))
        used_bytes = int(vps.get("traffic_used_bytes") or 0)
        previous_cycle = vps.get("traffic_cycle")
        previous_alert_level = int(vps.get("traffic_alert_level") or 0)
        baseline_rx = vps.get("traffic_baseline_rx_bytes")
        baseline_tx = vps.get("traffic_baseline_tx_bytes")
        if previous_cycle and previous_cycle != cycle:
            used_bytes = 0
            previous_alert_level = 0
        elif baseline_rx is not None and baseline_tx is not None:
            delta_rx = rx_bytes - baseline_rx if rx_bytes >= baseline_rx else rx_bytes
            delta_tx = tx_bytes - baseline_tx if tx_bytes >= baseline_tx else tx_bytes
            direction = vps.get("traffic_direction") or "both"
            if direction == "rx":
                used_bytes += max(delta_rx, 0)
            elif direction == "tx":
                used_bytes += max(delta_tx, 0)
            else:
                used_bytes += max(delta_rx, 0) + max(delta_tx, 0)
        total_gb = float(vps.get("traffic_total_gb") or 0)
        total_bytes = int(total_gb * 1_000_000_000)
        percent = used_bytes / total_bytes * 100 if total_bytes > 0 else 0
        alert_level = 2 if percent >= 95 else (1 if percent >= 80 else 0)
        stored_alert_level = alert_level
        if alert_level > previous_alert_level:
            threshold = 95 if alert_level == 2 else 80
            sent, _ = notification_send(
                f"⚠️ VPS 流量提醒\n名称：{vps['name']}\n"
                f"已用：{used_bytes / 1_000_000_000:.2f} GB / {total_gb:.2f} GB\n"
                f"使用率：{percent:.1f}%（达到 {threshold}%）\n"
                f"统计周期：{cycle}"
            )
            stored_alert_level = alert_level if sent else previous_alert_level
        with db_connection() as conn:
            conn.execute(
                """
                UPDATE vps SET traffic_used_bytes=?, traffic_baseline_rx_bytes=?,
                 traffic_baseline_tx_bytes=?, traffic_cycle=?, traffic_last_check=?,
                traffic_error=NULL, traffic_alert_level=? WHERE id=?
                """,
                (
                    used_bytes,
                    rx_bytes,
                    tx_bytes,
                    cycle,
                    checked_at,
                    stored_alert_level,
                    vps["id"],
                ),
            )
    except paramiko.AuthenticationException:
        error_message = "SSH 用户名或密码错误"
    except (OSError, paramiko.SSHException, RuntimeError, ValueError) as exc:
        error_message = str(exc) or "SSH 流量读取失败"
    else:
        return
    with db_connection() as conn:
        conn.execute(
            "UPDATE vps SET traffic_last_check=?, traffic_error=? WHERE id=?",
            (checked_at, error_message[:300], vps["id"]),
        )


def run_traffic_checks():
    if not traffic_check_lock.acquire(blocking=False):
        return
    try:
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM vps WHERE traffic_auto=1 ORDER BY id"
            ).fetchall()
        if rows:
            with ThreadPoolExecutor(max_workers=min(5, len(rows))) as pool:
                list(pool.map(check_traffic_one, rows))
    finally:
        traffic_check_lock.release()


def fetch_sui_summary(vps):
    client = get_sui_client(vps)
    status = client.get("status", {"r": "cpu,mem,net,sys,sbd,dsk,swp,dio,db"}) or {}
    onlines = client.get("onlines") or {}
    db_info = status.get("db") or {}
    singbox = status.get("sbd") or {}
    system = status.get("sys") or {}
    return {
        "api_ok": True,
        "core_running": bool(singbox.get("running")),
        "cpu": status.get("cpu", 0),
        "memory": status.get("mem") or {},
        "disk": status.get("dsk") or {},
        "swap": status.get("swp") or {},
        "network": status.get("net") or {},
        "disk_io": status.get("dio") or {},
        "system": system,
        "singbox": singbox,
        "counts": db_info,
        "onlines": onlines,
        "checked_at": now_local().isoformat(timespec="seconds"),
    }


def check_sui_one(vps_row):
    vps = dict(vps_row)
    checked_at = now_local().isoformat(timespec="seconds")
    previous_alerted = bool(vps.get("sui_alerted"))
    proxy_synced = int(vps.get("proxy_config_synced") or 0)
    proxy_error = None
    try:
        summary = fetch_sui_summary(vps)
        error_message = None if summary["core_running"] else "Sing-box 核心未运行"
        if not error_message:
            try:
                proxy_synced = int(inspect_sui_proxy_sync(vps))
            except (SUIError, ValueError) as exc:
                proxy_synced = 0
                proxy_error = str(exc)[:300]
    except (SUIError, ValueError) as exc:
        summary = {}
        error_message = str(exc) or "S-UI 检测失败"

    alerted = previous_alerted
    if error_message and not previous_alerted:
        notification_send(
            f"🔴 S-UI 节点异常\n名称：{vps['name']}\n地址：{vps['host']}\n"
            f"原因：{error_message}\n时间：{now_local():%Y-%m-%d %H:%M:%S}"
        )
        alerted = True
    elif not error_message and previous_alerted:
        notification_send(
            f"🟢 S-UI 节点已恢复\n名称：{vps['name']}\n地址：{vps['host']}\n"
            f"时间：{now_local():%Y-%m-%d %H:%M:%S}"
        )
        alerted = False

    with db_connection() as conn:
        conn.execute(
            """
            UPDATE vps SET sui_summary_json=?, sui_last_check=?, sui_error=?, sui_alerted=?,
            proxy_config_synced=?, proxy_config_last_check=?, proxy_config_error=?
            WHERE id=?
            """,
            (
                json.dumps(summary, ensure_ascii=False),
                checked_at,
                error_message,
                int(alerted),
                proxy_synced,
                checked_at,
                proxy_error,
                vps["id"],
            ),
        )


def run_sui_checks():
    if not sui_check_lock.acquire(blocking=False):
        return
    try:
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM vps WHERE sui_enabled=1 ORDER BY id"
            ).fetchall()
        if rows:
            with ThreadPoolExecutor(max_workers=min(5, len(rows))) as pool:
                list(pool.map(check_sui_one, rows))
    finally:
        sui_check_lock.release()


def serialize_proxy(row, include_password=False):
    item = dict(row)
    password = decrypt_secret(item.get("password_encrypted")) if include_password else ""
    item["password"] = password
    item["uri"] = build_proxy_uri(item, include_scheme=True) if include_password else ""
    item["cfnew_value"] = build_proxy_uri(item, include_scheme=False) if include_password else ""
    item["masked_address"] = f"{item.get('host')}:{item.get('port')}"
    return item


def get_proxy(proxy_id):
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM proxy_endpoints WHERE id=?", (proxy_id,)).fetchone()
    return dict(row) if row else None


def get_cloudflare_transport_proxy(preferred=None):
    """Choose an enabled SOCKS endpoint only as a reliable CFnew management path."""
    if preferred and preferred.get("enabled"):
        return serialize_proxy(preferred, include_password=True)
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM proxy_endpoints WHERE enabled=1
            ORDER BY CASE status WHEN 'online' THEN 0 ELSE 1 END, id LIMIT 1
            """
        ).fetchone()
    return serialize_proxy(row, include_password=True) if row else None


def check_proxy_one(proxy_row):
    proxy = dict(proxy_row)
    checked_at = now_local().isoformat(timespec="seconds")
    previous = proxy.get("status") or "unknown"
    try:
        check_data = {
            **proxy,
            "password": decrypt_secret(proxy.get("password_encrypted")),
        }
        result = check_socks5(check_data, timeout=PROXY_TIMEOUT)
        ok = True
        error_message = None
    except (ProxyCheckError, OSError, ValueError) as exc:
        result = {}
        ok = False
        error_message = str(exc) or "SOCKS5检测失败"

    failures = 0 if ok else int(proxy.get("consecutive_failures") or 0) + 1
    status = "online" if ok else ("offline" if failures >= FAILURE_THRESHOLD else previous)
    if status not in {"online", "offline"}:
        status = "unknown"
    alerted = bool(proxy.get("alerted"))
    alert_message = None
    if status == "offline" and previous != "offline" and not alerted:
        alerted = True
        alert_message = (
            f"🔴 SOCKS5落地不可用\n名称：{proxy['name']}\n"
            f"地址：{proxy['host']}:{proxy['port']}\n原因：{error_message}\n"
            f"时间：{now_local():%Y-%m-%d %H:%M:%S}"
        )
    elif ok and previous == "offline":
        alerted = False
        alert_message = (
            f"🟢 SOCKS5落地已恢复\n名称：{proxy['name']}\n"
            f"出口IP：{result.get('exit_ip') or '未知'}\n"
            f"延迟：{result.get('latency_ms')} ms\n时间：{now_local():%Y-%m-%d %H:%M:%S}"
        )
    elif ok:
        alerted = False

    with db_connection() as conn:
        conn.execute(
            """
            UPDATE proxy_endpoints SET status=?, latency_ms=?, exit_ip=?, last_check=?,
            error=?, consecutive_failures=?, alerted=? WHERE id=?
            """,
            (
                status,
                result.get("latency_ms") if ok else None,
                result.get("exit_ip") if ok else proxy.get("exit_ip"),
                checked_at,
                error_message[:300] if error_message else None,
                failures,
                int(alerted),
                proxy["id"],
            ),
        )
    if alert_message:
        notification_send(alert_message)


def run_proxy_checks():
    if not proxy_check_lock.acquire(blocking=False):
        return
    try:
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM proxy_endpoints WHERE enabled=1 ORDER BY id"
            ).fetchall()
        if rows:
            with ThreadPoolExecutor(max_workers=min(6, len(rows))) as pool:
                list(pool.map(check_proxy_one, rows))
    finally:
        proxy_check_lock.release()


def proxy_config_values(proxy, strategy):
    if not proxy:
        return "", ""
    proxy_with_secret = serialize_proxy(proxy, include_password=True)
    qj = {"proxy_first": "", "direct_first": "no", "proxy_only": "only"}.get(
        strategy, ""
    )
    return proxy_with_secret["cfnew_value"], qj


def normalize_proxy_config(value):
    value = (value or "").strip()
    for prefix in ("socks5://", "socks://"):
        if value.lower().startswith(prefix):
            return value[len(prefix):]
    return value


def prepare_cloudflare_panel(panel):
    runtime = dict(panel)
    runtime["panel_engine"] = panel_engine(runtime)
    runtime["admin_password"] = decrypt_secret(
        runtime.get("admin_password_encrypted")
    )
    return runtime


def check_cloudflare_one(panel_row):
    panel = prepare_cloudflare_panel(panel_row)
    checked_at = now_local().isoformat(timespec="seconds")
    previous = panel.get("status") or "unknown"
    selected_proxy = get_proxy(panel.get("proxy_id")) if panel.get("proxy_id") else None
    transport_proxy = get_cloudflare_transport_proxy(selected_proxy)
    try:
        result = check_cloudflare_panel(
            panel, timeout=CF_TIMEOUT, transport_proxy=transport_proxy
        )
        ok = True
        error_message = None
    except (RuntimeError, OSError, ValueError) as exc:
        result = {}
        ok = False
        error_message = str(exc) or "前置代理面板检测失败"

    failures = 0 if ok else int(panel.get("consecutive_failures") or 0) + 1
    status = "online" if ok else ("offline" if failures >= FAILURE_THRESHOLD else previous)
    if status not in {"online", "offline"}:
        status = "unknown"
    alerted = bool(panel.get("alerted"))
    alert_message = None
    if status == "offline" and previous != "offline" and not alerted:
        alerted = True
        alert_message = (
            f"🔴 Cloudflare前置代理异常\n名称：{panel['name']}\n"
            f"类型：{panel['panel_type'].upper()}\n原因：{error_message}\n"
            f"时间：{now_local():%Y-%m-%d %H:%M:%S}"
        )
    elif ok and previous == "offline":
        alerted = False
        alert_message = (
            f"🟢 Cloudflare前置代理已恢复\n名称：{panel['name']}\n"
            f"延迟：{result.get('latency_ms')} ms\n时间：{now_local():%Y-%m-%d %H:%M:%S}"
        )
    elif ok:
        alerted = False

    config_synced = 0
    config_error = None
    if ok:
        try:
            expected_s, expected_qj = proxy_config_values(
                selected_proxy, panel.get("outbound_strategy")
            )
            if panel["panel_engine"] == "edgetunnel" and selected_proxy:
                expected_qj = "only"
            if (
                panel["panel_engine"] == "edgetunnel"
                and not panel.get("admin_password")
                and not selected_proxy
            ):
                remote = {"s": "", "qj": ""}
            else:
                remote = panel_config_request(
                    panel, timeout=CF_TIMEOUT, transport_proxy=transport_proxy
                )
            config_synced = int(
                normalize_proxy_config(remote.get("s")) == normalize_proxy_config(expected_s)
                and (remote.get("qj") or "") == expected_qj
            )
        except (RuntimeError, ValueError) as exc:
            config_error = str(exc)[:300]

    with db_connection() as conn:
        conn.execute(
            """
            UPDATE cloudflare_panels SET status=?, latency_ms=?, http_status=?, version=?,
            cf_colo=?, proxy_ip_status=?, ech_status=?, node_count=?, cert_expiry=?,
            last_check=?, error=?, consecutive_failures=?, alerted=?, config_synced=?,
            config_last_check=?, config_error=? WHERE id=?
            """,
            (
                status,
                result.get("latency_ms") if ok else None,
                result.get("http_status") if ok else None,
                result.get("version") or panel.get("version"),
                result.get("cf_colo") or panel.get("cf_colo"),
                result.get("proxy_ip_status") or panel.get("proxy_ip_status"),
                result.get("ech_status") or panel.get("ech_status"),
                result.get("node_count", panel.get("node_count") or 0),
                result.get("cert_expiry") or panel.get("cert_expiry"),
                checked_at,
                error_message[:300] if error_message else None,
                failures,
                int(alerted),
                config_synced,
                checked_at,
                config_error,
                panel["id"],
            ),
        )
    if alert_message:
        notification_send(alert_message)


def run_cloudflare_checks():
    if not cf_check_lock.acquire(blocking=False):
        return
    try:
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM cloudflare_panels WHERE enabled=1 ORDER BY id"
            ).fetchall()
        if rows:
            with ThreadPoolExecutor(max_workers=min(6, len(rows))) as pool:
                list(pool.map(check_cloudflare_one, rows))
    finally:
        cf_check_lock.release()


def serialize_cloudflare_panel(row):
    item = dict(row)
    proxy = get_proxy(item["proxy_id"]) if item.get("proxy_id") else None
    item["proxy"] = serialize_proxy(proxy) if proxy else None
    item["strategy_label"] = {
        "proxy_first": "代理优先，失败后直连",
        "direct_first": "直连优先，失败后代理",
        "proxy_only": "只走代理，失败即断开",
    }.get(item.get("outbound_strategy"), "代理优先，失败后直连")
    return item


def backup_remote_sui(vps):
    # S-UI 1.5.x copies stats and change history in one large SQLite statement.
    # Busy panels can exceed SQLite's variable limit, so automatic safety
    # backups keep the actual configuration while omitting disposable history.
    content = get_sui_client(vps).get_database(exclude="stats,changes")
    folder = DATA_DIR / "sui-backups" / str(vps["id"])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"s-ui-{now_local():%Y%m%d-%H%M%S}.db"
    path.write_bytes(content)
    backups = sorted(folder.glob("s-ui-*.db"), reverse=True)
    for old_backup in backups[20:]:
        old_backup.unlink(missing_ok=True)
    return path


def run_all_checks():
    if not check_lock.acquire(blocking=False):
        return
    try:
        with db_connection() as conn:
            rows = conn.execute("SELECT * FROM vps ORDER BY id").fetchall()
        for vps in rows:
            if monitor_stop.is_set():
                return
            check_one(vps)
        check_expiry_reminders()
    finally:
        check_lock.release()


def monitor_loop():
    last_traffic_run = 0.0
    last_sui_run = 0.0
    last_proxy_run = 0.0
    last_cf_run = 0.0
    while not monitor_stop.is_set():
        try:
            run_all_checks()
            if time.monotonic() - last_traffic_run >= TRAFFIC_CHECK_INTERVAL:
                run_traffic_checks()
                last_traffic_run = time.monotonic()
            if time.monotonic() - last_sui_run >= SUI_CHECK_INTERVAL:
                run_sui_checks()
                last_sui_run = time.monotonic()
            if time.monotonic() - last_proxy_run >= PROXY_CHECK_INTERVAL:
                run_proxy_checks()
                last_proxy_run = time.monotonic()
            if time.monotonic() - last_cf_run >= CF_CHECK_INTERVAL:
                run_cloudflare_checks()
                last_cf_run = time.monotonic()
        except Exception as exc:
            app.logger.exception("监控循环出现异常：%s", exc)
        monitor_stop.wait(CHECK_INTERVAL)


def start_monitor():
    global monitor_thread
    if os.getenv("DISABLE_MONITOR", "0") == "1":
        return
    if monitor_thread is None or not monitor_thread.is_alive():
        monitor_thread = threading.Thread(target=monitor_loop, name="vps-monitor", daemon=True)
        monitor_thread.start()


def serialize_vps(row):
    item = dict(row)
    item["renewal_amount"] = f"{(item.get('renewal_amount_cents') or 0) / 100:.2f}"
    item["amount_display"] = format_amount(
        item.get("renewal_amount_cents") or 0, item.get("currency") or "CNY"
    )
    try:
        item["days_left"] = (date.fromisoformat(item["expiry_date"]) - now_local().date()).days
    except (TypeError, ValueError):
        item["days_left"] = None
    item["traffic_used_gb"] = round((item.get("traffic_used_bytes") or 0) / 1_000_000_000, 2)
    total_gb = item.get("traffic_total_gb")
    item["traffic_remaining_gb"] = (
        round(max(float(total_gb) - item["traffic_used_gb"], 0), 2)
        if total_gb is not None else None
    )
    item["traffic_percent"] = (
        min(round(item["traffic_used_gb"] / float(total_gb) * 100, 1), 100)
        if total_gb and float(total_gb) > 0 else 0
    )
    try:
        item["sui_summary"] = json.loads(item.get("sui_summary_json") or "{}")
    except (TypeError, ValueError):
        item["sui_summary"] = {}
    return item


def credential_cipher():
    secret = os.getenv("CREDENTIAL_KEY", "").strip() or str(app.secret_key)
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_password(password):
    if not password:
        return None
    return credential_cipher().encrypt(password.encode("utf-8")).decode("ascii")


def decrypt_password(encrypted):
    if not encrypted:
        return ""
    try:
        return credential_cipher().decrypt(encrypted.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError):
        return ""


encrypt_secret = encrypt_password
decrypt_secret = decrypt_password


def format_amount(cents, currency):
    currency = currency if currency in CURRENCIES else "CNY"
    return f"{currency} {CURRENCIES[currency]}{(cents or 0) / 100:,.2f}"


def format_bytes(value):
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    index = 0
    while abs(size) >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:,.2f} {units[index]}"


app.jinja_env.filters["bytesfmt"] = format_bytes


def get_sui_client(vps):
    token = decrypt_secret(vps["sui_token_encrypted"])
    if not vps["sui_url"] or not token:
        raise SUIError("未填写 S-UI 地址或 API Token")
    selected_proxy = get_proxy(vps.get("proxy_id")) if vps.get("proxy_id") else None
    return SUIClient(
        vps["sui_url"],
        token,
        bool(vps["sui_verify_tls"]),
        timeout=SUI_TIMEOUT,
        transport_proxy=get_cloudflare_transport_proxy(selected_proxy),
    )


def get_vps(vps_id):
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM vps WHERE id=?", (vps_id,)).fetchone()
    return dict(row) if row else None


def random_client_config(name):
    password = secrets.token_urlsafe(9)
    short_password = secrets.token_urlsafe(12)[:16]
    long_password = secrets.token_urlsafe(24)[:32]
    user_uuid = str(uuid.uuid4())
    return {
        "mixed": {"username": name, "password": password},
        "socks": {"username": name, "password": password},
        "http": {"username": name, "password": password},
        "shadowsocks": {"name": name, "password": long_password},
        "shadowsocks16": {"name": name, "password": short_password},
        "shadowtls": {"name": name, "password": long_password},
        "vmess": {"name": name, "uuid": user_uuid, "alterId": 0},
        "vless": {"name": name, "uuid": user_uuid, "flow": "xtls-rprx-vision"},
        "anytls": {"name": name, "password": password},
        "trojan": {"name": name, "password": password},
        "naive": {"username": name, "password": password},
        "hysteria": {"name": name, "auth_str": password},
        "tuic": {"name": name, "uuid": user_uuid, "password": password},
        "hysteria2": {"name": name, "password": password},
    }


def update_client_config_names(config, name):
    for item in (config or {}).values():
        if isinstance(item, dict):
            if "name" in item:
                item["name"] = name
            if "username" in item:
                item["username"] = name
    return config


SUI_RESOURCES = {
    "clients": "客户端",
    "inbounds": "入站",
    "outbounds": "出站",
    "endpoints": "端点",
    "services": "服务",
    "tls": "TLS 配置",
    "config": "Sing-box 配置",
    "settings": "面板设置",
    "users": "管理员",
    "onlines": "在线对象",
    "stats": "流量统计",
    "logs": "运行日志",
    "changes": "修改记录",
}


SUI_WRITABLE = {"clients", "inbounds", "outbounds", "endpoints", "services", "tls", "config", "settings"}


def sui_save(client, object_name, action, data, init_users=""):
    if object_name not in SUI_WRITABLE or action not in {"new", "edit", "del", "addbulk", "editbulk", "set"}:
        raise ValueError("不允许的 S-UI 修改操作")
    return client.post(
        "save",
        {
            "object": object_name,
            "action": action,
            "data": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            "initUsers": init_users,
        },
    )


SUI_PROXY_TAG = "vps-monitor-socks"


def sui_resource_list(result, key):
    if isinstance(result, dict):
        value = result.get(key, [])
        return value if isinstance(value, list) else []
    return result if isinstance(result, list) else []


def managed_sui_route_rule(inbound_tags):
    return {
        "inbound": inbound_tags,
        "action": "route",
        "outbound": SUI_PROXY_TAG,
    }


def inspect_sui_proxy_sync(vps):
    api = get_sui_client(vps)
    proxy = get_proxy(vps.get("proxy_id")) if vps.get("proxy_id") else None
    outbounds = sui_resource_list(api.get("outbounds") or {}, "outbounds")
    managed = next((item for item in outbounds if item.get("tag") == SUI_PROXY_TAG), None)
    config_result = api.get("config") or {}
    config = config_result.get("config", config_result) if isinstance(config_result, dict) else {}
    rules = ((config.get("route") or {}).get("rules") or []) if isinstance(config, dict) else []
    managed_rule = next(
        (item for item in rules if isinstance(item, dict) and item.get("outbound") == SUI_PROXY_TAG),
        None,
    )
    if not proxy:
        return managed_rule is None
    expected = serialize_proxy(proxy, include_password=True)
    if not managed or not managed_rule:
        return False
    return (
        managed.get("type") == "socks"
        and managed.get("server") == expected.get("host")
        and int(managed.get("server_port") or 0) == int(expected.get("port") or 0)
        and (managed.get("username") or "") == (expected.get("username") or "")
        and (managed.get("password") or "") == (expected.get("password") or "")
    )


def apply_sui_proxy(vps, proxy):
    api = get_sui_client(vps)
    backup_remote_sui(vps)
    outbounds = sui_resource_list(api.get("outbounds") or {}, "outbounds")
    managed = next((item for item in outbounds if item.get("tag") == SUI_PROXY_TAG), None)
    if proxy:
        endpoint = serialize_proxy(proxy, include_password=True)
        outbound = {
            "type": "socks",
            "tag": SUI_PROXY_TAG,
            "server": endpoint["host"],
            "server_port": int(endpoint["port"]),
            "version": "5",
        }
        if endpoint.get("username") or endpoint.get("password"):
            outbound["username"] = endpoint.get("username") or ""
            outbound["password"] = endpoint.get("password") or ""
        if managed and managed.get("id") is not None:
            outbound["id"] = managed["id"]
            sui_save(api, "outbounds", "edit", outbound)
        else:
            sui_save(api, "outbounds", "new", outbound)

    config_result = api.get("config") or {}
    config = config_result.get("config", config_result) if isinstance(config_result, dict) else {}
    if not isinstance(config, dict):
        raise SUIError("S-UI返回的Sing-box基础配置格式不正确")
    route = config.setdefault("route", {})
    rules = route.setdefault("rules", [])
    rules = [
        item for item in rules
        if not (isinstance(item, dict) and item.get("outbound") == SUI_PROXY_TAG)
    ]
    if proxy:
        inbounds = sui_resource_list(api.get("inbounds") or {}, "inbounds")
        inbound_tags = [item.get("tag") for item in inbounds if item.get("tag")]
        if not inbound_tags:
            raise SUIError("这台S-UI没有可绑定的入站节点")
        insert_at = 0
        while insert_at < len(rules) and isinstance(rules[insert_at], dict) and rules[insert_at].get("action") in {"sniff", "hijack-dns"}:
            insert_at += 1
        rules.insert(insert_at, managed_sui_route_rule(inbound_tags))
    route["rules"] = rules
    sui_save(api, "config", "set", config)
    return True


@app.route("/login", methods=["GET", "POST"])
def login():
    if flask_request.method == "POST":
        expected = os.getenv("ADMIN_PASSWORD", "").strip()
        supplied = flask_request.form.get("password", "")
        if expected and hmac.compare_digest(supplied, expected):
            session.clear()
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        flash("密码错误，或服务器没有设置 ADMIN_PASSWORD。", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    with db_connection() as conn:
        rows = conn.execute("SELECT * FROM vps ORDER BY expiry_date, name").fetchall()
    servers = [serialize_vps(row) for row in rows]
    totals = {}
    for server in servers:
        currency = server.get("currency") or "CNY"
        totals[currency] = totals.get(currency, 0) + (server.get("renewal_amount_cents") or 0)
    total_display = " / ".join(
        format_amount(cents, currency)
        for currency, cents in sorted(totals.items())
    ) or format_amount(0, "CNY")
    counts = {
        "all": len(servers),
        "online": sum(v["status"] == "online" for v in servers),
        "offline": sum(v["status"] == "offline" for v in servers),
        "due": sum(v["days_left"] is not None and v["days_left"] <= 3 for v in servers),
        "total_display": total_display,
    }
    return render_template("dashboard.html", servers=servers, counts=counts)


def parse_server_form():
    check_type = flask_request.form.get("check_type", "ping")
    port_text = flask_request.form.get("port", "").strip()
    if check_type not in {"ping", "tcp"}:
        raise ValueError("检测方式不正确")
    port = int(port_text) if port_text else None
    if check_type == "tcp" and (port is None or not 1 <= port <= 65535):
        raise ValueError("TCP 检测必须填写 1-65535 的端口")
    expiry_text = flask_request.form.get("expiry_date", "").strip()
    date.fromisoformat(expiry_text)
    amount_text = flask_request.form.get("renewal_amount", "").strip() or "0"
    try:
        amount = Decimal(amount_text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("续费金额不正确") from exc
    if amount < 0:
        raise ValueError("续费金额不能小于 0")
    currency = flask_request.form.get("currency", "CNY").strip().upper()
    if currency not in CURRENCIES:
        raise ValueError("币种不正确")

    def optional_number(name, label):
        text = flask_request.form.get(name, "").strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError(f"{label}必须是数字") from exc
        if value < 0:
            raise ValueError(f"{label}不能小于 0")
        return value

    cpu_text = flask_request.form.get("cpu_cores", "").strip()
    cpu_cores = int(cpu_text) if cpu_text else None
    if cpu_cores is not None and cpu_cores < 1:
        raise ValueError("CPU 核心数必须大于 0")
    ssh_port_text = flask_request.form.get("ssh_port", "").strip() or "22"
    ssh_port = int(ssh_port_text)
    if not 1 <= ssh_port <= 65535:
        raise ValueError("SSH 端口必须在 1-65535 之间")
    reset_day = int(flask_request.form.get("traffic_reset_day", "1").strip() or "1")
    if not 1 <= reset_day <= 28:
        raise ValueError("流量重置日必须在 1-28 日之间")
    traffic_direction = flask_request.form.get("traffic_direction", "both").strip()
    if traffic_direction not in {"rx", "tx", "both"}:
        raise ValueError("流量计算方式不正确")
    used_gb = optional_number("traffic_used_gb", "已用流量") or 0
    sui_url_text = flask_request.form.get("sui_url", "").strip()
    sui_url = normalize_sui_url(sui_url_text) if sui_url_text else ""
    return {
        "name": flask_request.form.get("name", "").strip(),
        "host": flask_request.form.get("host", "").strip(),
        "check_type": check_type,
        "port": port,
        "provider": flask_request.form.get("provider", "").strip(),
        "expiry_date": expiry_text,
        "renewal_url": flask_request.form.get("renewal_url", "").strip(),
        "renewal_amount_cents": int(amount * 100),
        "currency": currency,
        "traffic_total_gb": optional_number("traffic_total_gb", "总流量"),
        "bandwidth_mbps": optional_number("bandwidth_mbps", "带宽"),
        "traffic_auto": flask_request.form.get("traffic_auto") == "1",
        "traffic_used_bytes": int(used_gb * 1_000_000_000),
        "traffic_reset_day": reset_day,
        "traffic_direction": traffic_direction,
        "memory_gb": optional_number("memory_gb", "内存"),
        "cpu_cores": cpu_cores,
        "disk_gb": optional_number("disk_gb", "硬盘"),
        "login_username": flask_request.form.get("login_username", "").strip(),
        "ssh_port": ssh_port,
        "login_password": flask_request.form.get("login_password", ""),
        "clear_login_password": flask_request.form.get("clear_login_password") == "1",
        "sui_enabled": flask_request.form.get("sui_enabled") == "1",
        "sui_url": sui_url,
        "sui_token": flask_request.form.get("sui_token", "").strip(),
        "clear_sui_token": flask_request.form.get("clear_sui_token") == "1",
        "sui_verify_tls": flask_request.form.get("sui_verify_tls") == "1",
        "notes": flask_request.form.get("notes", "").strip(),
    }


@app.route("/vps/new", methods=["GET", "POST"])
@login_required
def add_vps():
    if flask_request.method == "POST":
        try:
            values = parse_server_form()
            if not values["name"] or not values["host"]:
                raise ValueError("名称和 IP/域名不能为空")
            if values["traffic_auto"]:
                if not values["traffic_total_gb"]:
                    raise ValueError("启用自动流量监控时必须填写套餐总流量")
                if not values["login_username"] or not values["login_password"]:
                    raise ValueError("启用自动流量监控时必须填写 SSH 用户名和密码")
            if values["sui_enabled"] and (not values["sui_url"] or not values["sui_token"]):
                raise ValueError("启用 S-UI 管理时必须填写面板地址和 API Token")
            with db_connection() as conn:
                values["login_password_encrypted"] = encrypt_password(values.pop("login_password"))
                values.pop("clear_login_password", None)
                values["sui_token_encrypted"] = encrypt_secret(values.pop("sui_token"))
                values.pop("clear_sui_token", None)
                conn.execute(
                    """
                    INSERT INTO vps
                    (name, host, check_type, port, provider, expiry_date, renewal_url,
                     renewal_amount_cents, currency, traffic_total_gb, bandwidth_mbps,
                     traffic_auto, traffic_used_bytes, traffic_reset_day, traffic_direction,
                     memory_gb, cpu_cores, disk_gb, login_username, login_password_encrypted,
                     ssh_port, sui_enabled, sui_url, sui_token_encrypted, sui_verify_tls,
                     notes, created_at)
                    VALUES (:name, :host, :check_type, :port, :provider, :expiry_date,
                            :renewal_url, :renewal_amount_cents, :currency, :traffic_total_gb,
                            :bandwidth_mbps, :traffic_auto, :traffic_used_bytes,
                            :traffic_reset_day, :traffic_direction, :memory_gb, :cpu_cores,
                            :disk_gb, :login_username, :login_password_encrypted, :ssh_port,
                            :sui_enabled, :sui_url, :sui_token_encrypted, :sui_verify_tls,
                            :notes, :created_at)
                    """,
                    {**values, "created_at": now_local().isoformat(timespec="seconds")},
                )
            flash("VPS 已添加，系统会在下一轮自动检测。", "success")
            return redirect(url_for("dashboard"))
        except (ValueError, TypeError) as exc:
            flash(str(exc) or "填写内容有误，请检查填写内容。", "error")
    return render_template("form.html", vps=None)


@app.route("/vps/<int:vps_id>/edit", methods=["GET", "POST"])
@login_required
def edit_vps(vps_id):
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM vps WHERE id=?", (vps_id,)).fetchone()
    if row is None:
        flash("找不到这台 VPS。", "error")
        return redirect(url_for("dashboard"))
    if flask_request.method == "POST":
        try:
            values = parse_server_form()
            if not values["name"] or not values["host"]:
                raise ValueError("名称和 IP/域名不能为空")
            if values["traffic_auto"]:
                if not values["traffic_total_gb"]:
                    raise ValueError("启用自动流量监控时必须填写套餐总流量")
                if not values["login_username"]:
                    raise ValueError("启用自动流量监控时必须填写 SSH 用户名")
                if not values["login_password"] and not row["login_password_encrypted"]:
                    raise ValueError("启用自动流量监控时必须填写 SSH 密码")
            if values["sui_enabled"]:
                if not values["sui_url"]:
                    raise ValueError("启用 S-UI 管理时必须填写面板地址")
                if not values["sui_token"] and not row["sui_token_encrypted"]:
                    raise ValueError("启用 S-UI 管理时必须填写 API Token")
            with db_connection() as conn:
                password = values.pop("login_password")
                clear_password = values.pop("clear_login_password")
                if clear_password:
                    values["login_password_encrypted"] = None
                elif password:
                    values["login_password_encrypted"] = encrypt_password(password)
                else:
                    values["login_password_encrypted"] = row["login_password_encrypted"]
                sui_token = values.pop("sui_token")
                clear_sui_token = values.pop("clear_sui_token")
                if clear_sui_token:
                    values["sui_token_encrypted"] = None
                elif sui_token:
                    values["sui_token_encrypted"] = encrypt_secret(sui_token)
                else:
                    values["sui_token_encrypted"] = row["sui_token_encrypted"]
                sui_changed = (
                    values["sui_url"] != (row["sui_url"] or "")
                    or bool(values["sui_enabled"]) != bool(row["sui_enabled"])
                    or bool(values["sui_verify_tls"]) != bool(row["sui_verify_tls"])
                    or bool(sui_token)
                    or clear_sui_token
                )
                values["sui_summary_json"] = None if sui_changed else row["sui_summary_json"]
                values["sui_error"] = None if sui_changed else row["sui_error"]
                values["sui_alerted"] = 0 if sui_changed or not values["sui_enabled"] else row["sui_alerted"]
                connection_changed = (
                    values["host"] != row["host"]
                    or values["login_username"] != (row["login_username"] or "")
                    or values["ssh_port"] != (row["ssh_port"] or 22)
                    or values["traffic_direction"] != (row["traffic_direction"] or "both")
                    or bool(values["traffic_auto"]) != bool(row["traffic_auto"])
                    or bool(password)
                    or clear_password
                )
                values["traffic_baseline_rx_bytes"] = (
                    None if connection_changed or not values["traffic_auto"]
                    else row["traffic_baseline_rx_bytes"]
                )
                values["traffic_baseline_tx_bytes"] = (
                    None if connection_changed or not values["traffic_auto"]
                    else row["traffic_baseline_tx_bytes"]
                )
                values["traffic_cycle"] = current_traffic_cycle(values["traffic_reset_day"])
                traffic_settings_changed = (
                    values["traffic_total_gb"] != row["traffic_total_gb"]
                    or values["traffic_used_bytes"] != row["traffic_used_bytes"]
                    or values["traffic_reset_day"] != row["traffic_reset_day"]
                    or connection_changed
                )
                values["traffic_alert_level"] = (
                    0 if traffic_settings_changed or not values["traffic_auto"]
                    else row["traffic_alert_level"]
                )
                conn.execute(
                    """
                    UPDATE vps SET name=:name, host=:host, check_type=:check_type,
                    port=:port, provider=:provider,
                    expiry_notice_for=CASE WHEN expiry_date <> :expiry_date THEN NULL ELSE expiry_notice_for END,
                    expiry_date=:expiry_date, renewal_url=:renewal_url,
                    renewal_amount_cents=:renewal_amount_cents, currency=:currency,
                    traffic_total_gb=:traffic_total_gb, bandwidth_mbps=:bandwidth_mbps,
                    traffic_auto=:traffic_auto, traffic_used_bytes=:traffic_used_bytes,
                    traffic_reset_day=:traffic_reset_day, traffic_direction=:traffic_direction,
                    traffic_baseline_rx_bytes=:traffic_baseline_rx_bytes,
                    traffic_baseline_tx_bytes=:traffic_baseline_tx_bytes,
                    traffic_cycle=:traffic_cycle, traffic_alert_level=:traffic_alert_level,
                    memory_gb=:memory_gb, cpu_cores=:cpu_cores, disk_gb=:disk_gb,
                    login_username=:login_username,
                    login_password_encrypted=:login_password_encrypted,
                    ssh_port=:ssh_port,
                    sui_enabled=:sui_enabled, sui_url=:sui_url,
                    sui_token_encrypted=:sui_token_encrypted,
                    sui_verify_tls=:sui_verify_tls,
                    sui_summary_json=:sui_summary_json,
                    sui_error=:sui_error, sui_alerted=:sui_alerted,
                    notes=:notes
                    WHERE id=:id
                    """,
                    {**values, "id": vps_id},
                )
            flash("资料已保存。续费后只需在这里修改新的到期日期。", "success")
            return redirect(url_for("dashboard"))
        except (ValueError, TypeError) as exc:
            flash(str(exc) or "填写内容有误，请检查填写内容。", "error")
    return render_template("form.html", vps=dict(row))


@app.get("/vps/<int:vps_id>")
@login_required
def vps_detail(vps_id):
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM vps WHERE id=?", (vps_id,)).fetchone()
    if row is None:
        flash("找不到这台 VPS。", "error")
        return redirect(url_for("dashboard"))
    return render_template("detail.html", vps=serialize_vps(row))


@app.get("/sui")
@login_required
def sui_overview():
    with db_connection() as conn:
        rows = conn.execute("SELECT * FROM vps ORDER BY name").fetchall()
    servers = [serialize_vps(row) for row in rows]
    enabled = [v for v in servers if v.get("sui_enabled")]
    counts = {
        "enabled": len(enabled),
        "healthy": sum(not v.get("sui_error") for v in enabled),
        "errors": sum(bool(v.get("sui_error")) for v in enabled),
        "online_users": sum(len((v.get("sui_summary") or {}).get("onlines", {}).get("user", []) or []) for v in enabled),
    }
    return render_template("sui_overview.html", servers=servers, counts=counts)


@app.post("/sui/check-now")
@login_required
def sui_check_now():
    threading.Thread(target=run_sui_checks, daemon=True).start()
    flash("已开始刷新所有 S-UI 节点，稍后刷新页面查看。", "success")
    return redirect(url_for("sui_overview"))


@app.get("/vps/<int:vps_id>/sui")
@login_required
def sui_detail(vps_id):
    vps = get_vps(vps_id)
    if not vps:
        flash("找不到这台 VPS。", "error")
        return redirect(url_for("sui_overview"))
    if not vps.get("sui_enabled"):
        flash("请先在编辑 VPS 页面启用 S-UI 管理。", "error")
        return redirect(url_for("edit_vps", vps_id=vps_id))
    data, errors = {}, []
    try:
        client = get_sui_client(vps)
        calls = {
            "status": ("status", {"r": "cpu,mem,net,sys,sbd,dsk,swp,dio,db"}),
            "onlines": ("onlines", None),
            "clients": ("clients", None),
            "inbounds": ("inbounds", None),
            "outbounds": ("outbounds", None),
            "endpoints": ("endpoints", None),
            "services": ("services", None),
        }
        for key, (action, params) in calls.items():
            try:
                data[key] = client.get(action, params)
            except SUIError as exc:
                errors.append(f"{SUI_RESOURCES.get(key, key)}：{exc}")
                data[key] = {}
    except SUIError as exc:
        errors.append(str(exc))
    folder = DATA_DIR / "sui-backups" / str(vps_id)
    backups = sorted(folder.glob("s-ui-*.db"), reverse=True)[:20] if folder.exists() else []
    return render_template(
        "sui_detail.html", vps=serialize_vps(vps), data=data, errors=errors,
        backups=[{"name": p.name, "size": p.stat().st_size} for p in backups],
    )


@app.get("/vps/<int:vps_id>/sui/resource/<resource>")
@login_required
def sui_resource(vps_id, resource):
    vps = get_vps(vps_id)
    if not vps or resource not in SUI_RESOURCES:
        flash("找不到该 S-UI 资源。", "error")
        return redirect(url_for("sui_overview"))
    params = {}
    if resource == "logs":
        params = {"c": flask_request.args.get("count", "200"), "l": flask_request.args.get("level", "debug")}
    elif resource == "changes":
        params = {"a": flask_request.args.get("actor", ""), "k": flask_request.args.get("key", ""), "c": flask_request.args.get("count", "200")}
    elif resource == "stats":
        params = {k: flask_request.args.get(k, "") for k in ("resource", "tag", "limit", "start", "end")}
    elif resource in {"clients", "inbounds"} and flask_request.args.get("id"):
        params = {"id": flask_request.args["id"]}
    try:
        api = get_sui_client(vps)
        data = api.get(resource, params)
        online_users = []
        if resource == "clients":
            online_result = api.get("onlines") or {}
            online_users = online_result.get("user", []) if isinstance(online_result, dict) else []
        error_message = ""
    except SUIError as exc:
        data, online_users, error_message = {}, [], str(exc)
    editable_data = data.get(resource, data) if isinstance(data, dict) else data
    return render_template(
        "sui_resource.html", vps=serialize_vps(vps), resource=resource,
        resource_label=SUI_RESOURCES[resource], data=data,
        pretty=json.dumps(data, ensure_ascii=False, indent=2), error_message=error_message,
        editable_pretty=json.dumps(editable_data, ensure_ascii=False, indent=2),
        writable=resource in SUI_WRITABLE, online_users=online_users,
    )


@app.post("/vps/<int:vps_id>/sui/save")
@login_required
def sui_generic_save(vps_id):
    vps = get_vps(vps_id)
    if not vps:
        flash("找不到这台 VPS。", "error")
        return redirect(url_for("sui_overview"))
    object_name = flask_request.form.get("object", "")
    action = flask_request.form.get("action", "edit")
    try:
        data = json.loads(flask_request.form.get("data", ""))
        backup_remote_sui(vps)
        sui_save(get_sui_client(vps), object_name, action, data, flask_request.form.get("init_users", ""))
        flash(f"S-UI {SUI_RESOURCES.get(object_name, object_name)}已保存，并已自动备份原数据库。", "success")
    except (ValueError, TypeError, json.JSONDecodeError, SUIError, OSError) as exc:
        flash(f"保存失败：{exc}", "error")
    return redirect(url_for("sui_resource", vps_id=vps_id, resource=object_name if object_name in SUI_RESOURCES else "config"))


@app.route("/vps/<int:vps_id>/sui/client/new", methods=["GET", "POST"])
@app.route("/vps/<int:vps_id>/sui/client/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
def sui_client_form(vps_id, client_id=None):
    vps = get_vps(vps_id)
    if not vps:
        flash("找不到这台 VPS。", "error")
        return redirect(url_for("sui_overview"))
    try:
        api = get_sui_client(vps)
        inbound_result = api.get("inbounds") or {}
        inbounds = inbound_result.get("inbounds", []) if isinstance(inbound_result, dict) else []
        client_data = None
        if client_id:
            result = api.get("clients", {"id": str(client_id)}) or {}
            items = result.get("clients", []) if isinstance(result, dict) else []
            client_data = items[0] if items else None
            if not client_data:
                raise ValueError("找不到该客户端")
            if client_data.get("expiry"):
                client_data["expiry_date"] = datetime.fromtimestamp(
                    int(client_data["expiry"]), TIMEZONE
                ).date().isoformat()
        if flask_request.method == "POST":
            name = flask_request.form.get("name", "").strip()
            if not name:
                raise ValueError("客户端名称不能为空")
            selected = [int(value) for value in flask_request.form.getlist("inbounds")]
            volume_gb = float(flask_request.form.get("volume_gb", "0") or 0)
            expiry_text = flask_request.form.get("expiry", "").strip()
            expiry = int(datetime.fromisoformat(expiry_text + "T23:59:59").replace(tzinfo=TIMEZONE).timestamp()) if expiry_text else 0
            if client_data:
                payload = dict(client_data)
                config = update_client_config_names(payload.get("config") or {}, name)
            else:
                payload = {"up": 0, "down": 0, "totalUp": 0, "totalDown": 0, "links": []}
                config = random_client_config(name)
            payload.update({
                "enable": flask_request.form.get("enable") == "1", "name": name,
                "config": config, "inbounds": selected,
                "volume": int(volume_gb * 1024 ** 3), "expiry": expiry,
                "desc": flask_request.form.get("desc", "").strip(),
                "group": flask_request.form.get("group", "").strip(),
                "remark": flask_request.form.get("remark", "").strip(),
                "delayStart": flask_request.form.get("delay_start") == "1",
                "autoReset": flask_request.form.get("auto_reset") == "1",
                "resetDays": int(flask_request.form.get("reset_days", "0") or 0),
            })
            if client_id:
                payload["id"] = client_id
            backup_remote_sui(vps)
            sui_save(api, "clients", "edit" if client_id else "new", payload)
            flash("S-UI 客户端已保存，并已自动备份原数据库。", "success")
            return redirect(url_for("sui_resource", vps_id=vps_id, resource="clients"))
        return render_template("sui_client_form.html", vps=serialize_vps(vps), client=client_data, inbounds=inbounds)
    except (SUIError, ValueError, TypeError) as exc:
        flash(f"S-UI 客户端操作失败：{exc}", "error")
        return redirect(url_for("sui_detail", vps_id=vps_id))


@app.post("/vps/<int:vps_id>/sui/client/<int:client_id>/delete")
@login_required
def sui_client_delete(vps_id, client_id):
    vps = get_vps(vps_id)
    try:
        if not vps:
            raise ValueError("找不到这台 VPS")
        backup_remote_sui(vps)
        sui_save(get_sui_client(vps), "clients", "del", client_id)
        flash("客户端已删除，删除前的数据库已备份。", "success")
    except (ValueError, SUIError, OSError) as exc:
        flash(f"删除失败：{exc}", "error")
    return redirect(url_for("sui_resource", vps_id=vps_id, resource="clients"))


@app.post("/vps/<int:vps_id>/sui/action/<action>")
@login_required
def sui_action(vps_id, action):
    labels = {"restartSb": "Sing-box 核心已重启", "restartApp": "S-UI 面板已重启", "resetTraffic": "全部客户端流量已清零"}
    vps = get_vps(vps_id)
    try:
        if not vps or action not in labels:
            raise ValueError("不允许的操作")
        if action == "resetTraffic":
            backup_remote_sui(vps)
        get_sui_client(vps).post(action)
        flash(labels[action], "success")
    except (ValueError, SUIError, OSError) as exc:
        flash(f"操作失败：{exc}", "error")
    return redirect(url_for("sui_detail", vps_id=vps_id))


@app.get("/vps/<int:vps_id>/sui/backup")
@login_required
def sui_backup(vps_id):
    vps = get_vps(vps_id)
    try:
        if not vps:
            raise ValueError("找不到这台 VPS")
        path = backup_remote_sui(vps)
        return send_file(path, as_attachment=True, download_name=path.name)
    except (ValueError, SUIError, OSError) as exc:
        flash(f"备份失败：{exc}", "error")
        return redirect(url_for("sui_detail", vps_id=vps_id))


@app.get("/vps/<int:vps_id>/sui/local-backup/<name>")
@login_required
def sui_local_backup(vps_id, name):
    safe_name = Path(name).name
    path = DATA_DIR / "sui-backups" / str(vps_id) / safe_name
    if not safe_name.startswith("s-ui-") or not path.is_file():
        flash("找不到该备份文件。", "error")
        return redirect(url_for("sui_detail", vps_id=vps_id))
    return send_file(path, as_attachment=True, download_name=safe_name)


@app.post("/vps/<int:vps_id>/sui/restore")
@login_required
def sui_restore(vps_id):
    vps = get_vps(vps_id)
    uploaded = flask_request.files.get("database")
    try:
        if not vps or not uploaded or not uploaded.filename:
            raise ValueError("请选择 S-UI 数据库文件")
        if not uploaded.filename.lower().endswith(".db"):
            raise ValueError("只允许上传 .db 数据库文件")
        content = uploaded.read(100 * 1024 * 1024 + 1)
        if not content or len(content) > 100 * 1024 * 1024:
            raise ValueError("数据库为空或超过 100MB")
        backup_remote_sui(vps)
        api = get_sui_client(vps)
        api.import_database(content, Path(uploaded.filename).name)
        api.post("restartApp")
        flash("数据库已恢复；恢复前的原数据库也已自动备份。", "success")
    except (ValueError, SUIError, OSError) as exc:
        flash(f"恢复失败：{exc}", "error")
    return redirect(url_for("sui_detail", vps_id=vps_id))


@app.route("/vps/<int:vps_id>/sui/tools", methods=["GET", "POST"])
@login_required
def sui_tools(vps_id):
    vps = get_vps(vps_id)
    result = None
    if not vps:
        flash("找不到这台 VPS。", "error")
        return redirect(url_for("sui_overview"))
    if flask_request.method == "POST":
        operation = flask_request.form.get("operation", "")
        try:
            api = get_sui_client(vps)
            if operation == "keypairs":
                result = api.get("keypairs", {"k": flask_request.form.get("key_type", "reality"), "o": flask_request.form.get("options", "")})
            elif operation == "checkOutbound":
                result = api.get("checkOutbound", {"tag": flask_request.form.get("tag", "")})
            elif operation in {"linkConvert", "subConvert"}:
                result = api.post(operation, {"link": flask_request.form.get("link", "")})
            else:
                raise ValueError("请选择工具")
        except (ValueError, SUIError) as exc:
            flash(f"工具执行失败：{exc}", "error")
    return render_template("sui_tools.html", vps=serialize_vps(vps), result=result, pretty=json.dumps(result, ensure_ascii=False, indent=2) if result is not None else "")


@app.post("/vps/<int:vps_id>/password")
@login_required
def reveal_vps_password(vps_id):
    with db_connection() as conn:
        row = conn.execute(
            "SELECT login_password_encrypted FROM vps WHERE id=?", (vps_id,)
        ).fetchone()
    if row is None:
        return {"ok": False, "message": "找不到这台 VPS"}, 404
    password = decrypt_password(row["login_password_encrypted"])
    if not password and row["login_password_encrypted"]:
        return {"ok": False, "message": "密码无法解密，请重新保存"}, 400
    return {"ok": True, "password": password}


def parse_proxy_form():
    port_text = flask_request.form.get("port", "").strip()
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("SOCKS5端口必须是数字") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SOCKS5端口必须在1-65535之间")
    values = {
        "name": flask_request.form.get("name", "").strip(),
        "host": flask_request.form.get("host", "").strip(),
        "port": port,
        "username": flask_request.form.get("username", "").strip(),
        "password": flask_request.form.get("password", ""),
        "clear_password": flask_request.form.get("clear_password") == "1",
        "enabled": flask_request.form.get("enabled") == "1",
        "notes": flask_request.form.get("notes", "").strip(),
    }
    if not values["name"] or not values["host"]:
        raise ValueError("出口名称和服务器地址不能为空")
    return values


@app.get("/proxy-nodes")
@login_required
def proxy_nodes():
    with db_connection() as conn:
        proxy_rows = conn.execute("SELECT * FROM proxy_endpoints ORDER BY name").fetchall()
        sui_rows = conn.execute("SELECT * FROM vps WHERE sui_enabled=1 ORDER BY name").fetchall()
        cf_rows = conn.execute("SELECT * FROM cloudflare_panels ORDER BY name").fetchall()
    proxies = [serialize_proxy(row) for row in proxy_rows]
    sui_nodes = []
    for row in sui_rows:
        node = serialize_vps(row)
        proxy = get_proxy(node["proxy_id"]) if node.get("proxy_id") else None
        node["proxy"] = serialize_proxy(proxy) if proxy else None
        sui_nodes.append(node)
    cloudflare_nodes = [serialize_cloudflare_panel(row) for row in cf_rows]
    counts = {
        "nodes": len(sui_nodes) + len(cloudflare_nodes),
        "sui": len(sui_nodes),
        "cloudflare": len(cloudflare_nodes),
        "proxies": len(proxies),
        "proxy_online": sum(item["status"] == "online" for item in proxies),
    }
    return render_template(
        "proxy_nodes.html", proxies=proxies, sui_nodes=sui_nodes,
        cloudflare_nodes=cloudflare_nodes, counts=counts,
    )


@app.route("/proxy-endpoints/new", methods=["GET", "POST"])
@login_required
def add_proxy_endpoint():
    if flask_request.method == "POST":
        try:
            values = parse_proxy_form()
            values["password_encrypted"] = encrypt_secret(values.pop("password"))
            values.pop("clear_password", None)
            with db_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO proxy_endpoints
                    (name, host, port, username, password_encrypted, enabled, notes, created_at)
                    VALUES (:name, :host, :port, :username, :password_encrypted,
                            :enabled, :notes, :created_at)
                    """,
                    {**values, "created_at": now_local().isoformat(timespec="seconds")},
                )
            flash("SOCKS5落地出口已添加，系统会自动检测真实出口IP。", "success")
            return redirect(url_for("proxy_nodes"))
        except (ValueError, TypeError) as exc:
            flash(str(exc), "error")
    return render_template("proxy_form.html", proxy=None)


@app.route("/proxy-endpoints/<int:proxy_id>/edit", methods=["GET", "POST"])
@login_required
def edit_proxy_endpoint(proxy_id):
    proxy = get_proxy(proxy_id)
    if not proxy:
        flash("找不到这个SOCKS5出口。", "error")
        return redirect(url_for("proxy_nodes"))
    if flask_request.method == "POST":
        try:
            values = parse_proxy_form()
            password = values.pop("password")
            clear_password = values.pop("clear_password")
            if clear_password:
                values["password_encrypted"] = None
            elif password:
                values["password_encrypted"] = encrypt_secret(password)
            else:
                values["password_encrypted"] = proxy.get("password_encrypted")
            with db_connection() as conn:
                conn.execute(
                    """
                    UPDATE proxy_endpoints SET name=:name, host=:host, port=:port,
                    username=:username, password_encrypted=:password_encrypted,
                    enabled=:enabled, notes=:notes, status='unknown', error=NULL,
                    consecutive_failures=0, alerted=0 WHERE id=:id
                    """,
                    {**values, "id": proxy_id},
                )
                conn.execute(
                    "UPDATE cloudflare_panels SET config_synced=0 WHERE proxy_id=?",
                    (proxy_id,),
                )
                conn.execute(
                    "UPDATE vps SET proxy_config_synced=0 WHERE proxy_id=?",
                    (proxy_id,),
                )
            flash("SOCKS5出口已保存；使用它的节点会显示“待重新应用”。", "success")
            return redirect(url_for("proxy_nodes"))
        except (ValueError, TypeError) as exc:
            flash(str(exc), "error")
    return render_template("proxy_form.html", proxy=proxy)


@app.post("/proxy-endpoints/<int:proxy_id>/secret")
@login_required
def reveal_proxy_endpoint(proxy_id):
    proxy = get_proxy(proxy_id)
    if not proxy:
        return {"ok": False, "message": "找不到这个SOCKS5出口"}, 404
    item = serialize_proxy(proxy, include_password=True)
    return {
        "ok": True,
        "host": item["host"], "port": item["port"],
        "username": item.get("username") or "", "password": item.get("password") or "",
        "uri": item["uri"], "cfnew_value": item["cfnew_value"],
    }


@app.post("/proxy-endpoints/<int:proxy_id>/delete")
@login_required
def delete_proxy_endpoint(proxy_id):
    with db_connection() as conn:
        used_cf = conn.execute(
            "SELECT COUNT(*) FROM cloudflare_panels WHERE proxy_id=?", (proxy_id,)
        ).fetchone()[0]
        used_sui = conn.execute(
            "SELECT COUNT(*) FROM vps WHERE proxy_id=?", (proxy_id,)
        ).fetchone()[0]
        if used_cf or used_sui:
            flash(f"这个出口仍被{used_cf + used_sui}个代理节点使用，请先解除绑定。", "error")
            return redirect(url_for("proxy_nodes"))
        conn.execute("DELETE FROM proxy_endpoints WHERE id=?", (proxy_id,))
    flash("SOCKS5落地出口已删除。", "success")
    return redirect(url_for("proxy_nodes"))


@app.post("/proxy-endpoints/check-now")
@login_required
def check_proxy_endpoints_now():
    threading.Thread(target=run_proxy_checks, daemon=True).start()
    flash("已开始检测全部SOCKS5出口，稍后刷新查看结果。", "success")
    return redirect(url_for("proxy_nodes"))


def parse_cloudflare_form():
    panel_type = flask_request.form.get("panel_type", "worker").strip()
    panel_engine_value = flask_request.form.get("panel_engine", "cfnew").strip().lower()
    if panel_engine_value not in {"cfnew", "edgetunnel"}:
        raise ValueError("面板程序类型不正确")
    if panel_type not in {"worker", "pages"}:
        raise ValueError("Cloudflare类型不正确")
    strategy = flask_request.form.get("outbound_strategy", "proxy_first").strip()
    if strategy not in {"proxy_first", "direct_first", "proxy_only"}:
        raise ValueError("出口策略不正确")
    proxy_text = flask_request.form.get("proxy_id", "").strip()
    proxy_id = int(proxy_text) if proxy_text else None
    if proxy_id and not get_proxy(proxy_id):
        raise ValueError("选择的SOCKS5出口不存在")
    values = {
        "name": flask_request.form.get("name", "").strip(),
        "panel_type": panel_type,
        "panel_engine": panel_engine_value,
        "panel_url": normalize_panel_url(flask_request.form.get("panel_url", "")),
        "admin_password": flask_request.form.get("admin_password", "").strip(),
        "proxy_id": proxy_id,
        "outbound_strategy": strategy,
        "enabled": flask_request.form.get("enabled") == "1",
        "verify_tls": flask_request.form.get("verify_tls") == "1",
        "notes": flask_request.form.get("notes", "").strip(),
    }
    if not values["name"]:
        raise ValueError("代理节点名称不能为空")
    return values


@app.route("/cloudflare/new", methods=["GET", "POST"])
@login_required
def add_cloudflare_panel():
    if flask_request.method == "POST":
        try:
            values = parse_cloudflare_form()
            values["admin_password_encrypted"] = encrypt_secret(
                values.pop("admin_password")
            )
            with db_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO cloudflare_panels
                    (name, panel_type, panel_engine, panel_url, admin_password_encrypted,
                     proxy_id, outbound_strategy, enabled, verify_tls, notes, created_at)
                    VALUES (:name, :panel_type, :panel_engine, :panel_url,
                            :admin_password_encrypted, :proxy_id, :outbound_strategy,
                            :enabled, :verify_tls, :notes, :created_at)
                    """,
                    {**values, "created_at": now_local().isoformat(timespec="seconds")},
                )
            flash("Cloudflare代理节点已接入。选择出口后可一键应用。", "success")
            return redirect(url_for("proxy_nodes"))
        except (ValueError, TypeError) as exc:
            flash(str(exc), "error")
    with db_connection() as conn:
        proxies = [serialize_proxy(row) for row in conn.execute("SELECT * FROM proxy_endpoints ORDER BY name").fetchall()]
    return render_template("cloudflare_form.html", panel=None, proxies=proxies)


@app.route("/cloudflare/<int:panel_id>/edit", methods=["GET", "POST"])
@login_required
def edit_cloudflare_panel(panel_id):
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM cloudflare_panels WHERE id=?", (panel_id,)).fetchone()
    if not row:
        flash("找不到这个Cloudflare代理节点。", "error")
        return redirect(url_for("proxy_nodes"))
    panel = dict(row)
    if flask_request.method == "POST":
        try:
            values = parse_cloudflare_form()
            admin_password = values.pop("admin_password")
            values["admin_password_encrypted"] = (
                encrypt_secret(admin_password)
                if admin_password
                else panel.get("admin_password_encrypted")
            )
            with db_connection() as conn:
                conn.execute(
                    """
                    UPDATE cloudflare_panels SET name=:name, panel_type=:panel_type,
                    panel_engine=:panel_engine, panel_url=:panel_url,
                    admin_password_encrypted=:admin_password_encrypted, proxy_id=:proxy_id,
                    outbound_strategy=:outbound_strategy, enabled=:enabled,
                    verify_tls=:verify_tls, notes=:notes, config_synced=0,
                    config_error=NULL WHERE id=:id
                    """,
                    {**values, "id": panel_id},
                )
            flash("Cloudflare代理节点资料已保存，请点击“应用出口”。", "success")
            return redirect(url_for("proxy_nodes"))
        except (ValueError, TypeError) as exc:
            flash(str(exc), "error")
    with db_connection() as conn:
        proxies = [serialize_proxy(row) for row in conn.execute("SELECT * FROM proxy_endpoints ORDER BY name").fetchall()]
    return render_template("cloudflare_form.html", panel=panel, proxies=proxies)


def apply_cloudflare_proxy(panel, proxy):
    panel = prepare_cloudflare_panel(panel)
    value, qj = proxy_config_values(proxy, panel.get("outbound_strategy"))
    if panel["panel_engine"] == "edgetunnel" and not panel.get("admin_password") and not proxy:
        result = {"success": True, "s": "", "qj": ""}
    else:
        result = panel_config_request(
            panel,
            method="POST",
            payload={"s": value, "qj": qj},
            timeout=CF_TIMEOUT,
            transport_proxy=get_cloudflare_transport_proxy(proxy),
        )
    with db_connection() as conn:
        conn.execute(
            """
            UPDATE cloudflare_panels SET config_synced=1, config_error=NULL,
            config_last_check=? WHERE id=?
            """,
            (now_local().isoformat(timespec="seconds"), panel["id"]),
        )
    return result


@app.post("/proxy-nodes/cloudflare/<int:panel_id>/bind")
@login_required
def bind_cloudflare_proxy(panel_id):
    proxy_text = flask_request.form.get("proxy_id", "").strip()
    proxy_id = int(proxy_text) if proxy_text else None
    proxy = get_proxy(proxy_id) if proxy_id else None
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM cloudflare_panels WHERE id=?", (panel_id,)).fetchone()
    if not row or (proxy_id and not proxy):
        flash("代理节点或SOCKS5出口不存在。", "error")
        return redirect(url_for("proxy_nodes"))
    panel = dict(row)
    panel["proxy_id"] = proxy_id
    try:
        with db_connection() as conn:
            conn.execute(
                "UPDATE cloudflare_panels SET proxy_id=?, config_synced=0 WHERE id=?",
                (proxy_id, panel_id),
            )
        apply_cloudflare_proxy(panel, proxy)
        flash("出口设置已应用到远程面板；其他配置没有改动。", "success")
    except (RuntimeError, ValueError) as exc:
        with db_connection() as conn:
            conn.execute(
                "UPDATE cloudflare_panels SET config_synced=0, config_error=? WHERE id=?",
                (str(exc)[:300], panel_id),
            )
        flash(f"出口绑定已保存，但应用到远程面板失败：{exc}", "error")
    return redirect(url_for("proxy_nodes"))


@app.post("/proxy-nodes/sui/<int:vps_id>/bind")
@login_required
def bind_sui_proxy(vps_id):
    proxy_text = flask_request.form.get("proxy_id", "").strip()
    proxy_id = int(proxy_text) if proxy_text else None
    proxy = get_proxy(proxy_id) if proxy_id else None
    vps = get_vps(vps_id)
    if not vps or not vps.get("sui_enabled") or (proxy_id and not proxy):
        flash("S-UI节点或SOCKS5出口不存在。", "error")
        return redirect(url_for("proxy_nodes"))
    vps["proxy_id"] = proxy_id
    try:
        with db_connection() as conn:
            conn.execute(
                "UPDATE vps SET proxy_id=?, proxy_config_synced=0 WHERE id=?",
                (proxy_id, vps_id),
            )
        apply_sui_proxy(vps, proxy)
        with db_connection() as conn:
            conn.execute(
                """
                UPDATE vps SET proxy_config_synced=1, proxy_config_error=NULL,
                proxy_config_last_check=? WHERE id=?
                """,
                (now_local().isoformat(timespec="seconds"), vps_id),
            )
        flash("SOCKS5出口已应用到这台S-UI的全部入站节点，并已自动备份。", "success")
    except (SUIError, ValueError, OSError) as exc:
        with db_connection() as conn:
            conn.execute(
                "UPDATE vps SET proxy_config_synced=0, proxy_config_error=? WHERE id=?",
                (str(exc)[:300], vps_id),
            )
        flash(f"出口绑定已保存，但应用到S-UI失败：{exc}", "error")
    return redirect(url_for("proxy_nodes"))


@app.post("/cloudflare/<int:panel_id>/delete")
@login_required
def delete_cloudflare_panel(panel_id):
    with db_connection() as conn:
        conn.execute("DELETE FROM cloudflare_panels WHERE id=?", (panel_id,))
    flash("Cloudflare代理节点记录已删除；远端CFnew配置没有被修改。", "success")
    return redirect(url_for("proxy_nodes"))


@app.post("/proxy-nodes/check-now")
@login_required
def check_proxy_nodes_now():
    def checks():
        run_proxy_checks()
        run_cloudflare_checks()
        run_sui_checks()
    threading.Thread(target=checks, daemon=True).start()
    flash("已开始检测全部代理节点和SOCKS5出口。", "success")
    return redirect(url_for("proxy_nodes"))


@app.post("/vps/<int:vps_id>/delete")
@login_required
def delete_vps(vps_id):
    with db_connection() as conn:
        conn.execute("DELETE FROM vps WHERE id=?", (vps_id,))
    flash("VPS 记录已删除。", "success")
    return redirect(url_for("dashboard"))


@app.post("/check-now")
@login_required
def check_now():
    def manual_checks():
        run_all_checks()
        run_traffic_checks()
        run_sui_checks()
        run_proxy_checks()
        run_cloudflare_checks()

    threading.Thread(target=manual_checks, daemon=True).start()
    flash("已经开始检测VPS、流量、S-UI、代理节点和SOCKS5出口，稍后刷新查看。", "success")
    return redirect(url_for("dashboard"))


@app.post("/test-telegram")
@login_required
def test_telegram():
    ok, detail = telegram_send(
        f"✅ VPS 监控测试成功\n时间：{now_local():%Y-%m-%d %H:%M:%S}"
    )
    flash("Telegram 测试消息发送成功。" if ok else f"发送失败：{detail}", "success" if ok else "error")
    return redirect(url_for("dashboard"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if flask_request.method == "POST":
        token = flask_request.form.get("telegram_bot_token", "").strip()
        chat_id = flask_request.form.get("telegram_chat_id", "").strip()
        if token:
            set_setting("telegram_bot_token", token)
        if chat_id:
            set_setting("telegram_chat_id", chat_id)
        if not token and not chat_id:
            flash("没有填写任何新内容。", "error")
        else:
            flash("Telegram 设置已保存，请点击“发送测试消息”。", "success")
        return redirect(url_for("settings"))
    return render_template(
        "settings.html",
        token_configured=bool(get_setting("telegram_bot_token", "TELEGRAM_BOT_TOKEN")),
        chat_id=get_setting("telegram_chat_id", "TELEGRAM_CHAT_ID"),
    )


@app.get("/health")
def health():
    return {"status": "ok"}


init_db()
start_monitor()
atexit.register(monitor_stop.set)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
