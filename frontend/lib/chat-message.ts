import type { Message } from "./types";

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
