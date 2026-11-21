from __future__ import annotations

import time

from .harness import Harness
from .llm import LocalPlanner
from .models import AgentResult, AgentTask, Evidence, ToolResult
from .skills import SkillContext, SkillRegistry


def unique_evidence(items):
    return list({item.evidence_id: item for item in items}.values())


class AuditAgent:
    agent_type = "base"
    display_name = "审计 Agent"

    def __init__(self, skills=None, memory=None, harness=None, planner=None, store=None):
        self.skills = skills or SkillRegistry()
        self.memory = memory
        self.harness = harness or Harness()
        self.planner = planner or LocalPlanner()
        self.store = store

    def run(self, task: AgentTask) -> AgentResult:
        started = time.monotonic()
        deadline = started+self.harness.policy["max_agent_seconds"]
        allowed = self.harness.allowed_skills(self.agent_type)
        trace, observations, warnings = [], [], []
        upstream = task.context.get("prior_results", [])
        c = SkillContext(task.query, self.memory, task, self.store, self.memory.l0, task.context.get("company_id"), task.context["since"], task.context["until"], observations, upstream)
        status = "completed"
        for step in range(self.harness.max_tool_calls):
            if time.monotonic() >= deadline:
                warnings.append("Agent 时间预算耗尽，已停止后续调用")
                status = "partial"
                break
            try:
                decision = self.planner.decide(agent_type=self.agent_type, query=task.query, allowed=allowed, observations=observations, context=task.context, schemas=self.skills.schemas(allowed), deadline=deadline)
            except Exception as exc:
                warnings.append("模型决策失败，使用已有证据返回："+type(exc).__name__)
                status = "partial"
                break
            if decision.tool is None:
                if not observations and not upstream:
                    warnings.append("模型未检索证据即结束，无法形成审计结论")
                    status = "partial"
                break
            entry = {"step": step+1, "task_id": task.task_id, "agent_type": self.agent_type, "decision": decision.rationale, "tool": decision.tool, "arguments": decision.arguments}
            action_start = time.monotonic()
            try:
                self.harness.check_action(self.agent_type, decision.tool, len(trace))
                schema = self.skills.get(decision.tool).parameters
                if set(decision.arguments) - set(schema.get("properties", {})):
                    raise ValueError("工具参数包含未声明字段")
                if time.monotonic() >= deadline:
                    raise TimeoutError("模型决策后已无工具执行预算")
                output = self.skills.run(decision.tool, c, decision.arguments)
                output.data["_call_arguments"] = decision.arguments
                observations.append(output)
                entry.update({"status": "ok", "observation": output.observation, "evidence_ids": [e.evidence_id for e in output.evidence], "data": output.data, "risks": output.risks, "missing": output.missing})
            except Exception as exc:
                # 失败和越界尝试同样占用预算，不能重置计数无限重试。
                message = "工具调用未完成："+type(exc).__name__
                warnings.append(message)
                observations.append(ToolResult(decision.tool, message, missing=[message]))
                entry.update({"status": "blocked" if isinstance(exc, PermissionError) else "error", "observation": message, "evidence_ids": []})
                status = "partial"
            entry["elapsed_ms"] = round((time.monotonic()-action_start)*1000, 2)
            trace.append(entry)
        evidence = unique_evidence([Evidence(**e) for r in upstream for e in r.get("evidence", [])] + [e for o in observations for e in o.evidence])
        risks = list(dict.fromkeys([risk for r in upstream for risk in r.get("risks", [])] + [risk for o in observations for risk in o.risks]))
        warnings.extend(m for o in observations for m in o.missing)
        warnings.extend(w for r in upstream for w in r.get("warnings", []))
        elapsed = round((time.monotonic()-started)*1000, 2)
        if elapsed > self.harness.policy["max_agent_seconds"]*1000:
            warnings.append("实际耗时超过 Agent 时间预算；请检查外部服务延迟")
            status = "partial"
        answer = self.compose(task, observations, evidence, risks)
        result = AgentResult(self.display_name, answer, evidence, trace, risks, agent_type=self.agent_type, task_id=task.task_id, elapsed_ms=elapsed, warnings=list(dict.fromkeys(warnings)), status=status)
        result, _ = self.harness.validate_and_repair(result, self.agent_type)
        return result

    def compose(self, task, observations, evidence, risks):
        # 对事实和金额进行确定性渲染，禁止把模型无来源的自由文本当作审计结论。
        heading = {"enterprise_profile": "企业画像与风险概览", "evidence_collection": "疑点与取证工作底稿", "report_generation": "审计报告草案"}[self.agent_type]
        lines = [heading, f"审计范围：{task.context.get('entity_name', '未指定企业')}；{task.context['since']} 至 {task.context['until']}。", "数据为本地合成演示数据，输出需人工复核。", "", "事实与来源："]
        facts = [e for e in evidence if e.kind == "fact"]
        if not facts:
            lines.append("未获得可追溯事实，暂不形成结论。")
        for e in facts:
            lines.append(f"- {e.finding} [{e.evidence_id}]")
        lines.extend(["", "风险线索（待核实）："])
        risk_evidence = [e for e in evidence if e.kind == "risk"]
        lines.extend(f"- {e.finding} [{e.evidence_id}]" for e in risk_evidence)
        if not risk_evidence:
            lines.append("现有观察未形成可引用的风险线索；不代表无风险。")
        lines.extend(["", "建议与下一步："])
        advice = [o.observation for o in observations if o.tool == "audit_advice"]
        lines.extend(advice or ["补齐原始合同、发票、流水及审批材料，并根据所列交易编号逐项复核。"])
        if self.agent_type == "report_generation":
            lines.append("结论局限：以上仅为工作底稿草案，规则命中不能替代审计人员的专业判断。")
        return "\n".join(lines)


class EnterpriseProfileAgent(AuditAgent):
    agent_type = "enterprise_profile"
    display_name = "审前企业画像分析 Agent"


class EvidenceCollectionAgent(AuditAgent):
    agent_type = "evidence_collection"
    display_name = "审中疑点取证 Agent"


class ReportGenerationAgent(AuditAgent):
    agent_type = "report_generation"
    display_name = "审后审计报告生成 Agent"


def build_agents(skills, memory, harness, planner=None, store=None):
    return {cls.agent_type: cls(skills, memory, harness, planner, store) for cls in [EnterpriseProfileAgent, EvidenceCollectionAgent, ReportGenerationAgent]}
