// Offline contracts for read-only snapshot refresh and SSE notification delivery.
// Run: node agent/evaluation/frontend_snapshot_contract.cjs (from autohunt/).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { createRequire } = require("node:module");
const vm = require("node:vm");
const { performance } = require("node:perf_hooks");

const frontend = path.resolve(__dirname, "../../frontend");
const frontendRequire = createRequire(path.join(frontend, "package.json"));
const ts = frontendRequire("typescript");
const sources = ["lib/latest-snapshot.ts", "lib/agent-runtime.ts", "lib/use-agent-runtime.ts"];
const hashes = () => Object.fromEntries(sources.map(name => [name,
  crypto.createHash("sha256").update(fs.readFileSync(path.join(frontend, name))).digest("hex")]));
const before = hashes();
function load(name, overrides = {}) {
  const exports = {};
  const compiled = ts.transpileModule(fs.readFileSync(path.join(frontend, name), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(compiled, { exports, require: frontendRequire, AbortController,
    setTimeout, clearTimeout, console, ...overrides }, { filename: name });
  return exports;
}
const { latestSnapshot } = load("lib/latest-snapshot.ts");
const runtimeExports = load("lib/agent-runtime.ts");
const turn = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
};
const cases = [];
const add = (name, run) => cases.push({ name, run });
let disposables = [];
function lane(options) {
  const value = latestSnapshot({ timeoutMs: 500, ...options });
  disposables.push(value);
  return value;
}

add("concurrent_polls_share_one_read_and_one_publication", async () => {
  const gate = deferred(), published = [], active = [];
  let reads = 0;
  const refresh = lane({ read: () => { reads++; return gate.promise; }, publish: v => published.push(v),
    failed: e => { throw e; }, refreshing: value => active.push(value) });
  const first = refresh.refresh();
  const polls = Array.from({ length: 25 }, () => refresh.refresh());
  assert(polls.every(promise => promise === first));
  await turn();
  assert.equal(reads, 1);
  gate.resolve({ stage: "applying" });
  await Promise.all([first, ...polls]);
  assert.equal(published.length, 1);
  assert.equal(published[0].stage, "applying");
  assert.deepEqual(active, [true, false]);
});

add("event_invalidation_drops_pending_old_snapshot_then_reads_fresh", async () => {
  const gates = [deferred(), deferred()], published = [], failures = [];
  let reads = 0;
  const refresh = lane({ read: () => gates[reads++].promise, publish: v => published.push(v),
    failed: e => failures.push(e), refreshing() {} });
  const first = refresh.refresh();
  await turn();
  const event = refresh.refresh(true);
  gates[0].resolve({ stage: "review", source: "old" });
  await turn();
  assert.equal(reads, 2);
  assert.equal(published.length, 0);
  gates[1].resolve({ stage: "applying", source: "fresh" });
  await Promise.all([first, event]);
  assert.equal(published.length, 1);
  assert.equal(published[0].source, "fresh");
  assert.equal(failures.length, 0);
});

add("burst_of_delivery_events_coalesces_to_one_followup_read", async () => {
  const gates = [deferred(), deferred()], published = [];
  let reads = 0;
  const refresh = lane({ read: () => gates[reads++].promise, publish: v => published.push(v), failed() {}, refreshing() {} });
  const first = refresh.refresh();
  await turn();
  const events = Array.from({ length: 100 }, () => refresh.refresh(true));
  gates[0].resolve("old");
  await turn();
  assert.equal(reads, 2);
  gates[1].resolve("fresh");
  await Promise.all([first, ...events]);
  assert.equal(reads, 2);
  assert.deepEqual(published, ["fresh"]);
});

add("timeout_aborts_read_is_bounded_and_next_refresh_recovers", async () => {
  const first = deferred(), failures = [];
  let reads = 0, signal, value = "previous";
  const refresh = lane({ timeoutMs: 25, read: nextSignal => {
    signal = nextSignal;
    return ++reads === 1 ? first.promise : Promise.resolve("recovered");
  }, publish: next => { value = next; }, failed: e => failures.push(e), refreshing() {} });
  const started = performance.now();
  const attempt = refresh.refresh();
  await turn();
  const firstSignal = signal;
  await attempt;
  assert(performance.now() - started < 1000, "timeout must bound an uncooperative reader");
  assert.equal(firstSignal.aborted, true);
  assert.equal(value, "previous");
  assert.equal(failures.length, 1);
  assert.match(String(failures[0]), /timed out/i);
  await refresh.refresh();
  assert.equal(value, "recovered");
  first.resolve("late obsolete response");
  await turn();
  assert.equal(value, "recovered");
});

add("malformed_reader_rejection_retains_prior_snapshot_and_recovers", async () => {
  let payload = { unexpected: "not an array" }, value = [{ id: 1, stage: "applying" }];
  const failures = [];
  const refresh = lane({ read: async () => {
    // Validation belongs to the reader. The helper must not publish its failure
    // as a fabricated empty list or erase an already observed application.
    if (!Array.isArray(payload)) throw new Error("Malformed jobs snapshot");
    return payload;
  }, publish: next => { value = next; }, failed: e => failures.push(e), refreshing() {} });
  const previous = value;
  await refresh.refresh();
  assert.strictEqual(value, previous);
  assert.equal(failures.length, 1);
  payload = [{ id: 1, stage: "waiting" }];
  await refresh.refresh();
  assert.equal(value[0].stage, "waiting");
});

add("dispose_aborts_pending_read_and_prevents_late_publication", async () => {
  const gate = deferred(), published = [], failures = [];
  let reads = 0, signal;
  const refresh = lane({ read: next => { signal = next; reads++; return gate.promise; },
    publish: v => published.push(v), failed: e => failures.push(e), refreshing() {} });
  const pending = refresh.refresh();
  await turn();
  refresh.dispose();
  assert.equal(signal.aborted, true);
  gate.resolve("late snapshot after unmount");
  await pending;
  await refresh.refresh(true);
  assert.equal(reads, 1);
  assert.equal(published.length, 0);
  assert.equal(failures.length, 0);
});

add("idle_callback_reentrant_refresh_is_not_lost", async () => {
  const published = [], active = [];
  let reads = 0, reentered = false, second;
  const refresh = lane({ read: async () => ++reads, publish: v => published.push(v), failed: e => { throw e; },
    refreshing: value => {
      active.push(value);
      if (!value && !reentered) { reentered = true; second = refresh.refresh(true); }
    } });
  await refresh.refresh();
  assert(second, "the idle callback must enqueue its refresh");
  await second;
  assert.equal(reads, 2);
  assert.deepEqual(published, [1, 2]);
  assert.deepEqual(active, [true, false, true, false]);
});

add("synchronous_refresh_start_invalidation_does_not_open_parallel_lane", async () => {
  let reads = 0, invalidated = false;
  const published = [];
  const refresh = lane({ read: async () => ++reads, publish: v => published.push(v), failed: e => { throw e; },
    refreshing: value => { if (value && !invalidated) { invalidated = true; void refresh.refresh(true); } } });
  await refresh.refresh();
  assert.equal(reads, 1);
  assert.deepEqual(published, [1]);
});

add("all_seven_inventory_event_types_keep_original_payload_and_sequence", () => {
  for (const kind of ["browser", "delivery", "status", "plan_step", "response", "browser_summary", "workflow_summary"]) {
    const event = Object.freeze({ seq: 23, kind, data: Object.freeze({ task: { id: "synthetic-task" },
      result: { message: "synthetic saved result" } }) });
    assert.equal(runtimeExports.changesJobInventory(event), true, kind);
    assert.strictEqual(runtimeExports.notificationForRunEvent(event, false), event, kind);
    if (kind !== "response") assert.strictEqual(runtimeExports.notificationForRunEvent(event, true), event, kind);
  }
});

add("foreground_response_only_notifies_invalidation_without_duplicate_reply", () => {
  const event = { seq: 24, kind: "response", data: { result: { message: "foreground reply" }, detail: "done" } };
  const notification = runtimeExports.notificationForRunEvent(event, true);
  assert.equal(notification.seq, event.seq);
  assert.equal(notification.kind, "response");
  assert.equal(notification.data.inventory_changed, true);
  assert.equal(notification.data.result, undefined);
  assert.equal(notification.data.detail, undefined);
  assert.equal(event.data.result.message, "foreground reply");
  for (const kind of ["progress", "delta", "search_state", "model_request"]) {
    assert.equal(runtimeExports.changesJobInventory({ kind }), false);
    assert.equal(runtimeExports.notificationForRunEvent({ seq: 25, kind, data: {} }, true), null);
  }
});

function observeHarness() {
  const state = [], refs = [], readers = [], requests = [];
  const react = {
    useCallback: callback => callback,
    useEffect() {}, // No mounting/network effects are needed for observe().
    useRef: value => { const reference = { current: value }; refs.push(reference); return reference; },
    useState: value => {
      const cell = { value };
      state.push(cell);
      return [value, update => { cell.value = typeof update === "function" ? update(cell.value) : update; }];
    },
  };
  const source = { ...runtimeExports,
    createRun: async (session_id, kind) => ({ id: "synthetic-foreground", session_id, kind, status: "running" }),
    followRun: (id, receive, signal) => new Promise(resolve => readers.push({ id, receive, signal, resolve })),
  };
  const hook = load("lib/use-agent-runtime.ts", {
    require: name => name === "react" ? react : name === "./agent-runtime" ? source : frontendRequire(name),
    fetch: async (url, options = {}) => { requests.push({ url, method: options.method || "GET" }); return { ok: false }; },
  }).useAgentRuntime();
  return { hook, state, refs, readers, requests,
    notifications: () => state.flatMap(cell => Array.isArray(cell.value)
      ? cell.value.filter(row => row && row.event && row.sessionId && row.runId) : []),
    close: async () => { readers.forEach(reader => reader.resolve()); await turn(); } };
}

add("hook_preserves_source_session_tag_and_ignores_aborted_old_stream", async () => {
  const h = observeHarness();
  try {
    const run = { id: "synthetic-foreign-run", session_id: "source-session", status: "running" };
    h.hook.observe(run);
    h.hook.currentSession.current = "selected-session";
    h.readers[0].receive({ seq: 1, kind: "delivery", data: { task: { id: "delivery-1", status: "waiting_for_reply" } } });
    assert.equal(h.notifications().length, 1);
    assert.equal(h.notifications()[0].sessionId, "source-session");
    assert.equal(h.notifications()[0].runId, run.id);
    assert.equal(h.notifications()[0].event.seq, 1);
    h.hook.observe(run, () => {});
    assert.equal(h.readers[0].signal.aborted, true);
    h.readers[0].receive({ seq: 2, kind: "delivery", data: { task: { status: "completed" } } });
    assert.equal(h.notifications().length, 1, "aborted connection must not publish a late notification");
    h.readers[1].receive({ seq: 3, kind: "delivery", data: { task: { status: "running" } } });
    assert.equal(h.notifications().length, 2);
    assert.equal(h.notifications()[1].sessionId, "source-session");
    assert(h.requests.every(request => request.method === "GET"), "notification must not send or cancel work");
  } finally { await h.close(); }
});

add("hook_foreground_response_keeps_callback_and_cancel_still_settles_busy", async () => {
  const h = observeHarness(), received = [];
  try {
    const work = h.hook.execute("chat", { message: "synthetic request" }, event => received.push(event));
    // Attach a rejection handler before injecting a terminal stream frame.
    const outcome = work.then(value => ({ value }), error => ({ error }));
    await turn();
    const reader = h.readers.find(item => item.id === "synthetic-foreground");
    const busyCell = h.state.find(cell => cell.value === "synthetic-foreground");
    assert(reader && busyCell);
    const reply = { seq: 4, kind: "response", data: { result: { message: "one foreground reply" } } };
    reader.receive(reply);
    const response = await outcome;
    assert.equal(response.value.message, "one foreground reply");
    assert.strictEqual(received[0], reply);
    assert.equal(h.notifications()[0].event.data.result, undefined);
    assert.equal(h.notifications()[0].event.data.inventory_changed, true);
    assert.equal(busyCell.value, null);
    const second = h.hook.execute("chat", { message: "another synthetic request" });
    const stopped = second.then(() => null, error => error);
    await turn();
    h.readers.at(-1).receive({ seq: 5, kind: "status", data: { status: "cancelled", detail: "Synthetic stop confirmed" } });
    assert.match(String(await stopped), /Synthetic stop confirmed/);
    assert.equal(busyCell.value, null);
    assert(h.requests.every(request => request.method === "GET"));
  } finally { await h.close(); }
});

async function main() {
  const output = path.join(__dirname, "results", "frontend-snapshot", new Date().toISOString().replace(/[-:.]/g, ""));
  fs.mkdirSync(output, { recursive: true });
  const results = [];
  for (const test of cases) {
    disposables = [];
    const started = performance.now();
    let timer;
    try {
      await Promise.race([Promise.resolve().then(test.run), new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("Contract case timed out")), 2500);
      })]);
      results.push({ name: test.name, status: "passed", duration_ms: Math.round(performance.now() - started) });
    } catch (error) {
      results.push({ name: test.name, status: "failed", duration_ms: Math.round(performance.now() - started), error: String(error.stack || error) });
    } finally {
      clearTimeout(timer);
      disposables.forEach(value => value.dispose());
    }
  }
  const after = hashes();
  const summary = { case_count: results.length, passed: results.filter(item => item.status === "passed").length,
    failed: results.filter(item => item.status === "failed").length, cases: results,
    source_hashes: before, source_changed_during_run: sources.filter(name => before[name] !== after[name]),
    network_requests: 0, model_calls: 0, real_recruitment_actions: 0,
    scope: "Offline TypeScript refresh and simulated React-hook/SSE contracts; not browser or real-site E2E" };
  fs.writeFileSync(path.join(output, "summary.json"), JSON.stringify(summary, null, 2) + "\n");
  for (const result of results) console.log(`${result.status.toUpperCase()}: ${result.name}`);
  console.log(JSON.stringify({ output, cases: summary.case_count, passed: summary.passed, failed: summary.failed,
    source_changed_during_run: summary.source_changed_during_run }));
  if (summary.failed || summary.source_changed_during_run.length) process.exitCode = 1;
}
main().catch(error => { console.error(error); process.exitCode = 1; });
