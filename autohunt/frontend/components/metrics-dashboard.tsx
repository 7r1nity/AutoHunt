"use client";

import { useEffect, useState } from "react";
import type { AgentRun } from "@/lib/agent-runtime";
import { terminalRun } from "@/lib/agent-runtime";
import type { useAgentRuntime } from "@/lib/use-agent-runtime";
import { dateText, durationText, metricText, numberText, readMetrics, runKind, statusText } from "@/lib/execution-metrics";
import type { EvaluationReport, ExecutionLedger } from "@/lib/execution-metrics";
import { EvaluationDocument } from "./evaluation-document";
import "./metrics-dashboard.css";

function StateBadge({ status }: { status: string }) {
  return <span className={`metrics-state metrics-state-${status}`}>{statusText(status)}</span>;
}

function ExecutionDetails({ ledger }: { ledger: ExecutionLedger }) {
  const a = ledger.accounting;
  const knownPrompt = ledger.calls.some((call) => call.prompt_tokens != null);
  const knownCompletion = ledger.calls.some((call) => call.completion_tokens != null);
  const knownTime = ledger.calls.some((call) => call.latency_ms != null);
  return <>
    <dl className="metrics-strip" aria-label="本轮执行指标">
      <div><dt>模型调用</dt><dd>{numberText(a.recorded_model_calls)}</dd><small>已记录调用；另有 {a.model_retries} 次传输重试</small></div>
      <div><dt>工具调用</dt><dd>{numberText(ledger.tools.length)}</dd><small>另有 {a.tool_retries} 次工具重试</small></div>
      <div><dt>输入 / 输出 token</dt><dd className="metrics-token-value">{knownPrompt || knownCompletion ? `${knownPrompt ? numberText(a.known_prompt_tokens) : "未记录"} / ${knownCompletion ? numberText(a.known_completion_tokens) : "未记录"}` : "未记录"}</dd><small>{a.tokens_complete ? "本轮已记录调用的 usage 完整" : "仅汇总已知 usage，数据不完整"}</small></div>
      <div><dt>本轮记录时长</dt><dd>{durationText(a.persisted_run_span_ms)}</dd><small>创建至最后更新，可能包含等待</small></div>
      <div><dt>模型累计耗时</dt><dd>{knownTime ? durationText(a.model_time_sum_ms) : "未记录"}</dd><small>{a.calls_without_latency ? `${a.calls_without_latency} 次调用缺少耗时` : "并发调用累加，与实际等待时间不同"}</small></div>
    </dl>
    {!a.tokens_complete && <p className="metrics-data-note">token 数据不完整：{a.calls_without_complete_usage} 次调用缺少完整 usage，{a.retry_attempts_without_usage} 次传输重试未取得 usage。未记录的用量不按 0 计；旧任务或任务之外的调用可能不在账本中。</p>}
    <section className="metrics-section" aria-label="失败与异常">
      <div className="metrics-section-heading"><h3>失败与异常</h3><span>{ledger.issues.length} 条过程记录</span></div>
      {ledger.error && <div className="metrics-error" role="status"><strong>本轮结束原因</strong><p>{ledger.error}</p></div>}
      {!ledger.error && !ledger.issues.length && <p className="metrics-empty-inline">本轮未记录失败原因。{terminalRun(ledger.status) ? "" : "任务仍在进行，结果将继续更新。"}</p>}
      {ledger.issues.length > 0 && <><p className="metrics-help">过程异常可能已恢复，最终结果以本轮状态为准。条件不足、等待处理与执行失败分别保留。</p><ol className="metrics-issues">{ledger.issues.map((issue, index) => <li key={`${issue.seq}-${index}`}>
        <div><strong>{issue.tool || ({ model_response: "模型请求", plan_failed: "任务规划", watchdog_expired: "执行超时" }[issue.kind] ?? "执行步骤")}</strong><code>{issue.code || "未记录错误码"}</code>{issue.uncertain && <StateBadge status="outcome_indeterminate" />}</div>
        <p>{issue.detail || issue.code || "未提供详细原因，请核对运行记录。"}</p>
      </li>)}</ol></>}
    </section>
    <section className="metrics-section">
      <div className="metrics-section-heading"><h3>执行步骤</h3><span>仅本轮；独立子任务可在上方切换查看</span></div>
      {ledger.steps.length ? <div className="metrics-table-wrap"><table><thead><tr><th>步骤</th><th>工具</th><th>状态</th><th>计划版本</th></tr></thead><tbody>{ledger.steps.map((step, index) => <tr key={`${step.id}-${index}`}><td>{step.id}</td><td>{step.tool}</td><td><StateBadge status={step.status} /></td><td>{step.revision == null ? "未记录" : `v${step.revision}`}</td></tr>)}</tbody></table></div> : <p className="metrics-empty-inline">本轮没有记录结构化计划步骤。</p>}
      <p className="metrics-help">工具调用成功或进入队列不等于简历已送达；投递结果请结合申请看板核对。</p>
    </section>
    <details className="metrics-section" open><summary>模型调用明细（{ledger.calls.length}）</summary>
      {ledger.calls.length ? <div className="metrics-table-wrap"><table><thead><tr><th>模型 / 操作</th><th>状态</th><th>输入 token</th><th>输出 token</th><th>耗时</th></tr></thead><tbody>{ledger.calls.map((call) => <tr key={call.call_id}><td><strong>{call.model || "未记录模型"}</strong><small>{call.provider || ""} {call.operation || ""}</small></td><td><StateBadge status={call.status} />{call.error_type && <small>{call.error_type}</small>}</td><td>{numberText(call.prompt_tokens)}</td><td>{numberText(call.completion_tokens)}</td><td>{durationText(call.latency_ms)}</td></tr>)}</tbody></table></div> : <p className="metrics-empty-inline">本轮没有记录模型调用，可能由规则处理或缺少历史观测数据。</p>}
    </details>
    <details className="metrics-section"><summary>工具调用明细（{ledger.tools.length}）</summary>
      {ledger.tools.length ? <div className="metrics-table-wrap"><table><thead><tr><th>工具</th><th>状态</th><th>尝试次数</th><th>耗时</th></tr></thead><tbody>{ledger.tools.map((tool, index) => <tr key={index}><td>{tool.tool}</td><td><StateBadge status={tool.status} /></td><td>{numberText(tool.attempts)}</td><td>{durationText(tool.duration_ms)}</td></tr>)}</tbody></table></div> : <p className="metrics-empty-inline">本轮没有记录工具调用。</p>}
    </details>
  </>;
}

