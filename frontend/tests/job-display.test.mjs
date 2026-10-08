import assert from "node:assert/strict";
import test from "node:test";
import { shortJobDate } from "../lib/job-display.ts";

test("board date is compact and uses Shanghai timezone without modifying the original", () => {
  const original = "2026-10-08T03:39:51.231772+00:00";
  assert.equal(shortJobDate(original), "2026/10/08");
  assert.equal(shortJobDate("2026-10-07T22:00:00Z"), "2026/10/08");
  assert.equal(original, "2026-10-08T03:39:51.231772+00:00");
});
test("unknown or invalid posted dates do not crash the board", () => {
  assert.equal(shortJobDate(""), "日期未注明");
  assert.equal(shortJobDate("刚刚"), "刚刚");
});
