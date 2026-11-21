from __future__ import annotations

from datetime import date
from pathlib import Path
import os
import re
import time

from .agents import build_agents, unique_evidence
from .harness import Harness
from .llm import make_planner
from .memory import FourLayerMemory
from .models import AuditResponse, UserIdentity
from .skills import SkillRegistry
from .storage import AuditStore
from .swarm import AgentSwarm


class AuditAssistant:
    def __init__(self, session_id="default", identity=None, harness_path=None, db_path=None, planner=None, registry=None, seed=True, memory_version=2):
        if not re.fullmatch(r"[\w-]{1,80}", session_id):
            raise ValueError("session_id 只允许 1-80 个字母、数字、下划线或连字符")
        if memory_version not in (1, 2):
            raise ValueError("memory_version 必须为 1 或 2")
        self.identity = identity or UserIdentity()
        self.store = AuditStore(db_path or os.getenv("AUDIT_DB_PATH", "data/audit.sqlite3"), seed=seed)
        self.memory = FourLayerMemory(session_id, store=self.store, identity=self.identity)
        self.memory.set_identity(self.identity)
        self.memory_version = memory_version
        default_yaml = Path(__file__).resolve().parent.parent / "config" / "harness.yaml"
        policy_path = Path(harness_path) if harness_path else default_yaml
        self.harness = Harness.from_yaml(policy_path) if harness_path or policy_path.exists() else Harness()
        self.skills = registry or SkillRegistry()
        for name in self.harness.policy["agents"]:
            unknown = set(self.harness.allowed_skills(name))-set(self.skills.names())
            if unknown:
                raise ValueError("Harness 引用了未注册 Skill："+", ".join(sorted(unknown)))
        self.planner = planner or make_planner()
        self.agents = build_agents(self.skills, self.memory, self.harness, self.planner, self.store)
        self.swarm = AgentSwarm(self.agents)

    def _scope(self, query, entity, since, until):
        companies = self.store.companies(self.identity)
        by_id = {c["id"]: c for c in companies}
        with self.store.connect() as db:
            all_names = list(db.execute("SELECT id,name FROM companies"))
        named = [row[0] for row in all_names if row[1] in query or re.search(r"(?<!\w)"+row[0]+r"(?!\w)", query)]
        if any(code not in by_id for code in named):
            raise PermissionError("问题涉及未授权企业")
        if len(named) > 1:
            raise ValueError("当前请求仅支持一家企业，请分别发起查询")
        if entity and entity not in by_id:
            found = [c["id"] for c in companies if c["name"] == entity]
            if not found:
                raise PermissionError("企业不存在或未授权")
            entity = found[0]
        if entity and named and entity != named[0]:
            raise ValueError("选择的企业与问题中的企业不一致")
        chosen = entity or (named[0] if named else None)
        old_tags = []
        if not chosen:
            # 出现新的未知企业名时不继承之前的企业，以免答错对象。
            unknown_subject = re.search(r"[\u4e00-\u9fffA-Za-z]{2,}(?:公司|供应商|科技|物流)", query)
            if not unknown_subject:
                history = [r for r in self.memory.l2 if r.session_id == self.memory.session_id]
                if history:
                    old_tags = history[-1].tags
                    chosen = next((t[7:] for t in old_tags if t.startswith("entity:") and t[7:] in by_id), None)
        if not chosen:
            return None
        years = re.findall(r"(?<!\d)(20\d{2})(?:年)?", query)
        recent = re.search(r"近([一二两三四五六七八九十\d]+)年", query)
        # 明确请求日期优先，其次继承同会话范围，否则为最近三个自然年度至今。
        today = date.today()
        if recent:
            digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
            count = digits.get(recent[1], int(recent[1]) if recent[1].isdigit() else 0)
            if not 1 <= count <= 20:
                raise ValueError("最近年度范围必须为 1-20 年")
            start, end = f"{today.year-count+1}-01-01", today.isoformat()
        elif years:
            first, last = min(map(int, years)), max(map(int, years))
            start, end = f"{first}-01-01", f"{last}-12-31"
        else:
            history = [r for r in self.memory.l2 if r.session_id == self.memory.session_id and "entity:"+chosen in r.tags]
            old_tags = history[-1].tags if history else []
            start = next((t[6:] for t in old_tags if t.startswith("since:")), f"{today.year-2}-01-01")
            end = next((t[6:] for t in old_tags if t.startswith("until:")), today.isoformat())
        start, end = since or start, until or end
        if date.fromisoformat(start) > date.fromisoformat(end):
            raise ValueError("开始日期不得晚于结束日期")
        return {"company_id": chosen, "entity_name": by_id[chosen]["name"], "since": start, "until": end}

    def ask(self, query, *, entity=None, since=None, until=None):
        if not isinstance(query, str) or not query.strip() or len(query) > 8000:
            raise ValueError("query 必须为 1-8000 字符")
        if not {"audit.read", "audit.analyze"}.issubset(self.identity.permissions):
            raise PermissionError("需要 audit.read 和 audit.analyze 权限")
        if self.identity.role not in {"auditor", "supervisor"}:
            raise PermissionError("当前角色不能执行审计分析")
        with self.memory.lock:
            return self._ask(query.strip(), entity, since, until)

    def _ask(self, query, entity, since, until):
        started = time.monotonic()
        self.memory.warnings.clear()
        scope = self._scope(query, entity, since, until)
        self.memory.add_message("user", query)
        if not scope:
            response = AuditResponse("请指定要审计的企业（例如华东供应商、远景科技、北方物流），或在页面选择已授权企业。", "clarify", [], [], [], [], session_id=self.memory.session_id, mode=self.planner.mode)
            self.memory.add_message("assistant", response.answer)
            response.elapsed_ms = round((time.monotonic()-started)*1000, 2)
            self._persist(query, response)
            return response
        episodic = self.memory.retrieve_episodes(query+scope["entity_name"])
        context = {**scope, "working_memory": self.memory.compressed_context(), "episodes": [r.summary[:2000] for r in episodic], "memory_version": self.memory_version}
        if self.memory_version == 2:
            context["core_memory"] = {"role": self.identity.role, "preferences": self.identity.preferences}
            context["semantic_rules"] = self.memory.semantic_rules(query)
        decision, results, tasks = self.swarm.run(query, context)
        evidence = unique_evidence([e for r in results for e in r.evidence])
        risks = list(dict.fromkeys(risk for r in results for risk in r.risks))
        report = next((r for r in results if r.agent_type == "report_generation" and r.status != "failed"), None)
        answer = report.answer if report else "\n\n".join(r.answer for r in results)
        warnings = list(dict.fromkeys(w for r in results for w in r.warnings))
        if any(r.status == "failed" for r in results):
            answer += "\n[证据补充提醒] 部分任务失败，当前结论不完整。"
        self.memory.add_message("assistant", answer)
        # 精简情景总结，避免将全量轨迹反复灌入下一轮上下文。
        summary = f"企业：{scope['entity_name']}；期间：{scope['since']} 至 {scope['until']}；问题：{query[:500]}；风险线索：{'、'.join(risks) or '未形成风险结论'}；证据："+", ".join(e.evidence_id for e in evidence)+"；待补充："+"；".join(warnings[:5])
        self.memory.remember_episode(summary, ["entity:"+scope["company_id"], "since:"+scope["since"], "until:"+scope["until"]])
        warnings.extend(self.memory.warnings)
        response = AuditResponse(answer, decision.mode, results, evidence, risks, [entry for r in results for entry in r.trace], repaired=any(r.repaired for r in results), warnings=warnings, session_id=self.memory.session_id, elapsed_ms=round((time.monotonic()-started)*1000, 2), route_reason=decision.reason, mode=self.planner.mode, memory_stats={**self.memory.last_stats, "version": self.memory_version, "retrieved_episodes": len(episodic), "semantic_rules": len(context.get("semantic_rules", [])), "backend": "sqlite+mem0" if self.memory.remote else "sqlite-character-vectors"}, tasks=tasks)
        response.scope = scope
        self.memory.distill(evidence, response.to_dict())
        response.elapsed_ms = round((time.monotonic()-started)*1000, 2)
        self._persist(query, response)
        return response

    def _persist(self, query, response):
        payload = response.to_dict()
        payload["query"] = query
        self.store.save_trajectory(self.identity.user_id, self.memory.session_id, payload)