function ReportHistory() {
  const [reports, setReports] = useState<EvaluationReport[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<EvaluationReport | null>(null);
  const [error, setError] = useState("");
  const [listError, setListError] = useState("");
  const [loading, setLoading] = useState(true);
  const [category, setCategory] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setListError("");
    readMetrics<EvaluationReport[]>("/api/agent/evaluation-reports", controller.signal).then((rows) => {
      if (controller.signal.aborted) return;
      setReports(rows); setSelected((id) => rows.some((row) => row.id === id) ? id : rows[0]?.id || "");
    }).catch((e) => { if (!controller.signal.aborted) setListError(e.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setDetail(null); setError("");
    readMetrics<EvaluationReport>(`/api/agent/evaluation-reports/${encodeURIComponent(selected)}`, controller.signal)
      .then((data) => { if (!controller.signal.aborted) setDetail(data); })
      .catch((e) => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [selected, revision]);
  const report = detail?.id === selected ? detail : null;
  const visibleReports = reports.filter((item) => !category || item.category === category);
  const status = { recorded: "历史记录", passed: "离线验收通过", failed: "验收未通过", pending: "尚未完成评测" };
  function download() {
    if (!report) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = `${report.id}.json`; anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <section className="metrics-history">
    <div className="metrics-toolbar"><p className="metrics-help">查看已保存的评测结果；打开面板不会运行测试或调用模型。离线验收显示每个套件最近 100 轮。</p><button className="button secondary compact" onClick={() => setRevision((n) => n + 1)}>刷新报告</button></div>
    <div className="metrics-filters"><label>报告类别<select aria-label="报告类别" value={category} onChange={(event) => { const next = event.target.value; setCategory(next); setSelected(reports.find((item) => !next || item.category === next)?.id || ""); }}><option value="">全部报告</option>{Array.from(new Set(reports.map((item) => item.category))).map((item) => <option key={item} value={item}>{item}</option>)}</select></label></div>
    {listError && <p className="metrics-error" role="alert">{listError}</p>}
    {loading && <p role="status">正在读取报告目录…</p>}
    {!loading && !listError && !reports.length && <div className="metrics-empty">尚无历史评测报告。运行项目的离线验收脚本后，在这里刷新查看。</div>}
    <div className="metrics-report-layout">
      <nav className="metrics-report-list" aria-label="历史评测报告">{visibleReports.map((item) => <button key={item.id} aria-pressed={selected === item.id} onClick={() => setSelected(item.id)}><small>{item.category} · {dateText(item.date)}</small><strong>{item.title}</strong></button>)}</nav>
      {selected && <article className="metrics-report-detail">
        {error ? <p className="metrics-error" role="alert">{error}。可点击“刷新报告”重试。</p> : !report ? <p role="status">正在读取报告…</p> : <>
          <div className="metrics-report-heading"><div><span className={`metrics-state metrics-state-${report.status}`}>{status[report.status]}</span><h2>{report.title}</h2><p>{dateText(report.date)}</p></div><button className="button secondary compact" onClick={download}>下载报告</button></div>
          <p className="metrics-scope">{report.scope}</p>
          <div className="metrics-table-wrap"><table><thead><tr><th>指标</th><th>本次评测结果</th></tr></thead><tbody>{report.metrics?.map((metric, index) => <tr key={index}><td>{metric.label}</td><td>{metricText(metric.value, metric.format)}</td></tr>)}</tbody></table></div>
          {report.document && <details className="metrics-report-document" open><summary>报告正文与覆盖边界</summary><EvaluationDocument text={report.document} /></details>}
          <details className="metrics-report-document"><summary>结构化评测数据</summary><pre>{JSON.stringify(report.data, null, 2)}</pre></details>
        </>}
      </article>}
    </div>
  </section>;
}

export function MetricsDashboard({ runtime, initialRunId = null }: { runtime: ReturnType<typeof useAgentRuntime>; initialRunId?: string | null }) {
  const [tab, setTab] = useState("runs");
  const [session, setSession] = useState(runtime.sessionId);
  const [picked, setPicked] = useState(initialRunId ?? "");
  const [runData, setRunData] = useState<{ session: string; runs: AgentRun[] } | null>(null);
  const [ledgerData, setLedgerData] = useState<ExecutionLedger | null>(null);
  const [runError, setRunError] = useState("");
  const [ledgerError, setLedgerError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [loadedAt, setLoadedAt] = useState("");
  const runs = runData?.session === session ? runData.runs : [];
  const selected = picked ? runs.find((run) => run.id === picked) : runs[0];
  const ledger = ledgerData?.run_id === selected?.id ? ledgerData : null;
  useEffect(() => { setSession(runtime.sessionId); setPicked(initialRunId ?? ""); }, [runtime.sessionId, initialRunId]);
  // A completed run is published by the existing runtime; refresh immediately,
  // while polling also observes independent browser/delivery tasks.
  const runtimeVersion = runtime.runs.map((run) => `${run.id}:${run.status}`).join("|");
  useEffect(() => {
    if (tab !== "runs") return;
    const controller = new AbortController();
    let timer: number;
    async function load() {
      try {
        const rows = await readMetrics<AgentRun[]>(`/api/agent/runs?conversation_id=${encodeURIComponent(session)}`, controller.signal);
        if (!controller.signal.aborted) { setRunData({ session, runs: rows }); setRunError(""); }
      } catch (e) { if (!controller.signal.aborted) setRunError(e instanceof Error ? e.message : "运行记录读取失败"); }
      finally { if (!controller.signal.aborted) timer = window.setTimeout(load, 5000); }
    }
    setRunError(""); void load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [session, refresh, tab, runtimeVersion]);
  useEffect(() => {
    if (!selected || tab !== "runs") return;
    const controller = new AbortController();
    setLedgerError("");
    readMetrics<ExecutionLedger>(`/api/agent/runs/${selected.id}/ledger`, controller.signal).then((data) => {
      if (!controller.signal.aborted) { setLedgerData(data); setLoadedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false })); }
    }).catch((e) => { if (!controller.signal.aborted) setLedgerError(e.message); });
    return () => controller.abort();
  }, [selected?.id, selected?.status, selected?.updated_at, runData, refresh, tab]);
  return <section className="metrics-dashboard">
    <nav className="metrics-tabs" aria-label="指标视图"><button aria-pressed={tab === "runs"} onClick={() => setTab("runs")}>本轮执行</button><button aria-pressed={tab === "reports"} onClick={() => setTab("reports")}>历史评测</button></nav>
    {tab === "reports" ? <ReportHistory /> : <>
      <div className="metrics-toolbar metrics-filters">
        <label>会话<select aria-label="会话" value={session} onChange={(event) => { setSession(event.target.value); setPicked(""); setRunData(null); setLedgerData(null); }}>
          {runtime.sessions.length ? runtime.sessions.map((item) => <option key={item.id} value={item.id}>{item.title}</option>) : <option value={session}>当前会话</option>}
        </select></label>
        <label className="metrics-run-filter">执行轮次<select aria-label="执行轮次" value={picked} onChange={(event) => { setPicked(event.target.value); setLedgerData(null); }}><option value="">最新一轮（自动跟随）</option>{runs.map((run) => <option value={run.id} key={run.id}>{dateText(run.created_at)} · {runKind(run.kind)} · {statusText(run.status)} · {run.id.slice(0, 8)}</option>)}</select></label>
        <button className="button secondary compact" onClick={() => setRefresh((n) => n + 1)}>刷新数据</button>
      </div>
      <p className="metrics-help">展示所选会话最近 500 轮。每 5 秒更新，任务结束后自动读取最终记录。{loadedAt && ` 最近读取 ${loadedAt}。`}</p>
      {runError && <p className="metrics-error" role="alert">{runError}。当前展示可能是上次数据，请刷新重试。</p>}
      {!runData && !runError && <p role="status">正在读取运行记录…</p>}
      {runData && !selected && <div className="metrics-empty">{picked ? "该轮记录暂不可见，请切换到最新一轮或刷新数据。" : "这个会话还没有执行记录。完成一次 Agent 对话后，执行数据会显示在这里。"}</div>}
      {selected && <>
        <header className="metrics-run-heading"><div><StateBadge status={ledger?.status ?? selected.status} /><h2>{runKind(selected.kind)}</h2><p>开始于 {dateText(selected.created_at)} <span className="metrics-run-id">任务编号 {selected.id}</span></p></div><span className="metrics-live-label">{terminalRun(selected.status) ? "本轮已结束" : "执行记录持续更新"}</span></header>
        {ledgerError && <p className="metrics-error" role="alert">{ledgerError}。账本暂不可用，刷新后重试。</p>}
        {!ledger && !ledgerError && <p role="status">正在读取本轮指标…</p>}
        {ledger && <ExecutionDetails ledger={ledger} />}
      </>}
    </>}
  </section>;
}
