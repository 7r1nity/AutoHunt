export const runLabels: Record<string, string> = {
  queued: "排队中", running: "执行中", completed: "已完成", failed: "失败", error: "错误", ok: "成功",
  waiting_user: "等待接管", waiting_reply: "等待招聘方回复", waiting_confirmation: "等待审核",
  waiting_approval: "等待范围确认", waiting_external: "等待外部任务", waiting_input: "等待补充信息",
  awaiting_user: "等待用户处理", cancelled: "已停止", cancelling: "停止中", interrupted: "已中断",
  timed_out: "超时", outcome_indeterminate: "结果待核验", blocked: "条件不足", pending: "等待前置步骤",
  claimed: "执行中", skipped: "未执行", incomplete: "记录不完整", succeeded: "成功",
};
export const statusText = (status: string) => runLabels[status] ?? status;
export const runKind = (kind: string) => ({ chat: "Agent 对话", browser: "职位搜索", delivery: "简历投递", job_analysis: "职位分析", daily: "每日模拟站任务" }[kind] ?? "Agent 任务");
export const numberText = (n: number | null | undefined) => n == null ? "未记录" : n.toLocaleString("zh-CN");
export function durationText(ms: number | null | undefined) {
  if (ms == null) return "未记录";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(2)} 秒`;
  return `${Math.floor(ms / 60000)} 分 ${Math.floor(ms % 60000 / 1000)} 秒`;
}
export const dateText = (date: string) => date.length === 10 ? date : new Date(date).toLocaleString("zh-CN", { hour12: false });

export type ExecutionLedger = {
  run_id: string; status: string; created_at: string; updated_at: string; error: string | null;
  execution_path?: { mode: "local_rule"; reason: string; detail: string; source_browser_id?: string | null } | null;
  calls: Array<{ call_id: string; provider: string | null; model: string | null; operation: string | null;
    status: string; latency_ms: number | null; prompt_tokens: number | null; completion_tokens: number | null; error_type: string | null }>;
  tools: Array<{ tool: string; status: string; duration_ms: number | null; attempts: number | null }>;
  steps: Array<{ id: string; tool: string; status: string; plan_id: string | null; revision: number | null }>;
  issues: Array<{ seq: number; kind: string; tool: string | null; step_id: string | null; code: string | null; status?: string | null; detail: string; uncertain: boolean }>;
  accounting: { recorded_model_calls: number; model_retries: number; tool_retries: number;
    known_prompt_tokens: number; known_completion_tokens: number; tokens_complete: boolean;
    calls_without_complete_usage: number; calls_without_latency: number; retry_attempts_without_usage: number;
    model_time_sum_ms: number; persisted_run_span_ms: number | null };
};

export type EvaluationReport = { id: string; title: string; date: string; category: string; scope: string;
  status: "passed" | "failed" | "pending" | "recorded";
  metrics?: Array<{ label: string; value: unknown; format: string }>; document?: string | null; data?: Record<string, unknown> };

export function metricText(value: unknown, format: string): string {
  if (value == null) return "暂无数据";
  if (format === "ratio" && typeof value === "object") {
    const ratio = value as { successes?: number; total?: number; rate?: number };
    return `${ratio.successes ?? "?"} / ${ratio.total ?? "?"}${typeof ratio.rate === "number" ? `（${(ratio.rate * 100).toFixed(1)}%）` : ""}`;
  }
  if (typeof value !== "number") return String(value);
  return format === "percent" ? `${(value * 100).toFixed(2)}%` : format === "score" ? value.toFixed(4) : format === "ms" ? durationText(value) : numberText(value);
}

export async function readMetrics<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal, cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "读取失败，请刷新重试");
  return data as T;
}
