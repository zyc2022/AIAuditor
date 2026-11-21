from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import json
from typing import Any

from .memory import similarity
from .models import Evidence, ToolResult, UserIdentity
from .storage import AuditStore


@dataclass
class SkillContext:
    query: str
    memory: Any = None
    task: Any = None
    store: AuditStore | None = None
    identity: UserIdentity = field(default_factory=UserIdentity)
    company_id: str | None = None
    since: str = "2000-01-01"
    until: str = "2099-12-31"
    observations: list[ToolResult] = field(default_factory=list)
    prior_results: list[dict] = field(default_factory=list)

    def read(self, sql, params=()):
        if self.store is None:
            raise RuntimeError("未配置数据源")
        return self.store.read(sql, params, self.identity)

    def transactions(self):
        return self.read("SELECT * FROM transactions WHERE company_id=? AND txn_date>=? AND txn_date<=? ORDER BY txn_date,id", (self.company_id, self.since, self.until))

    def contracts(self):
        return self.read("SELECT * FROM contracts WHERE company_id=? ORDER BY id", (self.company_id,))


def fact(table: str, ids: list[str], finding: str, **fields) -> Evidence:
    return Evidence("sqlite://" + table + "/" + ",".join(ids), finding, fields={"record_ids": ids, **fields})


class Skill:
    name = "skill"
    description = ""
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def run(self, context: SkillContext, arguments=None) -> ToolResult:
        raise NotImplementedError


class DeepSearchSkill(Skill):
    name = "deep_search"
    description = "检索指定企业主数据和相关案例原文；企业由会话授权上下文确定"

    def run(self, c, arguments=None):
        companies = c.read("SELECT * FROM companies WHERE id=?", (c.company_id,))
        docs = c.read("SELECT * FROM documents WHERE company_id=? ORDER BY id", (c.company_id,))
        evidence = []
        for row in companies:
            text = f"{row['name']}（{row['id']}），行业：{row['industry']}，地区：{row['region']}，成立：{row['established']}；关联方标识：{'是' if row['related_party'] else '否'}。"
            evidence.append(fact("companies", [row["id"]], text, company_id=row["id"]))
        for row in docs:
            evidence.append(fact("documents", [row["id"]], row["title"]+"："+row["content"], company_id=c.company_id))
        return ToolResult(self.name, "\n".join(e.finding for e in evidence) or "未找到授权范围内的企业资料。", evidence, {"companies": companies, "documents": docs}, missing=[] if evidence else ["请指定已授权的企业名称"])


class InformationAggregationSkill(Skill):
    name = "information_aggregation"
    description = "聚合当前企业和期间的真实交易金额、笔数和合同勾稽表"

    def run(self, c, arguments=None):
        rows, contracts = c.transactions(), c.contracts()
        if not rows:
            return ToolResult(self.name, "指定企业及期间没有交易记录。", missing=["缺少交易台账，不能判断无风险"])
        total = round(sum(r["amount"] for r in rows), 2)
        finding = f"期间 {c.since} 至 {c.until}，共 {len(rows)} 笔付款，合计 {total:,.2f} 元。"
        evidence = [fact("transactions", [r["id"] for r in rows], finding, company_id=c.company_id, total_amount=total, count=len(rows))]
        reconciliation = []
        for contract in contracts:
            paid = round(sum(r["amount"] for r in rows if r["contract_id"] == contract["id"]), 2)
            reconciliation.append({**contract, "period_paid": paid, "difference": round(paid-contract["amount"], 2)})
        missing = [f"合同 {r['id']} 原件未归档" for r in contracts if not r["document_available"]]
        return ToolResult(self.name, finding + " 合同付款勾稽：" + json.dumps(reconciliation, ensure_ascii=False), evidence, {"total_amount": total, "count": len(rows), "reconciliation": reconciliation}, missing=missing)


