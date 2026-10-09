"use client";

import { useState } from "react";
import type { AgentRun, RunEvent } from "@/lib/agent-runtime";
import { terminalRun } from "@/lib/agent-runtime";
import { chatDisplay } from "@/lib/chat-display";
import type { useAgentRuntime } from "@/lib/use-agent-runtime";

const labels: Record<string, string> = { queued: "排队中", running: "执行中", waiting_user: "等待接管",
  waiting_confirmation: "等待审核", cancelling: "正在停止", completed: "完成", cancelled: "已停止",
  waiting_reply: "等待回复", pending: "等待前置步骤", claimed: "已领取，执行中", waiting_external: "等待搜索完成",
  skipped: "前置失败，未执行", blocked: "条件或授权不足",
  interrupted: "已中断", timed_out: "超时", failed: "失败", outcome_indeterminate: "待核验" };

function stepDescription(event: RunEvent): string {
  const data = event.data;
  if (event.kind === "tool_retry" || event.kind === "model_retry")
    return `${event.kind === "tool_retry" ? data.tool : "模型请求"} · ${data.code} · 第 ${data.next_attempt}/3 次尝试，退避 ${data.delay_seconds} 秒（仅安全读取/推理）`;
  if (event.kind === "plan_repair")
    return data.repair_kind === "draft_format" ? "规划格式无效，进行一次修复；尚未执行工具" : data.repair_kind === "transport_metadata" ? "移除格式元数据；仍校验目标、范围和授权" : `${data.tool} · 修复只读参数编码，不改变范围`;
  if (event.kind === "request_resolution") return `上下文续接 · ${data.action}${data.detail ? ` · ${data.detail}` : ""}`;
  if (event.kind === "request_anchor") return `原请求定位 · ${data.status} · ${data.detail}`;
  if (event.kind === "turn_understanding") return `输入理解 · ${data.role} · ${data.new_task ? "新任务" : "不替换原任务"}`;
  if (event.kind === "request_understanding") return "已按结构化上下文理解请求；无需模型调用，尚未执行";
  if (event.kind === "watchdog_expired") return `安全停止 · ${data.code}；未自动重放动作`;
  if (event.kind === "plan_failed") return `规划未执行 · ${data.code} · ${data.detail}`;
  return String(data.detail || (event.kind === "request_plan" ? `已规划 ${(data.steps as unknown[] | undefined)?.length ?? 0} 个步骤` : event.kind === "plan_step" ? `${data.step_id} · ${data.tool} · ${labels[String(data.status)] ?? data.status}` : event.kind === "tool_started" ? `调用 ${data.tool}` : event.kind === "tool_finished" ? `${data.tool} · ${data.status}` : event.kind === "response" ? "已返回回复" : event.kind === "browser_summary" ? `搜索摘要：发现 ${data.discovered}，保存 ${data.stored}` : event.kind === "browser" ? "浏览器状态已更新" : event.kind));
}

function Steps({ run }: { run: AgentRun }) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [error, setError] = useState("");
  async function load() {
    try {
      let after = 0;
      const collected: RunEvent[] = [];
      while (true) {
        const response = await fetch(`/api/agent/runs/${run.id}/steps?after=${after}`, { cache: "no-store" });
        if (!response.ok) throw new Error("步骤读取失败");
        const batch = await response.json() as RunEvent[];
        collected.push(...batch);
        if (batch.length < 200) break;
        after = batch[batch.length - 1].seq;
      }
      setEvents(collected.filter((event) => event.kind !== "delta"));
    } catch { setError("暂时无法读取步骤"); }
  }
  return <details onToggle={(event) => { if (event.currentTarget.open) void load(); }}>
    <summary>执行步骤</summary>
    {error && <p>{error}</p>}
    <ol>{events.map((event) => <li key={event.seq}>
      {stepDescription(event)}
      {Boolean(event.data.failure) && <small> · 错误码 {String((event.data.failure as Record<string, unknown>).code)} · {(event.data.failure as Record<string, unknown>).uncertain ? "结果未知，请人工核对" : "步骤已停止"}</small>}
    </li>)}</ol>
  </details>;
}

