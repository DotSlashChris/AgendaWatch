import { test } from "node:test";
import assert from "node:assert/strict";
import { cronDecision, denverParts } from "../src/schedule.ts";

const t = (iso: string) => Date.parse(iso);

test("MDT: 14:21 UTC is 8:21 Denver -> run", () => {
  assert.deepEqual(cronDecision(t("2026-10-13T14:21:00Z")).action, "run");
  assert.equal(cronDecision(t("2026-10-13T14:21:00Z")).date, "2026-10-13");
});
test("MDT: 15:21 UTC is 9:21 Denver -> check (no second run)", () => {
  assert.equal(cronDecision(t("2026-10-13T15:21:00Z")).action, "check");
});
test("MST: 15:21 UTC is 8:21 Denver -> run; 14:21 UTC is 7:21 -> skip", () => {
  assert.equal(cronDecision(t("2026-12-13T15:21:00Z")).action, "run");
  assert.equal(cronDecision(t("2026-12-13T14:21:00Z")).action, "skip");
});
test("DST change days", () => {
  assert.equal(cronDecision(t("2026-11-01T15:21:00Z")).action, "run");   // first MST day (DST ended 2am)
  assert.equal(cronDecision(t("2026-03-08T14:21:00Z")).action, "run");   // first MDT day
  assert.equal(cronDecision(t("2027-03-14T14:21:00Z")).action, "run");
});
test("hour is numeric (regression: '08' !== '8')", () => {
  const p = denverParts(t("2026-10-13T14:21:00Z"));
  assert.equal(p.hour, 8); assert.equal(p.minute, 21); assert.equal(p.offset, "MDT");
});
test("Denver date, not UTC date, near midnight", () => {
  assert.equal(denverParts(t("2026-10-14T03:00:00Z")).date, "2026-10-13");
});
