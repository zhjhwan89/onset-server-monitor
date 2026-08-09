import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).with_name("host_monitor.py")
spec = importlib.util.spec_from_file_location("host_monitor", SCRIPT)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class FakeSender:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return True


def sample_status(condition="ok", vms=None):
    containers = [] if condition is None else [
        {
            "name": "test-container",
            "image": "test-image",
            "condition": condition,
            "status": "Up" if condition == "ok" else "Exited",
        }
    ]
    return {
        "updated_at": "2026-08-09T01:00:00+08:00",
        "boot_id": "boot-test",
        "uptime_seconds": 3600,
        "cpu_percent": 10.0,
        "memory_percent": 20.0,
        "disk_percent": 30.0,
        "load_average": [0.1, 0.2, 0.3],
        "temperature_c": 40.0,
        "resource_levels": {"cpu": "ok", "memory": "ok", "disk": "ok"},
        "docker_error": None,
        "containers": containers,
        "pve": {"error": None, "vms": vms or []},
        "issues": [] if condition in {"ok", None} else ["容器 test-container：exited"],
    }


sender = FakeSender()
state = monitor.notify_transitions(
    sender,
    sample_status(),
    {},
    {"cpu": "ok", "memory": "ok", "disk": "ok"},
    {"cpu": 0, "memory": 0},
)
assert len(sender.messages) == 1
assert "监控已启动" in sender.messages[0]

state = monitor.notify_transitions(
    sender,
    sample_status("exited"),
    state,
    {"cpu": "ok", "memory": "ok", "disk": "ok"},
    {"cpu": 0, "memory": 0},
)
assert len(sender.messages) == 2
assert "Docker容器异常" in sender.messages[-1]

state = monitor.notify_transitions(
    sender,
    sample_status("ok"),
    state,
    {"cpu": "ok", "memory": "ok", "disk": "ok"},
    {"cpu": 0, "memory": 0},
)
assert len(sender.messages) == 3
assert "Docker容器已恢复" in sender.messages[-1]

state = monitor.notify_transitions(
    sender,
    sample_status(None),
    state,
    {"cpu": "ok", "memory": "ok", "disk": "ok"},
    {"cpu": 0, "memory": 0},
)
assert len(sender.messages) == 4
assert "Docker容器已删除" in sender.messages[-1]
assert "missing" not in sender.messages[-1]

vm = {
    "id": "qemu:104", "vmid": 104, "name": "测试虚拟机", "type": "qemu",
    "status": "running", "config_signature": "one",
    "config_summary": "2核 / 4GB内存 / 32GB磁盘",
}
state = monitor.notify_transitions(
    sender,
    sample_status(None, [vm]),
    state,
    {"cpu": "ok", "memory": "ok", "disk": "ok"},
    {"cpu": 0, "memory": 0},
)
assert "虚拟机已创建" in sender.messages[-1]

assert monitor.resource_level(95, 90, "ok") == "warning"
assert monitor.resource_level(91, 80, "ok", 90) == "critical"
assert monitor.resource_level(50, 80, "warning", 90) == "ok"

print("Host monitor tests passed.")
