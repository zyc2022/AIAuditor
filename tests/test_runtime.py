from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
import copy
import json
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from audit_assistant import AuditAssistant, UserIdentity
from audit_assistant.harness import DEFAULT_POLICY, Harness
from audit_assistant.llm import Decision, LocalPlanner
from audit_assistant.memory import EntropyManager, FourLayerMemory
from audit_assistant.models import AgentResult, Message
from audit_assistant.skills import SkillContext, SkillRegistry
from audit_assistant.storage import AuditStore
from audit_assistant.trajectories import export_records, quality


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)/"audit.sqlite3"
        self.env = patch.dict("os.environ", {"AUDIT_MODE": "local", "MEM0_API_KEY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def assistant(self, session="test", **kwargs):
        return AuditAssistant(session, db_path=self.db, **kwargs)

    def test_single_path_queries_actual_totals(self):
        result = self.assistant().ask("华东供应商近三年企业画像")
        self.assertEqual(result.route, "single")
        self.assertEqual(len(result.trace), 3)
        self.assertTrue(all(step["status"] == "ok" for step in result.trace))
        aggregate = next(e for e in result.evidence if "total_amount" in e.fields)
        self.assertEqual(aggregate.fields["total_amount"], 2770000)
        self.assertEqual(aggregate.fields["count"], 5)
        self.assertIn("TX005", aggregate.fields["record_ids"])

    def test_swarm_report_consumes_upstream_results(self):
        result = self.assistant().ask("综合分析华东供应商近三年企业画像、疑点取证并生成报告")
        self.assertEqual(result.route, "swarm")
        self.assertEqual(len(result.agent_results), 3)
        self.assertTrue(all(len(r.trace) <= 3 for r in result.agent_results))
        report = result.agent_results[-1]
        self.assertEqual(report.agent_type, "report_generation")
        self.assertEqual(report.trace[0]["data"]["upstream_tasks"], 2)
        self.assertEqual(len(result.tasks[-1]["dependencies"]), 2)
        self.assertIn("重复发票号", result.risks)
        self.assertIn("HT002", result.answer)
        self.assertIn("证据补充提醒", result.answer)
        self.assertIn("风险标记", result.answer)
        self.assertEqual(result.answer, report.answer)
        json.dumps(result.to_dict())

    def test_scope_missing_asks_instead_of_inventing(self):
        result = self.assistant().ask("请做企业画像")
        self.assertEqual(result.route, "clarify")
        self.assertFalse(result.trace)
        self.assertFalse(result.evidence)

    def test_report_only_reserves_budget_for_risk_scan(self):
        result = self.assistant().ask("华东供应商生成报告")
        self.assertEqual(result.route, "single")
        self.assertIn("risk_assessment", [t["tool"] for t in result.trace])
        self.assertIn("期间付款超过合同额", result.risks)

    def test_remote_memory_failure_preserves_local_results(self):
        class Offline:
            def search(self, query): raise TimeoutError("offline")
            def add(self, *args): raise TimeoutError("offline")
        assistant = self.assistant()
        assistant.memory.remote = Offline()
        response = assistant.ask("华东供应商企业画像")
        self.assertTrue(response.evidence)
        self.assertTrue(any("Mem0" in w for w in response.warnings))
        self.assertTrue(assistant.memory.l2)

    def test_partial_swarm_failure_is_reported(self):
        assistant = self.assistant()
        with patch.object(assistant.agents["enterprise_profile"], "run", side_effect=RuntimeError("simulated")):
            result = assistant.ask("华东供应商画像、取证并生成报告")
        self.assertEqual(result.tasks[0]["status"], "failed")
        self.assertEqual(len(result.tasks[-1]["dependencies"]), 2)
        self.assertIn("部分任务失败", result.answer)
        self.assertTrue(result.evidence)

    def test_followup_and_history_survive_new_instance(self):
        first = self.assistant().ask("华东供应商2025年企业画像")
        second = self.assistant().ask("那具体有哪些取证建议？")
        self.assertEqual(second.scope["company_id"], "C001")
        self.assertEqual(second.scope["since"], "2025-01-01")
        self.assertGreater(second.memory_stats["retrieved_episodes"], 0)
        third = self.assistant().ask("未知供应商的企业画像")
        self.assertEqual(third.route, "clarify")

    def test_cross_session_retrieval_is_user_isolated(self):
        self.assistant("a").ask("华东供应商近三年企业画像")
        same = self.assistant("b")
        self.assertTrue(same.memory.retrieve_episodes("华东供应商关联交易"))
        other = self.assistant("c", identity=UserIdentity(user_id="other"))
        self.assertFalse(other.memory.retrieve_episodes("华东供应商关联交易"))
        with self.assertRaises(PermissionError):
            self.assistant("a", identity=UserIdentity(user_id="other"))

    def test_restricted_user_cannot_retrieve_other_entity(self):
        self.assistant("full").ask("华东供应商企业画像")
        limited = self.assistant("limited", identity=UserIdentity(entity_ids=["C002"]))
        self.assertFalse(limited.memory.l2)
        with self.assertRaises(PermissionError):
            limited.ask("华东供应商企业画像")
        self.assertFalse(limited.store.trajectories("demo-auditor", ["C002"]))

    def test_changed_permissions_require_new_session(self):
        self.assistant().ask("华东供应商企业画像")
        with self.assertRaises(PermissionError):
            self.assistant(identity=UserIdentity(entity_ids=["C002"]))

    def test_role_and_permissions_are_enforced(self):
        for i, identity in enumerate([UserIdentity(role="guest"), UserIdentity(permissions=["audit.read"])]):
            with self.assertRaises(PermissionError):
                self.assistant("denied"+str(i), identity=identity).ask("华东供应商企业画像")

    def test_sql_read_only_and_row_authorization(self):
        store = AuditStore(self.db)
        identity = UserIdentity(entity_ids=["C002"])
        rows = store.read("SELECT * FROM transactions", (), identity)
        self.assertEqual({r["company_id"] for r in rows}, {"C002"})
        for sql in ["DROP TABLE transactions", "SELECT * FROM transactions; DELETE FROM transactions", "SELECT * FROM main.transactions", "SELECT * FROM sessions", "SELECT load_extension('evil')", "SELECT * FROM transactions UNION SELECT * FROM main.transactions"]:
            with self.subTest(sql=sql), self.assertRaises((ValueError, sqlite3.DatabaseError)):
                store.read(sql, (), identity)

    def test_text2sql_executes_sqlite_and_period_filter(self):
        assistant = self.assistant()
        result = assistant.ask("华东供应商按年交易明细取证", since=f"{date.today().year}-01-01", until=f"{date.today().year}-12-31")
        sql = next(t for t in result.trace if t["tool"] == "text2sql")
        self.assertTrue(sql["data"]["read_only"])
        self.assertEqual(sql["data"]["rows"][0]["total_amount"], 600000)
        self.assertEqual(result.scope["since"], f"{date.today().year}-01-01")

    def test_risk_rules_do_not_depend_on_question_keywords(self):
        assistant = self.assistant()
        result = assistant.ask("企业画像", entity="C001")
        self.assertIn("重复发票号", result.risks)
        clean = self.assistant("clean").ask("企业画像", entity="C002")
        self.assertFalse(clean.risks)
        self.assertNotIn("风险标记", clean.answer)

    def test_empty_data_does_not_generate_claims(self):
        result = self.assistant(seed=False).ask("某公司画像")
        self.assertFalse(result.evidence)
        self.assertEqual(result.route, "clarify")

    def test_invalid_inputs_and_ambiguous_scope(self):
        assistant = self.assistant()
        for q in ["", "x"*8001, None]:
            with self.assertRaises(ValueError):
                assistant.ask(q)
        with self.assertRaises(ValueError):
            assistant.ask("华东供应商和远景科技分析")
        with self.assertRaises(ValueError):
            assistant.ask("华东供应商画像", entity="C002")
        with self.assertRaises(ValueError):
            assistant.ask("华东供应商画像", since="2026-12-31", until="2024-01-01")

    def test_yaml_boundaries_are_loaded_and_invalid_config_fails(self):
        harness = Harness.from_yaml(Path(__file__).parents[1]/"config/harness.yaml")
        self.assertIn("deep_research", harness.allowed_skills("report_generation"))
        for limit in [0, 4, True]:
            policy = copy.deepcopy(DEFAULT_POLICY); policy["max_tool_calls"] = limit
            with self.assertRaises(ValueError):
                Harness(policy)
        with self.assertRaises(FileNotFoundError):
            Harness.from_yaml(Path(self.temp.name)/"missing.yaml")
        invalid = Path(self.temp.name)/"bad.yaml"; invalid.write_text("invalid: yep", encoding="utf-8")
        with self.assertRaises(ValueError):
            Harness.from_yaml(invalid)

    def test_untrusted_model_tool_calls_are_blocked_and_budgeted(self):
        class Rogue:
            mode = "fake-model"
            def decide(self, **kwargs):
                return Decision("delete_database", {"sql": "DROP TABLE transactions"})
        result = self.assistant(planner=Rogue()).ask("华东供应商企业画像")
        self.assertEqual(len(result.trace), 3)
        self.assertTrue(all(t["status"] == "blocked" for t in result.trace))
        self.assertFalse(result.evidence)
        self.assertIn("证据补充提醒", result.answer)
        self.assertEqual(len(AuditStore(self.db).read("SELECT * FROM transactions", (), UserIdentity())), 8)

    def test_model_decisions_observe_previous_tool_result(self):
        class Adaptive:
            mode = "fake-model"
            def __init__(self): self.seen = []
            def decide(self, observations, **kwargs):
                self.seen.append(len(observations))
                return Decision("deep_search" if not observations else "risk_assessment" if len(observations) == 1 else None)
        planner = Adaptive()
        result = self.assistant(planner=planner).ask("华东供应商企业画像")
        self.assertEqual(planner.seen, [0, 1, 2])
        self.assertEqual([t["tool"] for t in result.trace], ["deep_search", "risk_assessment"])

    def test_time_budget_stops_after_slow_decision(self):
        class Slow:
            mode = "fake-model"
            def decide(self, **kwargs):
                time.sleep(.02)
                return Decision("deep_search")
        assistant = self.assistant(planner=Slow())
        assistant.harness.policy["max_agent_seconds"] = .01
        result = assistant.ask("华东供应商企业画像")
        self.assertLessEqual(len(result.trace), 1)
        self.assertFalse(result.evidence)
        self.assertEqual(result.agent_results[0].status, "partial")

    def test_entropy_deduplicates_but_keeps_latest_complete_question(self):
        question = "必须核查TX001的合同金额，保留原始证据。"
        context, stats = EntropyManager().compress([Message("user", "旧问题"), Message("assistant", "重复内容"*100), Message("assistant", "重复内容"*100), Message("user", question)])
        self.assertIn(question, context)
        self.assertGreater(stats["compression_ratio"], .35)

    def test_recent_five_rounds_and_shared_instance(self):
        assistant = self.assistant()
        memory = assistant.memory
        self.assertIs(memory, FourLayerMemory("test", store=assistant.store, identity=assistant.identity))
        for i in range(7):
            memory.add_message("user", f"问题{i}")
            memory.add_message("assistant", f"回答{i}")
        recent = memory.working_context()
        self.assertEqual(len(recent), 10)
        self.assertEqual(recent[0].content, "问题2")

    def test_v1_and_v2_memory_ablation(self):
        result = self.assistant(memory_version=1).ask("华东供应商画像")
        self.assertEqual(result.memory_stats["version"], 1)
        self.assertEqual(result.memory_stats["semantic_rules"], 0)

    def test_semantic_distillation_requires_multiple_records(self):
        assistant = self.assistant()
        assistant.ask("华东供应商企业画像")
        self.assertFalse([r for r in assistant.memory.semantic_rules() if r["type"] == "distilled"])
        assistant.ask("继续做企业画像")
        self.assertFalse([r for r in assistant.memory.semantic_rules() if r["type"] == "distilled"])
        with assistant.store.connect() as db:
            db.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", ("TX009", "C003", "北方物流", f"{date.today().year}-04-20", 1100000, "INV009", "HT004", f"{date.today().year}-04-30"))
        assistant.ask("北方物流企业画像")
        distilled = [r for r in assistant.memory.semantic_rules() if r["type"] == "distilled"]
        self.assertTrue(distilled)
        self.assertGreaterEqual(len(distilled[0]["support"]["request_ids"]), 2)

    def test_parallel_sessions_remain_independent(self):
        a, b = self.assistant("parallel-a"), self.assistant("parallel-b")
        with ThreadPoolExecutor(2) as pool:
            fa = pool.submit(a.ask, "华东供应商企业画像")
            fb = pool.submit(b.ask, "远景科技企业画像")
            ar, br = fa.result(), fb.result()
        self.assertTrue(ar.risks)
        self.assertFalse(br.risks)
        self.assertNotIn("华东供应商", b.memory.compressed_context())

    def test_repair_flags_risk_and_missing_citations(self):
        result = AgentResult("test", "结论 [E-unknown]", risks=["异常"], warnings=["缺少合同"])
        fixed, errors = Harness().validate_and_repair(result, "enterprise_profile")
        self.assertIn("风险标记", fixed.answer)
        self.assertIn("证据补充提醒", fixed.answer)
        self.assertIn("无效引用", fixed.answer)
        self.assertTrue(fixed.repaired)

    def test_exports_grounded_trajectories_and_rejects_failures(self):
        assistant = self.assistant()
        response = assistant.ask("华东供应商企业画像")
        record = assistant.store.trajectories(assistant.identity.user_id)[0]
        self.assertEqual(quality(record)["score"], 1)
        sft = json.loads(export_records([record], "sft", ["华东供应商"]))
        self.assertNotIn("华东供应商", json.dumps(sft, ensure_ascii=False))
        self.assertIn("tools", sft)
        self.assertTrue(any(m["role"] == "tool" for m in sft["messages"]))
        rl = json.loads(export_records([record], "rl"))
        self.assertTrue(rl["metadata"]["requires_human_review"])
        bad = copy.deepcopy(record); bad["agent_results"][0]["status"] = "failed"
        self.assertEqual(export_records([bad], "sft"), "")


if __name__ == "__main__":
    unittest.main()
