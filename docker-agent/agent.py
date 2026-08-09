#!/usr/bin/env python3
import http.client
import json
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


SOCKET_PATH = "/var/run/docker.sock"
COLLECTOR_URL = os.environ["COLLECTOR_URL"].rstrip("/")
AGENT_SECRET = os.environ["DOCKER_AGENT_SECRET"]
VM_ID = int(os.environ["VM_ID"])
VM_NAME = os.environ.get("VM_NAME", socket.gethostname())
INTERVAL = max(30, int(os.environ.get("CHECK_INTERVAL", "30")))
CHINA_TZ = timezone(timedelta(hours=8))


class UnixHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET_PATH)


def docker_get(path):
    connection = UnixHTTPConnection("localhost", timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        raw = response.read()
        if response.status != 200:
            raise RuntimeError(f"Docker API HTTP {response.status}")
        return json.loads(raw)
    finally:
        connection.close()


def containers():
    rows = docker_get("/containers/json?all=1")
    result = []
    for row in rows:
        names = row.get("Names") or []
        name = (names[0].lstrip("/") if names else row.get("Id", "unknown")[:12])
        state = (row.get("State") or "unknown").lower()
        status = row.get("Status") or ""
        lowered = status.lower()
        health = "unhealthy" if "(unhealthy)" in lowered else ("healthy" if "(healthy)" in lowered else "none")
        condition = "ok" if state == "running" and health != "unhealthy" else ("unhealthy" if health == "unhealthy" else state)
        ports = []
        for port in row.get("Ports") or []:
            private = port.get("PrivatePort")
            public = port.get("PublicPort")
            ports.append(f"{public or ''}->{private}/{port.get('Type', 'tcp')}" if public else f"{private}/{port.get('Type', 'tcp')}")
        result.append({"name": name, "image": row.get("Image") or "", "state": state, "health": health, "status": status, "ports": ", ".join(ports), "condition": condition})
    return sorted(result, key=lambda item: item["name"].lower())


def post(report):
    parsed = urlsplit(COLLECTOR_URL)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=12)
    try:
        body = json.dumps(report, ensure_ascii=False).encode("utf-8")
        connection.request("POST", (parsed.path.rstrip("/") or "") + "/report", body=body, headers={"Content-Type": "application/json", "Content-Length": str(len(body)), "X-Agent-Token": AGENT_SECRET})
        response = connection.getresponse()
        response.read()
        if response.status != 200:
            raise RuntimeError(f"上报失败 HTTP {response.status}")
    finally:
        connection.close()


def main():
    while True:
        try:
            report = {"vmid": VM_ID, "vm_name": VM_NAME, "hostname": socket.gethostname(), "updated_at": datetime.now(CHINA_TZ).isoformat(timespec="seconds"), "docker_available": True, "error": None, "containers": containers()}
        except Exception as exc:
            report = {"vmid": VM_ID, "vm_name": VM_NAME, "hostname": socket.gethostname(), "updated_at": datetime.now(CHINA_TZ).isoformat(timespec="seconds"), "docker_available": True, "error": str(exc)[:300], "containers": []}
        try:
            post(report)
        except Exception as exc:
            print(f"Docker状态上报失败：{exc}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