export function RuntimePanel({ runtime, notice, showSession = true }: { runtime: ReturnType<typeof useAgentRuntime>; notice: (text: string) => void; showSession?: boolean }) {
  return <div className="runtime-panel">
    {showSession && <div className="runtime-session">
      <select aria-label="当前会话" value={runtime.sessionId} onChange={(event) => runtime.setSessionId(event.target.value)}>
        {runtime.sessions.length ? runtime.sessions.map((session) => <option key={session.id} value={session.id}>{session.title}</option>) : <option value="default">默认会话</option>}
      </select>
      <button className="button secondary compact" onClick={() => void runtime.newSession().catch((error) => notice(error.message))}>新会话</button>
    </div>}
    {runtime.searchState && <details className="runtime-history">
      <summary>本会话搜索条件 · {runtime.searchState.query_source === "session" ? "会话指定" : runtime.searchState.query_source === "preference" ? "已保存偏好" : "当前简历"}</summary>
      <p>{runtime.searchState.queries.join(" · ") || "尚未指定岗位"}</p>
      <p>{runtime.searchState.locations.join("、") || "不限城市"} · 数量 {runtime.searchState.result_limit ?? "默认"} · 最低月薪 {runtime.searchState.salary_min ? `${runtime.searchState.salary_min.toLocaleString()} 元` : "不限"}</p>
      <small>只影响后续搜索，不改变正在运行的任务或长期偏好。</small>
      <button className="button secondary compact" disabled={Boolean(runtime.busyRun)} onClick={() => void runtime.resetSearchState().catch((error) => notice(error.message))}>重置本会话条件</button>
    </details>}
    {runtime.dialogue && <details className="runtime-history">
      <summary>上下文状态 · {({ search: "职位搜索", applications: "申请审核", resume: "简历", learning: "学习计划", general: "未激活任务" } as Record<string, string>)[runtime.dialogue.active_intent] ?? runtime.dialogue.active_intent}</summary>
      <small>v{runtime.dialogue.revision} · 上下文和搜索条件持续保留并按新指令更新；待确认动作独立过期。</small>
      {Object.entries(runtime.dialogue.frames).map(([topic, frame]) => <p key={topic}>{frame.last_question || "参数已记录"}{frame.missing_slots.length ? ` · 待补：${frame.missing_slots.join("、")}` : ""}</p>)}
      {runtime.dialogue.pending.map((item) => <p key={item.id}>待确认：{item.description}</p>)}
      <details><summary>精简轨迹与来源</summary>{runtime.dialogue.trace.map((item, index) => <p key={`${index}-${item.source}`}>{item.kind} {item.tool ?? ""} {item.status ?? ""} · {item.source.slice(0, 8)}</p>)}</details>
      <button className="button secondary compact" disabled={Boolean(runtime.busyRun)} onClick={() => void runtime.resetContext().catch((error) => notice(error.message))}>重置临时上下文</button>
    </details>}
    <details className="runtime-history" open={Boolean(runtime.busyRun)}>
      <summary>运行记录{runtime.runs.some((run) => !terminalRun(run.status)) ? " · 有任务进行中" : ""}</summary>
      {runtime.runs.slice(0, 8).map((run) => <article key={run.id} className="runtime-run">
        <div><strong>{run.kind === "learning_plan" ? "学习计划" : run.kind === "delivery" ? "简历投递" : run.kind === "browser" ? "职位搜索" : run.kind === "job_analysis" ? "职位分析" : "Agent 任务"}</strong><span>{labels[run.status] ?? run.status}</span></div>
        <small>{new Date(run.created_at).toLocaleString("zh-CN")} · {run.id.slice(0, 8)}</small>
        <p role="status">{runtime.progress[run.id] ?? run.error ?? "步骤已保存"}</p>
        {!!run.plan_steps?.length && <ol>{run.plan_steps.map((step) => <li key={step.id}>
          {step.tool} · {labels[step.status] ?? step.status}{step.depends_on.length > 0 && ` · 依赖 ${step.depends_on.join("、")}`}
        </li>)}</ol>}
        {typeof run.result?.message === "string" && <details><summary>查看回复</summary><p>{chatDisplay(run.result.message)}</p></details>}
        <div className="runtime-actions">
          {!terminalRun(run.status) && <button className="button secondary compact" onClick={() => void runtime.control(run, "cancel").catch((error) => notice(error.message))}>停止任务</button>}
          {run.can_resume && <button className="button primary compact" onClick={() => void runtime.control(run, "resume").catch((error) => notice(error.message))}>{run.status === "waiting_user" ? "确认登录，继续" : run.status === "waiting_reply" ? "检查回复" : "恢复执行"}</button>}
          {!terminalRun(run.status) && <button className="text-button" onClick={() => runtime.observe(run)}>重新连接</button>}
        </div>
        <Steps run={run} />
      </article>)}
      {!runtime.runs.length && <p>当前会话没有运行记录。</p>}
    </details>
  </div>;
}
