import assert from "node:assert/strict";
import { test } from "node:test";
import { inventoryRequest, inventoryError } from "../lib/inventory-request.ts";

test("rate limits and authentication errors are not misreported as stopped backend", async () => {
  assert.match(await inventoryError(new Response("", { status: 429 })), /限流/);
  assert.match(await inventoryError(new Response("", { status: 401 })), /认证/);
  assert.match(await inventoryError(new Response(JSON.stringify({detail: "Next.js server has no backend token configured"}), {status: 503})), /密钥未配置/);
  assert.match(await inventoryError(new Response("Internal Server Error", {status: 500})), /HTTP 500/);
});

test("transient read errors retry and recover without mutation", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.method, undefined);
    assert.equal(options.cache, "no-store");
    return ++calls === 1 ? new Response("", {status: 429, headers: {"Retry-After": "0.001"}})
      : new Response("[]", {status: 200});
  };
  try { assert.equal((await inventoryRequest("/api/agent/materials")).status, 200); assert.equal(calls, 2); }
  finally { globalThis.fetch = originalFetch; }
});

test("retry waits can be cancelled when leaving the resume page", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("", {status: 429, headers: {"Retry-After": "60"}});
  const controller = new AbortController();
  try {
    const result = inventoryRequest("/api/agent/materials", controller.signal);
    await Promise.resolve();
    controller.abort();
    await assert.rejects(result, {name: "AbortError"});
  } finally { globalThis.fetch = originalFetch; }
});
