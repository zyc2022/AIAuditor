"use strict";
const $ = (id) => document.getElementById(id);
let session = localStorage.getItem("audit-session") || "";
let busy = false;
let ready = false;
let latest = null;
let apiToken = "";

function node(tag, text, cls) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (cls) item.className = cls;
  return item;
}
function showError(message) { $("error").textContent = message; $("error").hidden = !message; }
async function api(path, body) {
  const headers = {};
  if (apiToken) headers.Authorization = "Bearer " + apiToken;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {method: body === undefined ? "GET" : "POST", headers, body: body === undefined ? undefined : JSON.stringify(body)});
  if (!response.ok) {
    let error;
    try { error = (await response.json()).error; } catch { error = "请求失败，请检查服务状态。"; }
    throw new Error(error || "请求失败");
  }
  return response;
}
function setBusy(value) {
  busy = value;
  ["send", "new-session", "entity", "connect"].forEach(id => $(id).disabled = value || !ready);
  $("query").disabled = value || !ready;
  $("send").textContent = value ? "分析中…" : "开始分析 ↑";
  $("progress").textContent = value ? "正在执行审计任务，等待证据与协作结果…" : "工具调用有硬上限，结论附带证据来源";
}
function message(role, text) {
  $("welcome")?.remove();
  const card = node("article", undefined, "message " + role);
  card.append(node("div", role === "user" ? "审计人员" : "审知 · 审计助手", "message-label"), node("div", text, "message-text"));
  $("messages").append(card);
  $("messages").scrollTop = $("messages").scrollHeight;
  return card;
}
async function download(path, filename) {
  try {
    const response = await api(path);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = node("a"); link.href = url; link.download = filename; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { showError(error.message); }
}
function renderEvidence(result) {
  const root = $("tab-evidence"); root.replaceChildren();
  root.append(node("p", "每项事实保留来源和记录编号，风险线索需核验原始资料。", "muted small"));
  if (!result.evidence.length) root.append(node("div", "尚无可追溯证据。", "empty-state"));
  result.evidence.forEach(e => {
    const card = node("article", undefined, "evidence-card");
    const top = node("div", undefined, "card-top");
    top.append(node("span", e.evidence_id));
    if (e.kind === "risk") top.append(node("span", "待核实线索", "risk-chip"));
    card.append(top, node("p", e.finding), node("span", e.source, "source")); root.append(card);
  });
}
function renderTasks(result) {
  const root = $("tab-tasks"); root.replaceChildren();
  root.append(node("p", result.route_reason || "尚未创建执行任务。", "muted small"));
  const names = {enterprise_profile: "审前 · 企业画像", evidence_collection: "审中 · 疑点取证", report_generation: "审后 · 报告生成"};
  const statuses = {completed: "已完成", partial: "部分完成", failed: "失败"};
  result.tasks.forEach(task => {
    const card = node("article", undefined, "task-card");
    const top = node("div", undefined, "card-top"); top.append(node("span", names[task.agent_type]), node("span", statuses[task.status] || task.status, "badge subtle"));
    const agent = result.agent_results.find(r => r.task_id === task.task_id);
    card.append(top, node("p", task.objective), node("span", `${agent?.trace.length || 0}/3 次工具调用 · ${agent?.elapsed_ms || 0} ms`, "source"));
    if (task.dependencies.length) card.append(node("p", "等待画像与取证结果后汇总，已注入上游证据。"));
    root.append(card);
  });
  const trace = $("trace-list"); trace.replaceChildren();
  result.agent_results.forEach(agent => {
    const details = node("details"); details.append(node("summary", agent.agent + ` · ${agent.trace.length} 次调用`), node("pre", JSON.stringify(agent.trace, null, 2))); trace.append(details);
  });
}
async function renderMemory() {
  if (!session) return;
  const memory = await (await api("/api/memory?session_id=" + encodeURIComponent(session))).json();
  const root = $("tab-memory"); root.replaceChildren();
  const ratio = Math.round((memory.compression.compression_ratio || 0)*100);
  const layers = [
    ["L0 · 核心记忆", `${memory.core.user_id} / ${memory.core.role}\n授权企业：${memory.core.entity_ids.join("、")}\n偏好：${JSON.stringify(memory.core.preferences)}`],
    ["L1 · 工作记忆", `加载 ${memory.working.length} 条最近对话，最多五轮。\n本次压缩 ${ratio}%（目标 35%，保留最近完整问题）。`],
    ["L2 · 情景记忆", `最近 ${memory.episodes.length} 条历史案例摘要，可跨会话检索。\n` + memory.episodes.map(e => e.summary).join("\n\n")],
    ["L3 · 语义记忆", memory.semantic.map(r => r.rule).join("\n\n")]
  ];
  layers.forEach(([title, text]) => { const card = node("article", undefined, "memory-card"); card.append(node("h3", title), node("p", text)); root.append(card); });
}
function resetMetrics() {
  $("metric-route").textContent = "等待任务";
  ["metric-evidence", "metric-risk", "metric-time"].forEach(id => $(id).textContent = "—");
  latest = null;
  $("trace-list").replaceChildren();
  $("tab-evidence").replaceChildren(node("div", "执行查询后查看证据。", "empty-state"));
  $("tab-tasks").replaceChildren(node("div", "执行查询后查看任务。", "empty-state"));
}
function renderResult(result, card) {
  latest = result;
  if (card && result.route !== "clarify") {
    const report = node("button", "↓ 下载审计底稿 · Markdown", "secondary download-report");
    report.addEventListener("click", () => download("/api/reports/"+result.request_id, "audit-report.md")); card.append(report);
  }
  $("metric-route").textContent = result.route === "swarm" ? "Swarm 协作" : result.route === "single" ? "单 Agent" : "待补充范围";
  $("metric-evidence").textContent = result.evidence.length;
  $("metric-risk").textContent = result.risks.length;
  $("metric-time").textContent = (result.elapsed_ms/1000).toFixed(2) + " s";
  renderEvidence(result); renderTasks(result);
}
async function newSession() {
  const result = await (await api("/api/sessions", {})).json();
  session = result.session_id; localStorage.setItem("audit-session", session);
  $("session-label").textContent = "会话 " + session.slice(0, 8);
  $("messages").replaceChildren();
  message("assistant", "新的审计会话已建立。请选择企业并描述审计目标；也可以输入“企业画像”“核查疑点”或“生成综合审计报告”。");
  resetMetrics();
  await renderMemory();
}
async function init() {
  ready = false; setBusy(false); $("connect").disabled = false;
  showError("");
  try {
    const config = await (await api("/api/config")).json();
    $("mode").textContent = config.mode === "llm" ? "LLM · 合成数据" : "本地规则模式 · 合成数据";
    $("entity").replaceChildren();
    config.companies.forEach(c => { const option = node("option", c.name); option.value = c.id; $("entity").append(option); });
    if (session) {
      try {
        const memory = await (await api("/api/memory?session_id="+encodeURIComponent(session))).json();
        if (memory.working.length) {
          $("messages").replaceChildren();
          let lastCard;
          memory.working.forEach(m => { lastCard = message(m.role, m.content); });
          if (memory.last_response) renderResult(memory.last_response, lastCard);
        }
        $("session-label").textContent = "会话 " + session.slice(0, 8);
        await renderMemory();
      } catch { session = ""; }
    }
    if (!session) {
      const result = await (await api("/api/sessions", {})).json();
      session = result.session_id; localStorage.setItem("audit-session", session);
      $("session-label").textContent = "会话 " + session.slice(0, 8);
      await renderMemory();
    }
    ready = true;
  } catch (error) { showError(error.message); }
  finally { setBusy(false); $("connect").disabled = false; }
}
$("chat-form").addEventListener("submit", async event => {
  event.preventDefault();
  const query = $("query").value.trim();
  if (!query || busy || !ready) return;
  setBusy(true); showError(""); message("user", query); $("query").value = "";
  try {
    const result = await (await api("/api/chat", {session_id: session, query, entity: $("entity").value})).json();
    const card = message("assistant", result.answer);
    renderResult(result, card); await renderMemory();
  } catch (error) { showError(error.message); message("assistant", "本次请求未完成："+error.message); $("query").value = query; }
  finally { setBusy(false); $("query").focus(); }
});
$("query").addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("chat-form").requestSubmit(); } });
document.querySelectorAll("[data-query]").forEach(button => button.addEventListener("click", () => { if (busy) return; $("query").value = button.dataset.query; $("query").focus(); }));
document.querySelectorAll("[data-tab]").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll("[data-tab]").forEach(tab => { const selected = button === tab; tab.classList.toggle("selected", selected); tab.setAttribute("aria-selected", String(selected)); $("tab-"+tab.dataset.tab).hidden = !selected; });
}));
document.querySelectorAll(".export").forEach(button => button.addEventListener("click", () => download("/api/export?format="+button.dataset.format, "audit-"+button.dataset.format+".jsonl")));
$("new-session").addEventListener("click", async () => { if (busy) return; setBusy(true); try { await newSession(); } catch (error) { showError(error.message); } finally { setBusy(false); } });
$("connect").addEventListener("click", () => { apiToken = $("token").value; init(); });
init();
