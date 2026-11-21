"""配置是运行时边界，而不是供模型选择性遵守的提示词。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .models import AgentResult

DEFAULT_POLICY = {
    "max_tool_calls": 3,
    "max_agent_seconds": 15,
    "required_evidence": True,
    "risk_marker": "[风险标记]",
    "missing_evidence_notice": "[证据补充提醒] 结论仍需补充可追溯来源。",
    "agents": {
        "enterprise_profile": ["deep_search", "information_aggregation", "risk_assessment", "audit_guide", "deep_research"],
        "evidence_collection": ["deep_search", "text2sql", "risk_assessment", "audit_advice", "audit_guide"],
        "report_generation": ["information_aggregation", "risk_assessment", "audit_advice", "audit_guide", "deep_research"],
    },
}


def _scalar(value):
    value = value.strip()
    if value in {"true", "false"}:
        return value == "true"
    if value.isdigit():
        return int(value)
    if value.startswith(('"', "'")) and value[-1:] == value[:1]:
        return value[1:-1]
    raise ValueError("YAML 标量必须为整数、布尔或带引号字符串："+value)


def load_policy(path):
    """支持本项目使用的 YAML 标量 + agents 字符串列表子集，未知语法直接报错。"""
    policy, current_agent = {}, None
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    in_agents = False
    for number, raw in enumerate(lines, 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw:
            raise ValueError(f"YAML 第 {number} 行不支持 tab 缩进")
        indent, line = len(raw)-len(raw.lstrip()), raw.strip()
        if indent == 0:
            key, sep, value = line.partition(":")
            if not sep or key in policy:
                raise ValueError(f"YAML 第 {number} 行无效或重复")
            in_agents = key == "agents"
            if in_agents:
                if value.strip():
                    raise ValueError("agents 必须使用缩进列表")
                policy[key] = {}
            else:
                policy[key] = _scalar(value)
        elif in_agents and indent == 2 and line.endswith(":"):
            current_agent = line[:-1]
            if current_agent in policy["agents"]:
                raise ValueError("重复 Agent 配置")
            policy["agents"][current_agent] = []
        elif in_agents and indent == 4 and line.startswith("- ") and current_agent:
            name = line[2:]
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError("非法 Skill 名称")
            policy["agents"][current_agent].append(name)
        else:
            raise ValueError(f"不支持的 YAML 语法，第 {number} 行")
    return policy


@dataclass
class Harness:
    policy: dict | None = None

    def __post_init__(self):
        import copy
        self.policy = copy.deepcopy(DEFAULT_POLICY if self.policy is None else self.policy)
        if set(self.policy) != set(DEFAULT_POLICY):
            raise ValueError("Harness 配置字段必须完整且无未知字段")
        limit = self.policy["max_tool_calls"]
        if type(limit) is not int or not 1 <= limit <= 3:
            raise ValueError("max_tool_calls 必须为 1 至 3")
        seconds = self.policy["max_agent_seconds"]
        if type(seconds) not in (float, int) or not 0 < seconds <= 15:
            raise ValueError("Agent 时间预算必须在 (0,15] 秒内")
        if type(self.policy["required_evidence"]) is not bool:
            raise ValueError("required_evidence 必须是布尔值")
        for key in ("risk_marker", "missing_evidence_notice"):
            if not isinstance(self.policy[key], str) or not self.policy[key]:
                raise ValueError(key+" 不能为空")
        if not isinstance(self.policy["agents"], dict) or set(self.policy["agents"]) != set(DEFAULT_POLICY["agents"]):
            raise ValueError("必须显式声明三个专业 Agent")
        for names in self.policy["agents"].values():
            if not isinstance(names, list) or not names or len(names) != len(set(names)) or not all(isinstance(n, str) for n in names):
                raise ValueError("Agent Skill 列表为空、重复或类型错误")

    @classmethod
    def from_yaml(cls, path):
        return cls(load_policy(path))

    @property
    def max_tool_calls(self):
        return min(self.policy["max_tool_calls"], 3)

    def allowed_skills(self, agent_type):
        if agent_type not in self.policy["agents"]:
            raise PermissionError("未知 Agent")
        return list(self.policy["agents"][agent_type])

    def check_action(self, agent_type, tool, calls):
        if calls >= self.max_tool_calls:
            raise PermissionError("已达到工具调用硬上限")
        if tool not in self.allowed_skills(agent_type):
            raise PermissionError("工具不在 Agent 能力边界内："+tool)

    def validate_trace(self, trace, agent_type):
        calls = list(trace)
        errors = []
        if len(calls) > self.max_tool_calls:
            errors.append("工具调用超过硬上限")
        if any(c.get("tool") not in self.allowed_skills(agent_type) for c in calls):
            errors.append("发现越界工具调用")
        return not errors, errors

    def validate_and_repair(self, result: AgentResult, agent_type):
        valid, errors = self.validate_trace(result.trace, agent_type)
        if not valid:
            result.status = "failed"
            result.warnings.extend(errors)
        if not any(e.kind == "fact" and e.source.startswith("sqlite://") for e in result.evidence) and self.policy["required_evidence"]:
            notice = self.policy["missing_evidence_notice"]
            if notice not in result.answer:
                result.answer += "\n"+notice
                result.repaired = True
            result.confidence = 0
        if result.risks and self.policy["risk_marker"] not in result.answer:
            result.answer = self.policy["risk_marker"] + " " + result.answer
            result.repaired = True
        for warning in result.warnings:
            if warning not in result.answer:
                result.answer += "\n[证据补充提醒] " + warning
                result.repaired = True
        known = {e.evidence_id for e in result.evidence}
        for citation in re.findall(r"\[(E-[a-zA-Z0-9]+)\]", result.answer):
            if citation not in known:
                result.answer = result.answer.replace("["+citation+"]", "[无效引用，待补充]")
                result.repaired = True
        return result, errors
