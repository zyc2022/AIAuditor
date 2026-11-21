from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
import hashlib
import uuid


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class UserIdentity:
    user_id: str = "demo-auditor"
    role: str = "auditor"
    permissions: list[str] = field(default_factory=lambda: ["audit.read", "audit.analyze"])
    preferences: dict[str, Any] = field(default_factory=dict)
    entity_ids: list[str] = field(default_factory=lambda: ["C001", "C002", "C003"])


@dataclass
class Message:
    role: str
    content: str
    timestamp: str = field(default_factory=utc_now)
    agent: str | None = None


@dataclass
class Evidence:
    source: str
    finding: str
    confidence: float = 1.0
    fields: dict[str, Any] = field(default_factory=dict)
    evidence_id: str = ""
    kind: str = "fact"

    def __post_init__(self):
        if not self.evidence_id:
            self.evidence_id = "E-" + hashlib.sha256((self.source + self.finding).encode()).hexdigest()[:12]


@dataclass
class ToolResult:
    tool: str
    observation: str
    evidence: list[Evidence] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    risks: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass
class AgentTask:
    task_id: str
    query: str
    agent_type: str
    context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, query: str, agent_type: str, context=None):
        return cls(uuid.uuid4().hex[:12], query, agent_type, context or {})


@dataclass
class AgentResult:
    agent: str
    answer: str
    evidence: list[Evidence] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    confidence: float = 0.0
    agent_type: str = ""
    task_id: str = ""
    elapsed_ms: float = 0
    warnings: list[str] = field(default_factory=list)
    repaired: bool = False
    status: str = "completed"


@dataclass
class AuditResponse:
    answer: str
    route: str
    agent_results: list[AgentResult]
    evidence: list[Evidence]
    risks: list[str]
    trace: list[dict[str, Any]]
    repaired: bool = False
    warnings: list[str] = field(default_factory=list)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str = ""
    elapsed_ms: float = 0
    route_reason: str = ""
    mode: str = "local-demo"
    memory_stats: dict[str, Any] = field(default_factory=dict)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    scope: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
