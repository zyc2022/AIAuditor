"""四层持久化记忆：会话共享、用户隔离、可选 Mem0 云端情景记忆。"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha256
from math import log2, sqrt
import json
import os
import re
import threading
from urllib.request import Request, urlopen
from weakref import WeakValueDictionary

from .models import Message, UserIdentity, utc_now
from .storage import AuditStore


def tokens(text: str) -> list[str]:
    parts = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+", text.lower())
    result = []
    for part in parts:
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            result.extend(part[i:i+2] for i in range(max(1, len(part)-1)))
        else:
            result.append(part)
    return result


def similarity(a: str, b: str) -> float:
    # 本地稀疏字符向量；不是语义 embedding，生产语义检索使用 Mem0。
    x, y = Counter(tokens(a)), Counter(tokens(b))
    return sum(v*y.get(k, 0) for k, v in x.items()) / (sqrt(sum(v*v for v in x.values()) * sum(v*v for v in y.values())) or 1)


@dataclass
class EpisodicRecord:
    session_id: str
    summary: str
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)


class EntropyManager:
    def compress(self, messages: list[Message], target: float = 0.35) -> tuple[str, dict]:
        original = "\n".join(f"{m.role}: {m.content}" for m in messages)
        unique = []
        seen = set()
        # 保留最近一次重复内容，避免旧上下文覆盖修正。
        for m in reversed(messages):
            value = re.sub(r"\s+", "", m.content)
            if value in seen:
                continue
            seen.add(value)
            unique.append(m)
        unique.reverse()
        budget = max(1, int(len(original)*(1-target)))
        selected = []
        latest_question = max((i for i, m in enumerate(unique) if m.role == "user"), default=-1)
        used = 0
        if latest_question >= 0:
            question = f"user: {unique[latest_question].content}"
            selected.append((latest_question, question))
            used = len(question)
        for index in range(len(unique)-1, -1, -1):
            m = unique[index]
            line = f"{m.role}: {m.content}"
            if index == latest_question:
                continue
            sentences = re.split(r"(?<=[。！？\n])", line)
            scored = []
            for n, sentence in enumerate(sentences):
                counts = Counter(tokens(sentence))
                total = sum(counts.values()) or 1
                entropy = -sum((c/total)*log2(c/total) for c in counts.values())
                scored.append((entropy + (2 if re.search(r"\d|不得|必须|证据|风险", sentence) else 0), n, sentence))
            chosen = []
            for score, n, sentence in sorted(scored, reverse=True):
                if used + len(sentence) + 1 <= budget:
                    chosen.append((n, sentence))
                    used += len(sentence)+1
            if chosen:
                selected.append((index, "".join(s for n, s in sorted(chosen))))
        compressed = "\n".join(s for i, s in sorted(selected))
        ratio = 1-len(compressed)/len(original) if original else 0
        return compressed, {"input_chars": len(original), "output_chars": len(compressed), "compression_ratio": round(ratio, 4), "target_ratio": target, "messages": len(messages)}


class Mem0Client:
    def __init__(self, key: str, namespace: str, base_url="https://api.mem0.ai", timeout=2.0):
        self.key, self.namespace, self.base_url, self.timeout = key, namespace, base_url.rstrip("/"), timeout

    def _post(self, path: str, payload: dict):
        request = Request(self.base_url + path, data=json.dumps(payload).encode(), headers={"Authorization": "Token " + self.key, "Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def add(self, summary: str, session_id: str, tags: list[str]):
        return self._post("/v1/memories/", {"messages": [{"role": "user", "content": summary}], "user_id": self.namespace, "run_id": session_id, "metadata": {"tags": tags}, "infer": False})

    def search(self, query: str):
        data = self._post("/v2/memories/search/", {"query": query, "filters": {"user_id": self.namespace}, "top_k": 3})
        return data.get("results", []) if isinstance(data, dict) else data


class FourLayerMemory:
    _instances = WeakValueDictionary()
    _guard = threading.RLock()

    def __new__(cls, session_id="default", recent_rounds=5, store=None, identity=None):
        owner = (identity or UserIdentity()).user_id
        path = str((store or AuditStore()).path)
        key = (path, owner, session_id)
        with cls._guard:
            if key not in cls._instances:
                obj = super().__new__(cls)
                obj._initialized = False
                cls._instances[key] = obj
            return cls._instances[key]

    def __init__(self, session_id="default", recent_rounds=5, store=None, identity=None):
        with self._guard:
            if self._initialized:
                return
            self.session_id = session_id
            self.recent_rounds = recent_rounds
            self.store = store or AuditStore()
            self.l0 = identity or UserIdentity()
            self.store.bind_session(session_id, self.l0)
            self.lock = threading.RLock()
            self.warnings = []
            self.entropy = EntropyManager()
            self.last_stats = {}
            key = os.getenv("MEM0_API_KEY")
            namespace = sha256((str(self.store.path) + self.l0.user_id).encode()).hexdigest()
            self.remote = Mem0Client(key, namespace, os.getenv("MEM0_BASE_URL", "https://api.mem0.ai")) if key else None
            self._initialized = True

    def set_identity(self, identity):
        if identity.user_id != self.l0.user_id:
            raise PermissionError("不可变更共享记忆的所有者")
        self.store.bind_session(self.session_id, identity)
        self.l0 = identity

    @property
    def l1(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT role,content,created_at FROM messages WHERE session_id=? ORDER BY id", (self.session_id,))
            return [Message(r[0], r[1], r[2]) for r in rows]

    @property
    def l2(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT session_id,summary,tags,created_at FROM episodes WHERE user_id=? ORDER BY id", (self.l0.user_id,))
            return [EpisodicRecord(r[0], r[1], json.loads(r[2]), r[3]) for r in rows if self._tags_allowed(json.loads(r[2]))]

    def _tags_allowed(self, tags):
        entity_tags = {t[7:] for t in tags if t.startswith("entity:")}
        return entity_tags.issubset(set(self.l0.entity_ids))

    def add_message(self, role, content, agent=None):
        # 工具状态在 task board/trace 中共享，不污染五轮对话的边界。
        if role not in {"user", "assistant"}:
            return
        with self.store.connect() as db:
            db.execute("INSERT INTO messages(session_id,role,content) VALUES (?,?,?)", (self.session_id, role, content))

    def working_context(self):
        with self.store.connect() as db:
            starts = db.execute("SELECT id FROM messages WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT ?", (self.session_id, self.recent_rounds)).fetchall()
            if not starts:
                return []
            rows = db.execute("SELECT role,content,created_at FROM messages WHERE session_id=? AND id>=? ORDER BY id", (self.session_id, min(r[0] for r in starts))).fetchall()
        return [Message(r[0], r[1], r[2]) for r in rows]

    def compressed_context(self):
        context, self.last_stats = self.entropy.compress(self.working_context())
        return context

    def remember_episode(self, summary, tags=None):
        tags = list(tags or [])
        with self.store.connect() as db:
            db.execute("INSERT INTO episodes(user_id,session_id,summary,tags) VALUES (?,?,?,?)", (self.l0.user_id, self.session_id, summary, json.dumps(tags)))
        if self.remote:
            try:
                self.remote.add(summary, self.session_id, tags)
            except Exception as exc:
                self.warnings.append("Mem0 写入失败，已保存在本地：" + type(exc).__name__)

    def retrieve_episodes(self, query, limit=3):
        ranked = sorted(((similarity(query, r.summary), r) for r in self.l2), key=lambda x: x[0], reverse=True)
        result = [r for score, r in ranked[:limit] if score > 0]
        if self.remote:
            try:
                for record in self.remote.search(query):
                    tags = (record.get("metadata") or {}).get("tags", [])
                    if self._tags_allowed(tags):
                        result.append(EpisodicRecord(str(record.get("run_id", "mem0")), record.get("memory", ""), tags))
            except Exception as exc:
                self.warnings.append("Mem0 检索失败，已使用本地字符向量检索：" + type(exc).__name__)
        return result[:limit]

    def semantic_rules(self, query=""):
        rules = [{"rule": "证据不足时不得把推测写成审计结论", "type": "evidence"}, {"rule": "重复发票号和同日多笔付款只作为线索，需补充原始凭证", "type": "risk"}]
        with self.store.connect() as db:
            for row in db.execute("SELECT rule,support FROM semantic_rules WHERE user_id=?", (self.l0.user_id,)):
                support = json.loads(row[1])
                if self._tags_allowed(support.get("tags", [])):
                    rules.append({"rule": row[0], "type": "distilled", "support": support})
        return rules

    def distill(self, evidence, current_record=None):
        # 同一风险模式在至少两个不同请求中有证据支持后才能进入 L3。
        records = self.store.trajectories(self.l0.user_id, self.l0.entity_ids)
        if current_record:
            records.append(current_record)
        for risk in {e.fields.get("rule") for e in evidence if e.kind == "risk"} - {None}:
            supporting, entities, signatures = [], set(), set()
            for record in records:
                matches = [e for e in record["evidence"] if e.get("fields", {}).get("rule") == risk]
                if matches:
                    supporting.append(record["request_id"])
                    entities.update(e["fields"].get("company_id", "") for e in matches)
                    for evidence_item in matches:
                        signatures.add(tuple(sorted(evidence_item["fields"].get("record_ids", []))))
            if len(supporting) >= 2 and len(signatures) >= 2:
                statement = f"历史核查多次观察到“{risk}”，后续同类审计优先复核原始凭证；该模式不能单独认定违规。"
                key = sha256((self.l0.user_id+str(risk)).encode()).hexdigest()
                support = {"request_ids": supporting, "distinct_cases": len(signatures), "tags": ["entity:"+e for e in sorted(entities) if e]}
                with self.store.connect() as db:
                    db.execute("INSERT OR REPLACE INTO semantic_rules(id,user_id,rule,support) VALUES (?,?,?,?)", (key, self.l0.user_id, statement, json.dumps(support)))
