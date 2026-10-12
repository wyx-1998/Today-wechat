import test from "node:test";
import assert from "node:assert/strict";
import { applyTaskUpdate, hasNewResults } from "../src/results-state.ts";

const task = (id, status = "pending") => ({task_id: id, status, title: "Generated task", owner_type: "unknown", due_at: null, source_message_ids: ["generated"], revision: 1});
const page = (items, revision) => ({items, results_revision: revision, synthetic: false, last_analysis: null});

test("patch user state without importing newly published rows or snapshot metadata", () => {
  const old = page([task("existing")], 1);
  const updated = {...task("existing", "completed"), revision: 2};
  const current = page([updated, task("new")], 2);
  const result = applyTaskUpdate(old, current, "existing");
  assert.deepEqual(result.items, [updated]);
  assert.equal(result.results_revision, 1);
  assert.equal(old.items[0].status, "pending");
  assert.equal(current.items.length, 2);
});
test("unrelated rows and ordering remain unchanged", () => {
  const first = task("first"), second = task("second");
  const old = page([first, second], 2);
  const result = applyTaskUpdate(old, page([task("second", "ignored"), first], 2), "second");
  assert.equal(result.items[0], first);
  assert.equal(result.items[1].status, "ignored");
});
test("missing target or unloaded view does not import a new list", () => {
  const old = page([task("old")], 1);
  assert.equal(applyTaskUpdate(old, page([task("new")], 3), "old"), old);
  assert.equal(applyTaskUpdate(null, page([task("new")], 3), "new"), null);
});
test("only a later published revision enables the new-content prompt", () => {
  assert.equal(hasNewResults(1, 2), true);
  for (const values of [[1,1], [2,1], [undefined,1], [1,undefined], [undefined,undefined]]) {
    assert.equal(hasNewResults(...values), false);
  }
});
