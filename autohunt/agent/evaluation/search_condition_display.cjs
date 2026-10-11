// Pure rendering contracts. Does not start Next.js or access application data.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const frontend = path.resolve(__dirname, "../../frontend");
const ts = require(path.join(frontend, "node_modules/typescript"));
const source = path.join(frontend, "lib/search-condition-display.ts");
const compiled = ts.transpileModule(fs.readFileSync(source, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } });
const loaded = new Module(source);
loaded._compile(compiled.outputText, source);
const { filterText, monthlySalaryText } = loaded.exports;
const monthly = { op: "condition", text: "月薪15k以上", salary: { unit: "monthly", minimum: 15000 } };
const negotiable = { op: "condition", text: "薪资面议", salary: { unit: "negotiable" } };
const daily = { op: "condition", text: "实习日薪至少300元", salary: { unit: "daily", minimum: 300 } };
const tree = { op: "any", children: [
  { op: "all", children: [{ op: "condition", text: "正式岗位" }, { op: "any", children: [monthly, negotiable] }] },
  { op: "all", children: [{ op: "condition", text: "实习岗位" }, daily] }
] };
const records = [];
function test(name, callback) {
  try { callback(); records.push({ name, status: "pass" }); }
  catch (error) { records.push({ name, status: "fail", detail: String(error) }); }
}
test("monthly_tree_is_not_unlimited_when_flat_field_is_empty", () => assert.match(monthlySalaryText(tree, null), /15,000 元/));
test("overview_marks_exception_scope", () => assert.match(monthlySalaryText(tree, null), /适用范围及例外/));
test("alternatives_render_or_not_and", () => assert.equal(filterText(tree), "（（正式岗位 且 （月薪15k以上 或 薪资面议）） 或 （实习岗位 且 实习日薪至少300元））"));
test("allowance_is_labeled_nonmandatory", () => assert.match(filterText({ op: "condition", text: "实习也可", mode: "allow" }), /非必选/));
test("preference_is_labeled_nonmandatory", () => assert.match(filterText({ op: "condition", text: "大公司", mode: "prefer" }), /非必选/));
test("daily_salary_is_never_shown_as_monthly", () => assert.doesNotMatch(monthlySalaryText(daily, null), /300 元/));
test("daily_only_tree_keeps_legacy_monthly_bound", () => assert.match(monthlySalaryText(daily, 15000), /15,000 元/));
test("negotiable_tree_does_not_show_stale_flat_bound", () => assert.doesNotMatch(monthlySalaryText(negotiable, 25000), /25,000/));
test("empty_tree_uses_explicit_flat_bound", () => assert.equal(monthlySalaryText(null, 15000), "15,000 元"));
test("negated_monthly_predicate_is_not_a_lower_bound", () => assert.doesNotMatch(monthlySalaryText({ op: "not", children: [monthly] }, null), /15,000/));
const directory = path.join(__dirname, "../test-results/search-condition-display", new Date().toISOString().replace(/[:.]/g, "-"));
fs.mkdirSync(directory, { recursive: true });
fs.writeFileSync(path.join(directory, "results.json"), JSON.stringify(records, null, 2));
const failures = records.filter(record => record.status === "fail").length;
process.stdout.write(JSON.stringify({ tests: records.length, failures, output: directory }) + "\n");
process.exitCode = failures ? 1 : 0;
