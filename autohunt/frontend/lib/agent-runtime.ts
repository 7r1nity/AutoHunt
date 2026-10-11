export type AgentRun = {
  id: string; session_id: string; kind: "chat" | "learning_plan" | "delivery" | "job_analysis" | "browser"; status: string;
  result: Record<string, unknown> | null; error: string | null; can_resume: boolean;
  browser_id: string | null; created_at: string;
  plan_steps?: Array<{ id: string; tool: string; depends_on: string[]; status: string; detail?: string }>;
};

export type RunEvent = { seq: number; kind: string; data: Record<string, unknown> };

export const terminalRun = (status: string) => ["completed", "failed", "cancelled", "timed_out", "interrupted", "outcome_indeterminate"].includes(status);

// Fully consumed control commands only: questions, negation, quotes and mixed
// requests must not cancel a task just because they mention stopping.
export const interruptionScope = (text: string): "session" | "all" | null => {
  const match = /^(?:请|帮我|先|请先|先帮我)?(?:中断|停止|取消|终止|结束)(所有|全部)?(?:当前|本次|这个|正在执行的|正在运行的)?(?:任务|执行|请求|搜索|投递|自动投递|沟通|浏览器任务)?[。！!]*$/.exec(text.replace(/\s+/g, ""));
  return match ? match[1] ? "all" : "session" : null;
};
export const interruptionCommand = (text: string) => interruptionScope(text) !== null;

export async function createRun(conversation_id: string, kind: AgentRun["kind"], payload: Record<string, string>) {
  const response = await fetch("/api/agent/runs", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversation_id, request_id: crypto.randomUUID(), kind, ...payload }) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail ?? "创建任务失败");
  return data as AgentRun;
}

export async function followRun(runId: string, onEvent: (event: RunEvent) => void, signal?: AbortSignal) {
  let cursor = 0;
  let attempts = 0;
  while (!signal?.aborted) {
    try {
      const response = await fetch(`/api/agent/runs/${runId}/events?after=${cursor}`, { signal, cache: "no-store" });
      if (!response.ok || !response.body) throw new Error("事件连接失败");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      try {
        while (true) {
          const { done, value } = await reader.read();
          buffer += decoder.decode(value, { stream: !done });
          let boundary: number;
          while ((boundary = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            const line = frame.split("\n").find((part) => part.startsWith("data: "));
            if (!line) continue;
            const event = JSON.parse(line.slice(6)) as RunEvent;
            if (event.seq <= cursor) continue;
            cursor = event.seq;
            attempts = 0;
            onEvent(event);
            if (event.kind === "status" && terminalRun(String(event.data.status))) return;
          }
          if (done) break;
        }
      } finally {
        await reader.cancel().catch(() => undefined);
        reader.releaseLock();
      }
      const snapshot = await fetch(`/api/agent/runs/${runId}`, { signal, cache: "no-store" });
      if (snapshot.ok) {
        const run = await snapshot.json() as AgentRun;
        if (terminalRun(run.status)) {
          // A dropped final frame must still settle the waiting chat promise.
          if (run.result) onEvent({ seq: cursor, kind: "response", data: { result: run.result } });
          onEvent({ seq: cursor, kind: "status", data: { status: run.status, detail: run.error ?? "任务已结束" } });
          return;
        }
      }
      throw new Error("事件流已断开");
    } catch (error) {
      if (signal?.aborted) return;
      if (++attempts > 3) throw error;
      await new Promise((resolve) => window.setTimeout(resolve, attempts * 1000));
    }
  }
}
