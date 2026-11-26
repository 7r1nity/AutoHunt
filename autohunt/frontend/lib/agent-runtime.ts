export type AgentRun = {
  id: string; session_id: string; kind: "chat" | "delivery" | "job_analysis" | "browser" | "daily"; status: string;
  daily?: { trigger: "manual" | "scheduled" | "catch_up"; scheduled_for?: string; coalesced_occurrences?: number } | null;
  result: Record<string, unknown> | null; error: string | null; can_resume: boolean;
  browser_id: string | null; created_at: string; updated_at?: string;
  plan_steps?: Array<{ id: string; tool: string; depends_on?: string[] | null; status: string; detail?: string }>;
  approvals?: Array<{ id: string; tool: string; arguments: Record<string, unknown>; detail: string }>;
  target_choices?: TargetChoice[];
};

export type TargetChoice = { id: string; step_id: string; expected_revision: number; selection_mode: "single" | "matching";
  options: Array<{ id: number; title: string; company: string; location?: string; salary?: string; score?: number }> };

export type RunEvent = { seq: number; kind: string; data: Record<string, unknown> };

export type TaskResults = {
  message: string; result_message?: string; status?: string; conversation_id?: string;
  snapshot_id?: string; snapshot?: Record<string, unknown> | null;
  message_id?: string | null; run_id?: string; user_message_id?: string | null;
};

export const terminalRun = (status: string) => ["completed", "failed", "cancelled", "timed_out", "interrupted", "outcome_indeterminate"].includes(status);

// Fully consumed control commands only: questions, negation, quotes and mixed
// requests must not cancel a task just because they mention stopping.
export const interruptionScope = (text: string): "session" | "all" | null => {
  const normalized = text.trim().replace(/\s+/g, " ").replace(/[.!！。]+$/, "");
  const match = /^(?:请|帮我|先|请先|先帮我)?(?:停止|中断|取消|终止|结束)(所有|全部)?(?:当前|本次|这个|正在执行的|正在运行的)?(?:(?:每日|每天|定时)?任务|执行|请求|搜索|投递|自动投递|沟通|浏览器任务)?(?:(?:并且|并|然后|，|,)(?:把|将)?(?:当前|目前|已有|已找到的|本轮)?(?:结果|进度|岗位|职位)(?:返回|发给我|给我|显示出来)|(?:并且|并|然后|，|,)(?:返回|显示|汇总|给我|发给我)(?:当前|目前|已有|本轮)?(?:结果|进度|岗位|职位))?$/.exec(normalized.replace(/\s+/g, ""));
  if (match) return match[1] ? "all" : "session";
  const english = /^(?:please )?(?:stop|cancel|interrupt|halt|abort|terminate)(?: (all|everything)(?: (?:tasks?|jobs?|applications?|work|searches?))?| (?:(?:the|my) )?(?:current )?(?:task|job|search|applications?|work|execution))?(?: (?:for now|now|please))?(?:[,;]?(?: and)? (?:show|return|give me)(?: the)? (?:current |partial )?(?:results|progress))?$/i.exec(normalized);
  return english ? english[1] ? "all" : "session" : null;
};
export const interruptionCommand = (text: string) => interruptionScope(text) !== null;

// Only complete read requests bypass foreground work. Mentioning progress in a
// new search, a condition amendment or a quoted example does not authorize it.
export const progressCommand = (text: string): boolean => {
  const chinese = text.replace(/\s+/g, "").replace(/[。！!？?．.]+$/, "");
  const english = text.replace(/\s+/g, " ").trim().replace(/[.?!！。？]+$/, "");
  return /^(?:(?:请|帮我|先|麻烦)?(?:查看|看看|查询|汇报|报告|给我|告诉我|展示|显示)?(?:一下)?(?:当前|目前|现在|本轮|这轮|本次|这次)?(?:的)?(?:任务|搜索|申请|投递|沟通|求职)?(?:的)?(?:进度|状态|结果|统计|汇总|总结|阶段性结果|已找到的岗位|已找到的职位)(?:如何|怎么样|怎样|到哪了|是什么|有哪些|呢)?|现在在干什么|进行到哪了|进展如何|进度如何|进度怎么样了|搜索进度到哪了|(?:已经|已|目前|现在|本轮|这轮|这次|一共|今天)?(?:已经|已)?(?:找到|搜到|找|搜索到|保存|核对|投递|投|沟通|申请)(?:了)?(?:多少|几个)(?:个|份)?(?:岗位|职位|工作|简历)?(?:了|呢)?)$/.test(chinese)
    || /^(?:please )?(?:(?:(?:show|tell) me )?(?:(?:the|my) )?(?:(?:current|latest|task|search|application) )?(?:status|progress|results?|summary|statistics)|partial results|(?:show|give me|return)(?: the| my)?(?: current| partial)? (?:results|progress|status)|how(?:['’]s| is) it going|what(?:['’]s| is) (?:the )?(?:(?:current|task|search|application) )?(?:status|progress)|how many (?:jobs|applications)(?: (?:(?:have you )?(?:found|saved|contacted|applied to)|so far|today))?|how many jobs (?:have you (?:found|applied to)|did you (?:find|apply to))(?:(?: so far| today))?)$/i.test(english);
};

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
