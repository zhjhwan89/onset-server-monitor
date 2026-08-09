#!/usr/bin/env python3
import ast
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "docker-reports"


def load_secret():
    path = BASE_DIR / "agents.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("DOCKER_AGENT_SECRET="):
            return line.split("=", 1)[1].strip()
    return ""


SECRET = load_secret()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply(200, {"ok": True}) if self.path == "/health" else self.reply(404, {"ok": False})

    def read_request_body(self):
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            body = bytearray()
            while True:
                line = self.rfile.readline(128)
                if not line:
                    raise ValueError("incomplete chunked body")
                size = int(line.split(b";", 1)[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline(8192) not in {b"\r\n", b"\n", b""}:
                        pass
                    break
                if len(body) + size > 2_000_000:
                    raise ValueError("body too large")
                body.extend(self.rfile.read(size))
                self.rfile.read(2)
            return bytes(body)
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 2_000_000:
            raise ValueError("invalid length")
        return self.rfile.read(length)

    @staticmethod
    def decode_report(body):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            report = ast.literal_eval(body.decode("utf-8"))
            if not isinstance(report, dict):
                raise ValueError("invalid report")
            return report

    def do_POST(self):
        if self.path != "/report":
            self.reply(404, {"ok": False})
            return
        if not SECRET or self.headers.get("X-Agent-Token", "") != SECRET:
            self.reply(403, {"ok": False, "error": "forbidden"})
            return
        try:
            report = self.decode_report(self.read_request_body())
            vmid = str(int(report["vmid"]))
            if not isinstance(report.get("containers"), list):
                raise ValueError("invalid containers")
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            target = REPORTS_DIR / f"vm-{re.sub(r'[^0-9]', '', vmid)}.json"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, target)
            self.reply(200, {"ok": True})
        except (KeyError, ValueError, TypeError, UnicodeDecodeError, SyntaxError) as exc:
            self.reply(400, {"ok": False, "error": str(exc)[:120]})


if __name__ == "__main__":
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", 9123), Handler).serve_forever()
