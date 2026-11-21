"""标准库 HTTP API 与本地演示前端；部署入口默认只监听回环地址。"""
from __future__ import annotations

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import hmac
import json
import os
import threading
import uuid

from .models import UserIdentity
from .orchestrator import AuditAssistant
from .storage import AuditStore
from .trajectories import export_records

STATIC = Path(__file__).with_name("web")


class AuditHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, db_path=None, token=None, identity=None, planner=None, harness_path=None):
        self.token = token if token is not None else os.getenv("AUDIT_API_TOKEN", "")
        if address[0] not in {"127.0.0.1", "localhost", "::1"} and not self.token:
            raise ValueError("监听非回环地址必须设置 AUDIT_API_TOKEN")
        self.store = AuditStore(db_path or os.getenv("AUDIT_DB_PATH", "data/audit.sqlite3"))
        self.identity = identity or UserIdentity(user_id=os.getenv("AUDIT_USER_ID", "demo-auditor"), entity_ids=os.getenv("AUDIT_ENTITY_IDS", "C001,C002,C003").split(","))
        self.planner, self.harness_path = planner, harness_path
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "AuditHarness/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(45)

    def log_message(self, format, *args):
        # 不记录 URL/query/token，避免审计问题与身份信息进入访问日志。
        pass

    def send_bytes(self, data, status=200, content_type="application/json; charset=utf-8", download=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        self.wfile.write(data)

    def json(self, payload, status=200):
        self.send_bytes(json.dumps(payload, ensure_ascii=False).encode(), status)

    def authorize(self):
        expected = self.server.token
        if expected and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer "+expected):
            self.json({"error": "需要有效的 API 访问令牌"}, 401)
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            actual = urlparse("http://"+self.headers.get("Host", ""))
            if (parsed.hostname, parsed.port or 80) != (actual.hostname, actual.port or 80) or parsed.scheme != "http":
                self.json({"error": "拒绝跨站请求"}, 403)
                return False
        # Host 必须与回环监听地址相符，降低本地演示服务 DNS 重绑定风险。
        if self.server.server_address[0].startswith("127.") and urlparse("http://"+self.headers.get("Host", "")).hostname not in {"localhost", "127.0.0.1"}:
            self.json({"error": "无效 Host"}, 403)
            return False
        return True

    def assistant(self, session_id):
        with self.server.store.connect() as db:
            row = db.execute("SELECT user_id FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if not row:
            raise LookupError("会话不存在")
        if row[0] != self.server.identity.user_id:
            raise PermissionError("会话不属于当前用户")
        return AuditAssistant(session_id, identity=self.server.identity, db_path=self.server.store.path, planner=self.server.planner, harness_path=self.server.harness_path)

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            query = parse_qs(urlparse(self.path).query)
            if path in {"/", "/app.js", "/style.css"}:
                name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[path]
                mime = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name]
                return self.send_bytes((STATIC/name).read_bytes(), content_type=mime+"; charset=utf-8")
            if not self.authorize():
                return
            if path == "/api/health":
                return self.json({"status": "ok", "mode": "llm" if os.getenv("AUDIT_MODE") == "llm" else "local-demo", "synthetic_data": True})
            if path == "/api/config":
                return self.json({"companies": self.server.store.companies(self.server.identity), "identity": asdict(self.server.identity), "mode": "llm" if os.getenv("AUDIT_MODE") == "llm" else "local-demo"})
            if path == "/api/memory":
                assistant = self.assistant(query.get("session_id", [""])[0])
                memory = assistant.memory
                context = memory.compressed_context()
                records = [r for r in assistant.store.trajectories(assistant.identity.user_id, assistant.identity.entity_ids) if r["session_id"] == memory.session_id]
                return self.json({"core": asdict(memory.l0), "working": [asdict(m) for m in memory.working_context()], "compressed": context, "compression": memory.last_stats, "episodes": [asdict(r) for r in memory.l2[-5:]], "semantic": memory.semantic_rules(), "last_response": records[-1] if records else None})
            if path == "/api/export":
                names = [c["name"] for c in self.server.store.companies(self.server.identity)]
                format = query.get("format", ["sft"])[0]
                content = export_records(self.server.store.trajectories(self.server.identity.user_id, self.server.identity.entity_ids), format, names)
                return self.send_bytes(content.encode(), content_type="application/x-ndjson; charset=utf-8", download=f"audit-{format}.jsonl")
            if path.startswith("/api/reports/"):
                request_id = path.rsplit("/", 1)[1]
                record = next((r for r in self.server.store.trajectories(self.server.identity.user_id, self.server.identity.entity_ids) if r["request_id"] == request_id), None)
                if not record:
                    raise LookupError("报告不存在")
                return self.send_bytes(record["answer"].encode(), content_type="text/markdown; charset=utf-8", download="audit-report.md")
            self.json({"error": "接口不存在"}, 404)
        except (PermissionError, ValueError, LookupError) as exc:
            self.json({"error": str(exc)}, 403 if isinstance(exc, PermissionError) else 404 if isinstance(exc, LookupError) else 400)
        except Exception:
            self.json({"error": "服务异常，请查看本地配置"}, 500)

    def do_POST(self):
        if not self.authorize():
            return
        try:
            if self.headers.get_content_type() != "application/json":
                return self.json({"error": "只接受 application/json"}, 415)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                return self.json({"error": "请求体必须在 1-65536 字节内"}, 413)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求体必须为 JSON 对象")
            path = urlparse(self.path).path
            if path == "/api/sessions":
                session_id = uuid.uuid4().hex
                self.server.store.bind_session(session_id, self.server.identity)
                return self.json({"session_id": session_id}, 201)
            if path == "/api/chat":
                if not self.server.slots.acquire(blocking=False):
                    return self.json({"error": "当前并发任务已满，请稍后重试"}, 429)
                try:
                    assistant = self.assistant(body.get("session_id", ""))
                    response = assistant.ask(body.get("query", ""), entity=body.get("entity"), since=body.get("since"), until=body.get("until"))
                    return self.json(response.to_dict())
                finally:
                    self.server.slots.release()
            self.json({"error": "接口不存在"}, 404)
        except (PermissionError, ValueError, LookupError, TypeError) as exc:
            self.json({"error": str(exc)}, 403 if isinstance(exc, PermissionError) else 404 if isinstance(exc, LookupError) else 400)
        except Exception:
            self.json({"error": "服务异常，请查看本地配置"}, 500)


def serve(host="127.0.0.1", port=8000, db_path=None, harness_path=None):
    server = AuditHTTPServer((host, port), db_path=db_path, harness_path=harness_path)
    print(f"审计助手：http://{host}:{server.server_address[1]} （Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