class RiskAssessmentSkill(Skill):
    name = "risk_assessment"
    description = "基于交易记录识别大额、重复发票、疑似拆分、逾期与合同超付线索，不能认定违规"

    def run(self, c, arguments=None):
        rows, contracts = c.transactions(), c.contracts()
        evidence, risks = [], []
        def add(rule, transactions, explanation, contract_ids=None):
            ids = [r["id"] for r in transactions]
            finding = f"{rule}：{explanation}，关联交易 {', '.join(ids)}；需补充原始凭证核实。"
            e = fact("transactions", ids, finding, rule=rule, level="待核实", company_id=c.company_id, contract_ids=contract_ids or [])
            e.kind = "risk"
            evidence.append(e)
            risks.append(rule)
        large = [r for r in rows if r["amount"] >= 1000000]
        if large:
            add("大额付款", large, "单笔金额达到演示关注阈值 100 万元")
        invoices, days = defaultdict(list), defaultdict(list)
        for row in rows:
            if row["invoice_no"]:
                invoices[row["invoice_no"]].append(row)
            days[(row["txn_date"], row["counterparty"])].append(row)
        for number, group in invoices.items():
            if len(group) > 1:
                add("重复发票号", group, f"发票号 {number} 出现在 {len(group)} 笔付款中，可能为分次付款或重复支付")
        for key, group in days.items():
            if len(group) > 1 and all(r["amount"] < 500000 for r in group) and sum(r["amount"] for r in group) >= 500000:
                add("疑似拆分付款", group, f"同日同对手方多笔小于 50 万元，合计 {sum(r['amount'] for r in group):,.2f} 元；50 万元仅为演示阈值")
        late = [r for r in rows if r["due_date"] and r["txn_date"] > r["due_date"]]
        if late:
            add("逾期付款", late, "付款日期晚于台账记录的到期日")
        for contract in contracts:
            group = [r for r in rows if r["contract_id"] == contract["id"]]
            paid = sum(r["amount"] for r in group)
            if paid > contract["amount"]:
                add("期间付款超过合同额", group, f"合同 {contract['id']} 金额 {contract['amount']:,.2f} 元，期间付款 {paid:,.2f} 元", [contract["id"]])
        companies = c.read("SELECT * FROM companies WHERE id=?", (c.company_id,))
        if companies and companies[0]["related_party"]:
            evidence.append(fact("companies", [c.company_id], "主数据标记为关联方，应核查定价与披露；关联身份本身不构成违规。", company_id=c.company_id))
        missing = [f"合同 {r['id']} 原件缺失，真实性和完整性尚不能验证" for r in contracts if not r["document_available"]]
        if len(rows) >= 200:
            missing.append("交易查询达到 200 条返回上限，结论覆盖范围有限")
        if not rows:
            missing.append("缺少交易数据，不能形成风险结论")
        observation = "\n".join(e.finding for e in evidence) or "已扫描现有记录，未命中演示规则；不能据此认定不存在风险。"
        return ToolResult(self.name, observation, evidence, {"risk_count": len(risks), "scanned_transactions": len(rows)}, risks, missing)


