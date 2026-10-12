import test from "node:test";
import assert from "node:assert/strict";
import { sourceIds, chronologicalSources, loadSources } from "../src/source-view.ts";

const message = (id, sent_at = "2026-10-03T08:00:00+08:00", account_id = "generated-account") => ({message_id: id, account_id, sent_at, content: `Original ${id}`});
const current = () => true;

test("one source entry per distinct original, preserving source identifiers", () => {
  const ids = ["first", "second", "first"];
  assert.deepEqual(sourceIds(ids), ["first", "second"]);
  assert.deepEqual(ids, ["first", "second", "first"]);
});
test("all eighteen originals load without truncation or cloud requests", async () => {
  const ids = Array.from({length: 18}, (_, index) => `id-${index}`);
  const fetched = [];
  const results = await loadSources(ids, "generated-account", async id => { fetched.push(id); return message(id); }, current);
  assert.equal(results.length, 18);
  assert.deepEqual(fetched, ids);
  assert.ok(results.every(item => !item.unavailable && item.message.content === `Original ${item.id}`));
});
test("sort originals by message time, not returned or reference order", async () => {
  const results = await loadSources(["later", "earlier"], "generated-account", async id => message(id, id === "earlier" ? "2026-10-02T09:00:00+08:00" : "2026-10-03T09:00:00+08:00"), current);
  assert.deepEqual(results.map(row => row.id), ["earlier", "later"]);
});
test("equal timestamps remain stable and sorting never changes the input", () => {
  const entries = ["b", "a"].map(id => ({id, message: message(id), unavailable: false}));
  const results = chronologicalSources(entries);
  assert.deepEqual(results.map(row => row.id), ["b", "a"]);
  assert.notEqual(results, entries);
});
test("a missing original retains its reference without hiding readable sources", async () => {
  const results = await loadSources(["missing", "readable"], "generated-account", async id => {
    if (id === "missing") throw new Error("private-response-secret");
    return message(id);
  }, current);
  assert.deepEqual(results.map(row => row.id), ["readable", "missing"]);
  assert.deepEqual(results[1], {id: "missing", message: null, unavailable: true});
  assert.doesNotMatch(JSON.stringify(results), /private-response-secret/);
});
test("wrong account never becomes a displayed original", async () => {
  const results = await loadSources(["original"], "generated-account", async id => message(id, undefined, "other-account"), current);
  assert.deepEqual(results, [{id: "original", message: null, unavailable: true}]);
});
test("wrong message id never substitutes another source", async () => {
  const results = await loadSources(["requested"], "generated-account", async () => message("unrelated"), current);
  assert.deepEqual(results, [{id: "requested", message: null, unavailable: true}]);
});
test("closing or switching account during loading discards stale originals", async () => {
  let active = true, calls = 0;
  const results = await loadSources(Array.from({length: 18}, (_, i) => String(i)), "generated-account", async id => {
    calls++; active = false; return message(id);
  }, () => active);
  assert.deepEqual(results, []);
  assert.equal(calls, 4);
});
test("loading at most four local detail requests concurrently", async () => {
  let inflight = 0, maximum = 0;
  const results = await loadSources(Array.from({length: 19}, (_, i) => String(i)), "generated-account", async id => {
    inflight++; maximum = Math.max(maximum, inflight);
    await new Promise(resolve => setTimeout(resolve, 1));
    inflight--; return message(id);
  }, current);
  assert.equal(maximum, 4);
  assert.equal(results.length, 19);
});
test("empty or already closed source view performs no requests", async () => {
  const fetch = () => { throw new Error("must not fetch"); };
  assert.deepEqual(await loadSources([], "generated-account", fetch, current), []);
  assert.deepEqual(await loadSources(["one"], "generated-account", fetch, () => false), []);
});
test("unknown dates and unavailable sources do not drop or rewrite originals", () => {
  const results = chronologicalSources([
    {id: "unknown", message: message("unknown", "not-a-date"), unavailable: false},
    {id: "missing", message: null, unavailable: true},
    {id: "dated", message: message("dated"), unavailable: false},
  ]);
  assert.deepEqual(results.map(row => row.id), ["dated", "unknown", "missing"]);
  assert.equal(results[1].message.sent_at, "not-a-date");
});
test("original text, line breaks and effective identity are preserved", async () => {
  const original = {...message("one"), content: "完整原文\n".repeat(1000), is_self: null, effective_is_self: true};
  const [result] = await loadSources(["one", "one"], "generated-account", async () => original, current);
  assert.equal(result.message, original);
  assert.equal(result.message.content, original.content);
  assert.equal(result.message.effective_is_self, true);
});
