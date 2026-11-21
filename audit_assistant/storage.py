"""SQLite 演示业务库。查询在数据库层执行只读与企业授权检查。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path
import json
import sqlite3

from .models import UserIdentity

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
 id TEXT PRIMARY KEY, name TEXT, industry TEXT, region TEXT, established TEXT, related_party INTEGER);
CREATE TABLE IF NOT EXISTS contracts (
 id TEXT PRIMARY KEY, company_id TEXT, title TEXT, amount REAL, document_available INTEGER);
CREATE TABLE IF NOT EXISTS transactions (
 id TEXT PRIMARY KEY, company_id TEXT, counterparty TEXT, txn_date TEXT,
 amount REAL, invoice_no TEXT, contract_id TEXT, due_date TEXT);
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, title TEXT, content TEXT, category TEXT, company_id TEXT);
CREATE TABLE IF NOT EXISTS sessions (
 session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, identity_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS episodes (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, session_id TEXT, summary TEXT, tags TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS semantic_rules (
 id TEXT PRIMARY KEY, user_id TEXT, rule TEXT, support TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS trajectories (
 request_id TEXT PRIMARY KEY, user_id TEXT, session_id TEXT, payload TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


class AuditStore:
    def __init__(self, path: str | Path = "data/audit.sqlite3", seed: bool = True):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            if seed and not db.execute("SELECT 1 FROM companies LIMIT 1").fetchone():
                self._seed(db)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _seed(self, db):
        year = date.today().year
        db.executemany("INSERT INTO companies VALUES (?,?,?,?,?,?)", [
            ("C001", "华东供应商", "工业设备", "上海", "2014-06-12", 1),
            ("C002", "远景科技", "软件服务", "杭州", "2018-03-08", 0),
            ("C003", "北方物流", "运输", "北京", "2010-09-21", 0),
        ])
        db.executemany("INSERT INTO contracts VALUES (?,?,?,?,?)", [
            ("HT001", "C001", "设备采购合同", 2200000, 1),
            ("HT002", "C001", "咨询服务合同", 300000, 0),
            ("HT003", "C002", "软件维护合同", 180000, 1),
            ("HT004", "C003", "物流服务合同", 120000, 1),
        ])
        db.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", [
            ("TX001", "C001", "华东供应商", f"{year-2}-06-01", 1200000, "INV001", "HT001", f"{year-2}-05-15"),
            ("TX002", "C001", "华东供应商", f"{year-1}-02-01", 480000, "INV002", "HT001", f"{year-1}-02-15"),
            ("TX003", "C001", "华东供应商", f"{year-1}-02-01", 490000, "INV003", "HT001", f"{year-1}-02-15"),
            ("TX004", "C001", "华东供应商", f"{year}-01-18", 300000, "INV004", "HT002", f"{year}-01-30"),
            ("TX005", "C001", "华东供应商", f"{year}-02-10", 300000, "INV004", "HT002", f"{year}-02-01"),
            ("TX006", "C002", "远景科技", f"{year}-03-05", 80000, "INV006", "HT003", f"{year}-03-10"),
            ("TX007", "C002", "远景科技", f"{year}-06-05", 90000, "INV007", "HT003", f"{year}-06-10"),
            ("TX008", "C003", "北方物流", f"{year}-04-12", 100000, "INV008", "HT004", f"{year}-04-15"),
        ])
        db.executemany("INSERT INTO documents VALUES (?,?,?,?,?)", [
            ("G001", "关联交易审计工作指南（演示）", "识别关联方，核对交易定价依据、公允性及披露完整性。关联方身份本身不构成违规结论。", "guide", None),
            ("G002", "支付取证工作指南（演示）", "核查合同、发票、审批记录、银行流水。重复发票号需核查分次付款和冲销情况。疑似拆分需核对审批阈值与真实业务。", "guide", None),
            ("G003", "报告编制工作指南（演示）", "报告应列出事实、金额、证据来源、局限性与后续程序，缺少证据不得作确定性违规判断。", "guide", None),
            ("D001", "华东供应商走访纪要（合成）", "主体经营工业设备，关联方标识来自演示企业主数据；咨询服务合同 HT002 原件尚未归档。", "case", "C001"),
            ("D002", "远景科技核查记录（合成）", "软件维护服务两笔付款，合同原件可用，需核查服务验收。", "case", "C002"),
        ])

    def bind_session(self, session_id: str, identity: UserIdentity):
        from dataclasses import asdict
        with self.connect() as db:
            old = db.execute("SELECT user_id,identity_json FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if old and old[0] != identity.user_id:
                raise PermissionError("该会话不属于当前审计人员")
            if old:
                previous = json.loads(old[1])
                if set(previous["entity_ids"]) != set(identity.entity_ids) or set(previous["permissions"]) != set(identity.permissions) or previous["role"] != identity.role:
                    raise PermissionError("授权范围已变化，请创建新会话以防旧上下文泄露")
            db.execute("INSERT INTO sessions(session_id,user_id,identity_json) VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET identity_json=excluded.identity_json", (session_id, identity.user_id, json.dumps(asdict(identity), ensure_ascii=False)))

    def companies(self, identity: UserIdentity) -> list[dict]:
        return self.read("SELECT * FROM companies", (), identity)

    def read(self, sql: str, params: tuple, identity: UserIdentity, limit: int = 200) -> list[dict]:
        """仅允许业务表 SELECT；用临时视图在数据库内部实施行级授权。"""
        if "audit.read" not in identity.permissions:
            raise PermissionError("缺少 audit.read 权限")
        clean = sql.strip()
        if not clean.lower().startswith("select ") or ";" in clean.rstrip(";"):
            raise ValueError("只允许单条 SELECT 查询")
        allowed_tables = {"companies", "contracts", "transactions", "documents"}
        with self.connect() as db:
            db.execute("CREATE TEMP TABLE permitted_entities(id TEXT PRIMARY KEY)")
            db.executemany("INSERT OR IGNORE INTO permitted_entities VALUES (?)", [(x,) for x in identity.entity_ids])
            for table in sorted(allowed_tables):
                column = "id" if table == "companies" else "company_id"
                extra = f" OR {column} IS NULL" if table == "documents" else ""
                db.execute(f"CREATE TEMP VIEW {table} AS SELECT * FROM main.{table} WHERE {column} IN (SELECT id FROM permitted_entities){extra}")

            def authorize(action, arg1, arg2, database, trigger):
                if action == sqlite3.SQLITE_SELECT:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION:
                    return sqlite3.SQLITE_OK if (arg2 or "").lower() in {"sum", "count", "avg", "min", "max", "round", "coalesce", "substr", "date", "abs", "lower", "upper"} else sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ:
                    if database == "main" and arg1 in allowed_tables and trigger == arg1:
                        return sqlite3.SQLITE_OK
                    if database == "temp" and arg1 in allowed_tables | {"permitted_entities"}:
                        return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            db.set_authorizer(authorize)
            import time
            deadline = time.monotonic() + 1
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            try:
                return [dict(row) for row in db.execute(clean, params).fetchmany(limit)]
            finally:
                db.set_authorizer(None)
                db.set_progress_handler(None, 0)

    def save_trajectory(self, user_id: str, session_id: str, payload: dict):
        with self.connect() as db:
            db.execute("INSERT INTO trajectories(request_id,user_id,session_id,payload) VALUES (?,?,?,?)", (payload["request_id"], user_id, session_id, json.dumps(payload, ensure_ascii=False)))

    def trajectories(self, user_id: str, entity_ids=None):
        with self.connect() as db:
            records = [json.loads(row[0]) for row in db.execute("SELECT payload FROM trajectories WHERE user_id=? ORDER BY rowid", (user_id,))]
        if entity_ids is not None:
            def permitted(record):
                evidence = record.get("evidence", [])
                scope_id = record.get("scope", {}).get("company_id")
                required = {e.get("fields", {}).get("company_id") for e in evidence} - {None, ""}
                if scope_id:
                    required.add(scope_id)
                if evidence and not required:
                    return False
                return required.issubset(set(entity_ids))
            records = [r for r in records if permitted(r)]
        return records
