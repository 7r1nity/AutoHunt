import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { createChatTurn, createMessageId } from "../lib/chat-message.ts";

test("concurrent message sources stay unique even with a frozen clock", () => {
  const originalNow = Date.now;
  Date.now = () => 1790995090222;
  try {
    const ids = Array.from({ length: 10_000 }, () => createMessageId());
    assert.equal(new Set(ids).size, ids.length);
    assert.ok(ids.every((id) => id.startsWith("message:")));
  } finally {
    Date.now = originalNow;
  }
});

test("crossing a millisecond cannot give the user and reply the same identity", () => {
  const originalNow = Date.now;
  let clock = 1790995090221;
  Date.now = () => clock++;
  try {
    const [user, reply] = createChatTurn("帮我找工作", "12:00");
    assert.notEqual(user.id, reply.id);
    assert.equal(user.author, "user");
    assert.equal(reply.author, "agent");
    assert.equal(reply.text, "正在处理…");
  } finally {
    Date.now = originalNow;
  }
});

test("streaming and final responses update only the intended placeholder", () => {
  const turn = createChatTurn("搜索金融风控岗位", "12:00");
  const replyId = turn[1].id;
  const notification = { id: createMessageId(), author: "agent", text: "搜索报告" };
  let messages = [...turn, notification];
  for (const text of ["正在搜索", "搜索已完成"]) {
    messages = messages.map((message) => message.id === replyId ? { ...message, text } : message);
  }
  assert.equal(messages[0].text, "搜索金融风控岗位");
  assert.equal(messages[1].text, "搜索已完成");
  assert.equal(messages[2].text, "搜索报告");
  assert.equal(new Set(messages.map((message) => message.id)).size, messages.length);
});

test("chat entry points no longer use timestamp arithmetic for message IDs", () => {
  const page = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(page, /AutoHunt Agent 保持静默/);
  assert.doesNotMatch(page, /\bid:\s*(?:Date\.now\(\)|userMessageId\s*\+|createdAt\s*\+|index\s*\+\s*1)/);
  assert.doesNotMatch(page, /const (?:messageId|userMessageId) = Date\.now/);
  assert.match(page, /const turn = createChatTurn\(value, now\(\)\)/);
  assert.match(page, /key=\{message\.id\}/);
});
