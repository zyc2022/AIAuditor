import argparse
import json
from pathlib import Path

from .orchestrator import AuditAssistant


def main() -> None:
    parser = argparse.ArgumentParser(description="智能审计助手 Agent Harness")
    parser.add_argument("--query", help="审计问题")
    parser.add_argument("--session", default="cli", help="会话 ID")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--serve", action="store_true", help="启动本地工作台与 HTTP API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="data/audit.sqlite3", help="持久化数据库路径")
    parser.add_argument("--harness", help="Harness YAML 路径")
    parser.add_argument("--entity", help="企业编号或名称，追问可沿用上次企业")
    parser.add_argument("--since", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--until", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--memory-version", type=int, choices=[1, 2], default=2)
    parser.add_argument("--export", choices=["sft", "rl", "raw"], help="导出当前用户的历史轨迹")
    parser.add_argument("--output", help="将结果保存到文件")
    args = parser.parse_args()
    if args.serve:
        from .server import serve
        serve(args.host, args.port, args.db, args.harness)
        return
    assistant = AuditAssistant(session_id=args.session, db_path=args.db, harness_path=args.harness, memory_version=args.memory_version)
    if args.export:
        from .trajectories import export_records
        text = export_records(assistant.store.trajectories(assistant.identity.user_id, assistant.identity.entity_ids), args.export, [c["name"] for c in assistant.store.companies(assistant.identity)])
    elif args.query:
        response = assistant.ask(args.query, entity=args.entity, since=args.since, until=args.until)
        text = json.dumps(response.to_dict(), ensure_ascii=False, indent=2) if args.json else response.answer
    else:
        parser.error("请指定 --query、--serve 或 --export")
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        import sys
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()

