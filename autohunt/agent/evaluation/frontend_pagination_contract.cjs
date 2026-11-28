// Synthetic read-only pagination contracts. No network, model or recruitment.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");
const { createRequire } = require("node:module");
const frontend = path.resolve(__dirname, "../../frontend");
const localRequire = createRequire(path.join(frontend, "package.json"));
const ts = localRequire("typescript");
const sources = ["lib/job-board-pages.ts", "lib/use-job-board.ts", "lib/latest-snapshot.ts", "app/api/agent/jobs/route.ts"];
const hashes = () => Object.fromEntries(sources.map(name => [name, crypto.createHash("sha256").update(fs.readFileSync(path.join(frontend, name))).digest("hex")]));
const before = hashes();
function load(name, overrides = {}) {
  const exports = {};
  const code = ts.transpileModule(fs.readFileSync(path.join(frontend, name), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(code, { exports, require: localRequire, AbortController, DOMException, URLSearchParams,
    Response, setTimeout, clearTimeout, console, ...overrides }, { filename: name });
  return exports;
}
const jobs = (count, shift = 0) => Array.from({ length: count }, (_, index) => ({
  id: index + 1 + shift, role: `Synthetic ${index + 1 + shift}`, company: "Fictional",
  stage: "applying", tags: [], source_url: `http://example.invalid/job/${index + 1 + shift}`,
}));
const snapshot = "a".repeat(64), newer = "b".repeat(64);
function page(rows, offset = 0, revision = snapshot) {
  const end = Math.min(offset + 100, rows.length);
  return { items: rows.slice(offset, end), total: rows.length, offset, limit: 100,
    next_offset: end < rows.length ? end : null, snapshot: revision };
}
const response = (body, status = 200) => new Response(JSON.stringify(body), { status,
  headers: { "Content-Type": "application/json" } });
function reader(handler) {
  const requests = [];
  const { readJobPages } = load("lib/job-board-pages.ts", { fetch: async (url, options) => {
    assert.equal(options.cache, "no-store");
    assert.equal(options.method, undefined);
    const target = new URL(url, "http://127.0.0.1:3000");
    assert.equal(target.pathname, "/api/agent/jobs");
    assert.equal(target.searchParams.get("page"), "true");
    assert.equal(target.searchParams.get("limit"), "100");
    requests.push({ offset: Number(target.searchParams.get("offset")), snapshot: target.searchParams.get("snapshot") });
    return handler(target, options, requests);
  } });
  return { read: signal => readJobPages(signal || new AbortController().signal), requests };
}
const cases = [];
const add = (name, run) => cases.push({ name, run });

add("250_rows_aggregate_three_pages_with_exact_total_and_no_duplicates", async () => {
  const rows = jobs(250);
  const r = reader(target => response(page(rows, Number(target.searchParams.get("offset")))));
  const all = await r.read();
  assert.equal(all.total, 250);
  assert.equal(all.jobs.length, 250);
  assert.equal(new Set(all.jobs.map(row => row.id)).size, 250);
  assert.deepEqual(r.requests.map(item => item.offset), [0, 100, 200]);
  assert.equal(r.requests[0].snapshot, null);
  assert(r.requests.slice(1).every(item => item.snapshot === snapshot));
  assert.equal(all.jobs[249].sourceUrl, rows[249].source_url);
});

for (const count of [0, 100, 200, 1250]) add(`complete_${count}_rows_without_magic_total_cap_or_extra_empty_page`, async () => {
  const rows = jobs(count), r = reader(target => response(page(rows, Number(target.searchParams.get("offset")))));
  const all = await r.read();
  assert.equal(all.jobs.length, count);
  assert.equal(all.total, count);
  assert.equal(r.requests.length, Math.max(1, Math.ceil(count / 100)));
});

add("legacy_array_and_missing_or_misreported_metadata_are_not_silent_100_row_success", async () => {
  for (const bad of [jobs(100), { items: jobs(100) }, { ...page(jobs(250)), next_offset: null },
    { ...page(jobs(250)), items: jobs(50) }, { ...page(jobs(250)), total: -1 },
    { ...page(jobs(250)), total: Infinity }, { ...page(jobs(250)), offset: 1 },
    { ...page(jobs(250)), limit: 50 }, { ...page(jobs(250)), snapshot: "invalid" }]) {
    const r = reader(() => response(bad));
    await assert.rejects(r.read(), /Invalid/);
    assert.equal(r.requests.length, 1);
  }
});

add("second_page_identity_total_snapshot_and_progress_must_be_consistent", async () => {
  const rows = jobs(150);
  for (const change of [p => ({ ...p, total: 151 }), p => ({ ...p, snapshot: newer }),
    p => ({ ...p, offset: 0 }), p => ({ ...p, next_offset: 100 }),
    p => ({ ...p, items: p.items.map((item, i) => i ? item : { ...item, id: 1 }) })]) {
    const r = reader(target => {
      const offset = Number(target.searchParams.get("offset"));
      const payload = page(rows, offset);
      return response(offset ? change(payload) : payload);
    });
    await assert.rejects(r.read(), /Invalid|Inconsistent|Duplicate/);
    assert.equal(r.requests.length, 2);
  }
});

add("invalid_job_identity_stage_or_tags_rejects_whole_page", async () => {
  for (const change of [{ id: -1 }, { id: 1.5 }, { stage: "invented" }, { stage: "rejected" },
    { role: null }, { company: null }, { tags: "SQL" }]) {
    const rows = [{ ...jobs(1)[0], ...change }], r = reader(() => response(page(rows)));
    await assert.rejects(r.read(), /Invalid/);
  }
});

add("snapshot_conflict_restarts_entire_read_once_without_mixing_old_rows", async () => {
  const oldRows = jobs(250), newRows = jobs(150, 1000);
  let changed = false;
  const r = reader(target => {
    const offset = Number(target.searchParams.get("offset"));
    if (!changed && offset) {
      changed = true;
      return response({ detail: { code: "jobs_snapshot_changed" } }, 409);
    }
    return response(page(changed ? newRows : oldRows, offset, changed ? newer : snapshot));
  });
  const all = await r.read();
  assert.equal(all.total, 150);
  assert(all.jobs.every(row => row.id > 1000));
  assert.deepEqual(r.requests.map(item => item.offset), [0, 100, 0, 100]);
  assert.equal(r.requests[2].snapshot, null);
});

add("continuous_snapshot_changes_fail_after_one_restart_and_never_spin", async () => {
  const r = reader(target => Number(target.searchParams.get("offset"))
    ? response({ detail: { code: "jobs_snapshot_changed" } }, 409) : response(page(jobs(250))));
  await assert.rejects(r.read(), /changed/);
  assert.equal(r.requests.length, 4);
});

add("unrelated_409_transport_or_json_failure_is_not_retried", async () => {
  for (const handler of [() => response({ detail: { code: "not_a_snapshot_conflict" } }, 409),
    () => response({ detail: "unavailable" }, 503), () => { throw new Error("synthetic network failure"); },
    () => new Response("{invalid-json", { headers: { "Content-Type": "application/json" } })]) {
    const r = reader(handler);
    await assert.rejects(r.read());
    assert.equal(r.requests.length, 1);
  }
});

add("abort_after_first_page_prevents_followup_read", async () => {
  const controller = new AbortController();
  const r = reader(() => { controller.abort(); return response(page(jobs(250))); });
  await assert.rejects(r.read(controller.signal), /Aborted/);
  assert.equal(r.requests.length, 1);
});

add("late_page_failure_keeps_previous_complete_snapshot_until_full_recovery", async () => {
  let broken = true, published = jobs(1), failures = [];
  const r = reader(target => Number(target.searchParams.get("offset")) && broken
    ? response({ detail: "synthetic late-page failure" }, 503)
    : response(page(jobs(250), Number(target.searchParams.get("offset")))));
  const { latestSnapshot } = load("lib/latest-snapshot.ts");
  const lane = latestSnapshot({ read: r.read, publish: value => { published = value.jobs; },
    failed: error => failures.push(error), refreshing() {} });
  try {
    await lane.refresh();
    assert.equal(published.length, 1);
    assert.equal(failures.length, 1);
    broken = false;
    await lane.refresh();
    assert.equal(published.length, 250);
  } finally { lane.dispose(); }
});

add("compiled_board_hook_publishes_only_complete_pages_and_preserves_last_total_on_failure", async () => {
  const cells = [], effects = [];
  let rows = jobs(250), fail = false;
  const r = reader(target => Number(target.searchParams.get("offset")) && fail
    ? response({ detail: "synthetic late-page failure" }, 503)
    : response(page(rows, Number(target.searchParams.get("offset")))));
  const react = {
    useCallback: value => value,
    useRef: value => ({ current: value }),
    useEffect: effect => effects.push(effect),
    useState: initial => {
      const cell = { value: initial }; cells.push(cell);
      return [initial, update => { cell.value = typeof update === "function" ? update(cell.value) : update; }];
    },
  };
  const helpers = load("lib/latest-snapshot.ts");
  const { useJobBoard } = load("lib/use-job-board.ts", {
    require: name => name === "react" ? react : name === "./latest-snapshot" ? helpers
      : name === "./job-board-pages" ? { readJobPages: r.read } : localRequire(name),
    window: { addEventListener() {}, removeEventListener() {} },
    document: { hidden: false, addEventListener() {}, removeEventListener() {} },
  });
  const hook = useJobBoard(), cleanup = effects.map(effect => effect());
  try {
    await hook.refreshJobs();
    assert.equal(cells[0].value.length, 250);
    assert.equal(cells[1].value.total, 250);
    assert.equal(cells[1].value.failed, false);
    fail = true;
    await hook.refreshJobs({ invalidate: true });
    assert.equal(cells[0].value.length, 250);
    assert.equal(cells[1].value.total, 250);
    assert.equal(cells[1].value.failed, true);
    rows = jobs(150); fail = false;
    await hook.refreshJobs({ invalidate: true });
    assert.equal(cells[0].value.length, 150);
    assert.equal(cells[1].value.total, 150);
    assert.equal(cells[1].value.failed, false);
  } finally { cleanup.forEach(dispose => dispose?.()); }
});

add("proxy_preserves_only_supported_page_and_legacy_query_parameters", async () => {
  class NextResponse extends Response {
    static json(value, init) { return response(value, init?.status); }
  }
  const requests = [];
  const { GET } = load("app/api/agent/jobs/route.ts", {
    require: name => name === "next/server" ? { NextResponse } : localRequire(name),
    process: { env: { AUTOHUNT_BACKEND_URL: "http://synthetic.invalid", AUTOHUNT_BACKEND_TOKEN: "synthetic-only" } },
    AbortSignal,
    fetch: async (url, options) => { requests.push({ url, options }); return response({ synthetic: true }); },
  });
  await GET({ nextUrl: new URL(`http://127.0.0.1/api/agent/jobs?page=true&stage=applying&limit=100&offset=100&snapshot=${snapshot}&ignored=discard`) });
  const pageUrl = new URL(requests[0].url);
  assert.equal(pageUrl.pathname, "/api/v1/jobs/page");
  assert.equal(pageUrl.searchParams.get("stage"), "applying");
  assert.equal(pageUrl.searchParams.get("offset"), "100");
  assert.equal(pageUrl.searchParams.get("snapshot"), snapshot);
  assert.equal(pageUrl.searchParams.has("ignored"), false);
  assert.equal(pageUrl.searchParams.has("page"), false);
  await GET({ nextUrl: new URL("http://127.0.0.1/api/agent/jobs?stage=review&limit=250") });
  assert.equal(new URL(requests[1].url).pathname, "/api/v1/jobs");
  assert.equal(new URL(requests[1].url).searchParams.get("limit"), "250");
  assert(requests.every(item => item.options.method === undefined && item.options.cache === "no-store"));
});

async function main() {
  const output = path.join(__dirname, "results", "frontend-pagination", new Date().toISOString().replace(/[-:.]/g, ""));
  fs.mkdirSync(output, { recursive: true });
  const results = [];
  for (const test of cases) {
    try {
      await test.run();
      results.push({ name: test.name, passed: true });
    } catch (error) {
      results.push({ name: test.name, passed: false, error: String(error.stack || error) });
    }
  }
  const after = hashes();
  const summary = { case_count: results.length, passed: results.filter(row => row.passed).length,
    failed: results.filter(row => !row.passed).length, cases: results, source_sha256: before,
    source_changed_during_run: sources.filter(name => before[name] !== after[name]),
    model_calls: 0, network_requests: 0, real_recruitment_actions: 0 };
  fs.writeFileSync(path.join(output, "summary.json"), JSON.stringify(summary, null, 2));
  for (const row of results) console.log(`${row.passed ? "PASS" : "FAIL"}: ${row.name}${row.error ? "\n" + row.error : ""}`);
  console.log(JSON.stringify({ output, cases: summary.case_count, passed: summary.passed, failed: summary.failed,
    source_changed_during_run: summary.source_changed_during_run }));
  if (summary.failed || summary.source_changed_during_run.length) process.exitCode = 1;
}
main().catch(error => { console.error(error); process.exitCode = 1; });
