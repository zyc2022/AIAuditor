"""训练数据导出与可解释质量信号，不伪造人工偏好或训练完成指标。"""
from __future__ import annotations

from hashlib import sha256
import json
import re


def quality(record):
    results = record.get("agent_results", [])
    known = {e["evidence_id"] for e in record.get("evidence", [])}
    cited = re.findall(r"\[(E-[a-zA-Z0-9]+)\]", record.get("answer", ""))
    checks = {
        "has_evidence": bool(known),
        "valid_citations": bool(cited) and all(c in known for c in cited),
        "tool_budget": bool(results) and all(len(r["trace"]) <= 3 for r in results),
        "all_tasks_completed": bool(results) and all(r.get("status") == "completed" for r in results),
        "tool_success": bool(results) and all(t.get("status") == "ok" for r in results for t in r["trace"]),
        "risk_disclosed": not record.get("risks") or "风险标记" in record.get("answer", ""),
        "missing_disclosed": not record.get("warnings") or "证据补充提醒" in record.get("answer", ""),
    }
    return {"checks": checks, "score": round(sum(checks.values())/len(checks), 4), "definition": "规则代理奖励，未经人工校准，不能等同审计正确率"}


def _redact(value, replacements):
    text = json.dumps(value, ensure_ascii=False)
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<EMAIL>", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "<PHONE>", text)
    text = re.sub(r"(?i)(Bearer|Token)\s+[A-Za-z0-9_.-]+", r"\1 <REDACTED>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "<API_KEY>", text)
    for old, new in sorted(replacements.items(), key=lambda pair: len(pair[0]), reverse=True):
        text = text.replace(old, new)
    return json.loads(text)


def export_records(records, format="sft", company_names=None):
    if format not in {"sft", "rl", "raw"}:
        raise ValueError("format 只支持 sft、rl、raw")
    names = company_names or []
    replacements = {name: f"企业_{i+1}" for i, name in enumerate(sorted(names))}
    output = []
    for original in records:
        # 会话标识匿名化，业务数据只作基础脱敏；真实数据出域前还需人工审查。
        record = _redact(original, replacements)
        record["session_id"] = sha256(record.get("session_id", "").encode()).hexdigest()[:16]
        score = quality(record)
        if format == "sft":
            if not all(score["checks"].values()):
                continue
            request = "审计范围："+json.dumps(record.get("scope", {}), ensure_ascii=False)+"\n任务："+record.get("query", "")
            messages = [{"role": "system", "content": "你是审计助手。用工具核对证据，引用来源，把风险线索与已证实事实分开。"}, {"role": "user", "content": request}]
            # 分 Agent 轨迹保留标准 function/tool 消息。协作结构另存在 metadata。
            for result in record["agent_results"]:
                for i, step in enumerate(result["trace"]):
                    call_id = result["task_id"]+"_"+str(i)
                    messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": step["tool"], "arguments": json.dumps(step.get("arguments", {}), ensure_ascii=False)}}]})
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"observation": step["observation"], "evidence_ids": step.get("evidence_ids", [])}, ensure_ascii=False)})
            messages.append({"role": "assistant", "content": record["answer"]})
            from .skills import SkillRegistry
            registry = SkillRegistry()
            output.append({"messages": messages, "tools": registry.schemas(registry.names()), "metadata": {"request_id": record["request_id"], "route": record["route"], "mode": record["mode"], "quality": score, "tasks": record.get("tasks", [])}})
        elif format == "rl":
            output.append({"prompt": record.get("query", ""), "response": record["answer"], "trajectory": record["agent_results"], "reward": score["score"], "reward_signals": score, "metadata": {"mode": record["mode"], "request_id": record["request_id"], "requires_human_review": True}})
        else:
            record["quality"] = score
            output.append(record)
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in output) + ("\n" if output else "")
