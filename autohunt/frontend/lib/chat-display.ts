// Last-resort display guard for legacy/cached responses. Never alter user text,
// tool records, action payloads or ordinary JSON examples unrelated to tools.
export function chatDisplay(text: string): string {
  const toolDump = /(?:browser_task_(?:stop|pause|resume|read)|context_(?:state_read|history_read|reference_resolve)|search_state_(?:read|update)|job_(?:results_read|stats_read|search_start))\s*[：:]\s*(?:```(?:json|python)?\s*)?[\[{]/i;
  const envelope = /["']tool["']\s*:\s*["'][a-z_]+["']/i;
  const internal = /["'](?:login_evidence|selector_version|tool_events|resume_stamp|shared_browser_busy|omitted)["']\s*:/;
  if (toolDump.test(text) || internal.test(text) || envelope.test(text) && /["'](?:result|failure)["']\s*:/.test(text)) {
    return "这条回复包含内部工具数据，请在「任务与上下文」的运行记录中核对结果；不能据此确认任务已完成。";
  }
  try {
    const value = JSON.parse(text.replace(/^```(?:json)?\s*|\s*```$/g, "").trim()) as Record<string, unknown>;
    if (value && !Array.isArray(value)) {
      const reply = value.message ?? value.answer ?? value.reply;
      if (typeof reply === "string" && reply !== text) return chatDisplay(reply);
    }
  } catch { /* Normal prose/Markdown is intentionally unchanged. */ }
  return text;
}
