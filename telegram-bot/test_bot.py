import importlib.util
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path


os.environ["BOT_TOKEN"] = "test-token"
os.environ["ALLOWED_CHAT_ID"] = "123456789"

BOT_PATH = Path(__file__).with_name("bot.py")
spec = importlib.util.spec_from_file_location("telegram_menu_bot", BOT_PATH)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

with tempfile.TemporaryDirectory() as temp_dir:
    db_path = Path(temp_dir) / "vps-monitor.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE vps (
            name TEXT, status TEXT, latency_ms REAL, provider TEXT, expiry_date TEXT,
            renewal_amount_cents INTEGER, currency TEXT, sui_enabled INTEGER,
            sui_error TEXT, traffic_auto INTEGER, traffic_used_bytes INTEGER,
            traffic_total_gb REAL, traffic_error TEXT
        );
        CREATE TABLE proxy_endpoints (name TEXT, enabled INTEGER, status TEXT, error TEXT);
        CREATE TABLE cloudflare_panels (name TEXT, enabled INTEGER, status TEXT, error TEXT);
        """
    )
    due_date = (datetime.now(bot.CHINA_TZ).date() + timedelta(days=3)).isoformat()
    conn.execute(
        "INSERT INTO vps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("测试VPS", "offline", None, "测试商家", due_date, 4999, "USD", 1, "S-UI连接失败", 1, 850_000_000, 1.0, None),
    )
    conn.execute("INSERT INTO proxy_endpoints VALUES ('测试SOCKS',1,'offline','连接失败')")
    conn.execute("INSERT INTO cloudflare_panels VALUES ('测试CF',1,'online',NULL)")
    conn.commit()
    conn.close()

    bot.MONITOR_DB = db_path
    host_status = Path(temp_dir) / "host-status.json"
    host_status.write_text(
        '{"updated_at":"2026-08-09T01:00:00+08:00","uptime_seconds":3600,'
        '"cpu_percent":10,"memory_percent":20,"disk_percent":30,'
        '"load_average":[0.1,0.2,0.3],"temperature_c":40,'
        '"system":{"os":"Debian 13","cpu_count":2,"memory_total_bytes":4294967296},'
        '"containers":[{"name":"test-container","condition":"ok","status":"Up"}],'
        '"pve":{"error":null,"nodes":[{"node":"pve","status":"online","cpu_percent":2.5,'
        '"cpu_count":6,"cpu_model":"Intel Test","memory_used_bytes":8589934592,'
        '"memory_total_bytes":34359738368,"root_used_bytes":17179869184,'
        '"root_total_bytes":107374182400,"uptime_seconds":86400,"pve_version":"9.2.5"}],'
        '"vms":[{"vmid":101,"name":"Debian13-Tools","status":"running","cpu_count":2,'
        '"cpu_percent":5,"memory_used_bytes":2147483648,"memory_total_bytes":4294967296,'
        '"disk_total_bytes":34359738368,"uptime_seconds":3600,"onboot":true,'
        '"disks":[{}],"networks":[{}]}]},'
        '"issues":[]}',
        encoding="utf-8",
    )
    bot.HOST_STATUS_PATH = host_status
    assert "1 在线 / 1 离线" not in bot.render_summary()
    assert "0 在线 / 1 离线" in bot.render_summary()
    assert "测试VPS" in bot.render_vps()
    assert "还有 3 天" in bot.render_expiry()
    assert "85.0%" in bot.render_traffic()
    assert "Intel Test" in bot.render_host()
    assert "Debian13-Tools" in bot.render_vms()
    assert "101 Debian13-Tools" in bot.render_docker()
    assert "test-container" in bot.render_docker(101)
    assert "docker_vm:101" in bot.docker_menu_json()
    problems = bot.render_problems()
    assert "VPS离线：测试VPS" in problems
    assert "S-UI异常：测试VPS" in problems
    assert "SOCKS5异常：测试SOCKS" in problems
    assert bot.authorized(123456789)
    assert not bot.authorized(123)

print("Telegram menu bot tests passed.")
