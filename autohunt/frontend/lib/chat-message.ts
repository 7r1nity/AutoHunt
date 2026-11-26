import type { Message } from "./types";
import type { TaskResults } from "./agent-runtime";

// Message identity must not depend on timestamps: streaming, attachments and
// browser notifications can all create messages within the same millisecond.
export function createMessageId(): Message["id"] {
  return `message:${globalThis.crypto.randomUUID()}`;
}

export function createChatTurn(text: string, time: string): [Message, Message] {
  return [
    { id: createMessageId(), author: "user", text, time },
    { id: createMessageId(), author: "agent", text: "正在处理…", time },
  ];
}

// A result can arrive through HTTP, a stop poll and the persisted event stream.
// Replace its temporary row and fold copies by durable identity, not by job order.
export function mergeTaskResults(messages: Message[], result: TaskResults, placeholderId: string, time: string): Message[] {
  const id = result.message_id ? `result:${result.message_id}` : placeholderId;
  const text = result.result_message ?? result.message;
  let replaced = false;
  const next = messages.flatMap((item) => {
    const matches = item.author === "agent" && (item.id === placeholderId || item.id === id
      || Boolean(result.run_id && item.sourceRunId === result.run_id)
      || Boolean(!result.message_id && result.snapshot_id && item.snapshotId === result.snapshot_id));
    if (!matches) return [item];
    if (replaced) return [];
    replaced = true;
    return [{ ...item, id, text, time, snapshotId: result.snapshot_id, sourceRunId: result.run_id }];
  });
  return replaced ? next : [...next, { id, author: "agent", text, time,
    snapshotId: result.snapshot_id, sourceRunId: result.run_id }];
}
