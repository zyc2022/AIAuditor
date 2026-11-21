"""可复现实验入口；仅评测合成样本，不将目标比例冒充线上统计。"""
from pathlib import Path
import argparse
import json
import statistics
import tempfile
import time

from .orchestrator import AuditAssistant
from .trajectories import quality


def evaluate(cases_path, memory_version=2):
    cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    rows = []
    with tempfile.TemporaryDirectory() as temp:
        for case in cases:
            started = time.perf_counter()
            result = AuditAssistant(case["id"], db_path=Path(temp)/"eval.sqlite3", memory_version=memory_version).ask(case["query"])
            elapsed = (time.perf_counter()-started)*1000
            expected = set(case["expected_risks"])
            rows.append({"id": case["id"], "mode": result.mode, "route": result.route, "route_correct": result.route == case["route"], "risk_expectation_met": expected.issubset(result.risks) if expected else not result.risks, "quality": quality(result.to_dict()), "elapsed_ms": round(elapsed, 2), "agent_ms": [r.elapsed_ms for r in result.agent_results]})
    agent_times = [t for r in rows for t in r["agent_ms"]]
    latency = sorted(r["elapsed_ms"] for r in rows)
    return {"dataset": str(cases_path), "count": len(rows), "memory_version": memory_version, "synthetic_data": True, "note": "10 条样本刻意覆盖 7 单 Agent / 3 Swarm，不能代表线上流量。LLM 与 Mem0 使用真实配置时会联网并产生费用。", "single_ratio": sum(r["route"] == "single" for r in rows)/len(rows), "route_accuracy": sum(r["route_correct"] for r in rows)/len(rows), "risk_expectation_rate": sum(r["risk_expectation_met"] for r in rows)/len(rows), "latency_ms": {"p50": statistics.median(latency), "p95": latency[max(0, int(.95*len(latency)+.999)-1)], "mean_agent": statistics.mean(agent_times)}, "all_agents_within_15s": all(t <= 15000 for t in agent_times), "cases": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="evals/cases.json")
    parser.add_argument("--memory-version", type=int, choices=[1, 2], default=2)
    parser.add_argument("--output", default="artifacts/evaluation.json")
    args = parser.parse_args()
    report = evaluate(args.cases, args.memory_version)
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
