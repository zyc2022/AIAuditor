from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
import threading

from .models import AgentResult, AgentTask


@dataclass
class RouteDecision:
    mode: str
    agents: list[str]
    reason: str
    complexity: int = 0


class SwarmRouter:
    def decide(self, query):
        profile = any(k in query for k in ["画像", "背景", "经营", "风险", "分析"])
        collect = any(k in query for k in ["取证", "疑点", "证据", "流水", "SQL", "明细"])
        report = any(k in query for k in ["报告", "汇报", "审后"])
        domains = sum([profile, collect, report])
        score = domains + int("综合" in query) + int(len(query) > 80)
        if domains >= 2 or score >= 3:
            agents = ["enterprise_profile", "evidence_collection"]
            if report:
                agents.append("report_generation")
            return RouteDecision("swarm", agents, f"识别到 {domains} 类审计目标，先并行画像与取证，再按依赖生成报告。", score)
        agent = "report_generation" if report else "evidence_collection" if collect else "enterprise_profile"
        return RouteDecision("single", [agent], "单一审计目标，采用专业 Agent 快速通道。", score)


class TaskBoard:
    def __init__(self):
        self._tasks = {}
        self._lock = threading.RLock()

    def update(self, task, status, dependencies=None, result=None):
        with self._lock:
            self._tasks[task.task_id] = {"task_id": task.task_id, "agent_type": task.agent_type, "objective": task.context.get("objective", ""), "status": status, "dependencies": dependencies or [], "evidence_ids": [e.evidence_id for e in result.evidence] if result else []}

    def snapshot(self):
        with self._lock:
            return [dict(value) for value in self._tasks.values()]


class AgentSwarm:
    def __init__(self, agents, router=None):
        self.agents = agents
        self.router = router or SwarmRouter()

    def run(self, query, context=None):
        context = context or {}
        decision = self.router.decide(query)
        board = TaskBoard()
        objectives = {"enterprise_profile": "识别企业背景、期间交易规模与主要风险线索", "evidence_collection": "定位疑点交易编号、执行只读查询并给出取证缺口", "report_generation": "汇总已完成任务的证据和局限，生成可审阅的报告草案"}
        tasks = [AgentTask.create(query, name, {**context, "objective": objectives[name]}) for name in decision.agents]
        results = {}
        for task in tasks:
            board.update(task, "pending")

        def execute(task, dependencies=None):
            board.update(task, "running", dependencies)
            try:
                result = self.agents[task.agent_type].run(task)
            except Exception as exc:
                result = AgentResult(self.agents[task.agent_type].display_name, "任务失败，未形成结论。", agent_type=task.agent_type, task_id=task.task_id, status="failed", warnings=[type(exc).__name__])
            board.update(task, result.status, dependencies, result)
            return result

        parallel = [t for t in tasks if t.agent_type != "report_generation" or decision.mode == "single"]
        with ThreadPoolExecutor(max_workers=max(1, len(parallel))) as pool:
            futures = {pool.submit(execute, task): task for task in parallel}
            for future in as_completed(futures):
                task = futures[future]
                results[task.agent_type] = future.result()
        for task in tasks:
            if task.agent_type not in results:
                upstream = [results[name] for name in decision.agents if name in results]
                task.context["prior_results"] = [asdict(r) for r in upstream]
                task.context["prior_risks"] = [risk for r in upstream for risk in r.risks]
                task.context["task_board"] = board.snapshot()
                results[task.agent_type] = execute(task, [r.task_id for r in upstream])
        return decision, [results[name] for name in decision.agents], board.snapshot()