class AuditAdviceSkill(Skill):
    name = "audit_advice"
    description = "结合已观察到的风险、缺失证据和指南，形成针对性取证程序"

    def run(self, c, arguments=None):
        observed = " ".join(o.observation for o in c.observations) + json.dumps(c.prior_results, ensure_ascii=False)
        steps = ["获取合同原件、验收资料、发票及银行流水，按交易编号建立证据链。"]
        for keyword, step in [
            ("重复发票", "核对重复发票对应的付款批次、冲销和累计付款，区分分次支付与重复支付。"),
            ("拆分", "取得审批制度及审批日志，核对同日付款业务背景和审批阈值。"),
            ("关联", "穿透股权及管理层关系，比较同类市场报价并核查披露。"),
            ("原件", "补取未归档合同原件及签章记录，在取证前保留结论局限。"),
        ]:
            if keyword in observed or keyword in c.query:
                steps.append(step)
        guides = c.read("SELECT * FROM documents WHERE category=?", ("guide",))
        evidence = [fact("documents", [r["id"]], r["content"]) for r in guides if r["id"] == "G002"]
        return ToolResult(self.name, "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps)), evidence, {"steps": steps})


class Text2SQLSkill(Skill):
    name = "text2sql"
    description = "生成并实际执行 SQLite 只读查询；企业与期间强制使用授权上下文，支持汇总、按年及明细"
    parameters = {"type": "object", "properties": {"view": {"type": "string", "enum": ["summary", "yearly", "details"]}}, "additionalProperties": False}

    def run(self, c, arguments=None):
        view = (arguments or {}).get("view", "yearly" if any(k in c.query for k in ["每年", "按年"]) else "details" if "明细" in c.query else "summary")
        if view not in {"summary", "yearly", "details"}:
            raise ValueError("不支持的 SQL 查询类型")
        base = " FROM transactions WHERE company_id=? AND txn_date>=? AND txn_date<=?"
        sql = {"summary": "SELECT counterparty, SUM(amount) AS total_amount, COUNT(*) AS txn_count"+base+" GROUP BY counterparty ORDER BY total_amount DESC", "yearly": "SELECT substr(txn_date,1,4) AS year, SUM(amount) AS total_amount, COUNT(*) AS txn_count"+base+" GROUP BY substr(txn_date,1,4) ORDER BY year", "details": "SELECT *"+base+" ORDER BY txn_date,id"}[view]
        params = (c.company_id, c.since, c.until)
        rows = c.read(sql, params)
        source_rows = c.transactions()
        text = "只读查询结果：" + json.dumps(rows, ensure_ascii=False)
        evidence = [fact("transactions", [r["id"] for r in source_rows], text, company_id=c.company_id, sql=sql, parameters=list(params))] if source_rows else []
        return ToolResult(self.name, text, evidence, {"sql": sql, "parameters": list(params), "rows": rows, "read_only": True}, missing=[] if source_rows else ["查询结果为空，请检查企业与期间"])


class AuditGuideSkill(Skill):
    name = "audit_guide"
    description = "检索本地审计指南原文并保留文档编号；演示指南不等同正式法规"

    def run(self, c, arguments=None):
        docs = c.read("SELECT * FROM documents WHERE category=?", ("guide",))
        ranked = sorted(docs, key=lambda d: similarity(c.query, d["title"]+d["content"]), reverse=True)[:3]
        evidence = [fact("documents", [r["id"]], r["title"]+"："+r["content"]) for r in ranked]
        return ToolResult(self.name, "\n".join(e.finding for e in evidence), evidence, {"documents": ranked})


class DeepResearchSkill(Skill):
    name = "deep_research"
    description = "以当前工具观察、上游任务结果和案例指南交叉核对，形成研究综述和证据缺口"

    def run(self, c, arguments=None):
        docs = c.read("SELECT * FROM documents WHERE company_id=? OR category=?", (c.company_id, "guide"))
        ranked = sorted(docs, key=lambda d: similarity(c.query, d["content"]), reverse=True)[:3]
        evidence = [fact("documents", [r["id"]], r["content"], company_id=r["company_id"]) for r in ranked]
        missing = list(dict.fromkeys(m for o in c.observations for m in o.missing))
        text = f"已交叉核对 {len(c.observations)} 项工具观察、{len(c.prior_results)} 项上游任务和 {len(ranked)} 份资料。\n" + "\n".join(e.finding for e in evidence)
        if missing:
            text += "\n待补充："+"；".join(missing)
        return ToolResult(self.name, text, evidence, {"upstream_tasks": len(c.prior_results)}, missing=missing)


class SkillRegistry:
    def __init__(self, skills=None):
        supplied = skills if skills is not None else [DeepSearchSkill(), InformationAggregationSkill(), RiskAssessmentSkill(), AuditAdviceSkill(), Text2SQLSkill(), AuditGuideSkill(), DeepResearchSkill()]
        self._skills = {}
        for skill in supplied:
            self.register(skill)

    def register(self, skill):
        if skill.name in self._skills:
            raise ValueError("Skill 重名："+skill.name)
        self._skills[skill.name] = skill

    def names(self):
        return list(self._skills)

    def get(self, name):
        return self._skills[name]

    def run(self, name, context, arguments=None):
        return self.get(name).run(context, arguments)

    def schemas(self, names):
        return [{"type": "function", "function": {"name": name, "description": self.get(name).description, "parameters": self.get(name).parameters}} for name in names]
