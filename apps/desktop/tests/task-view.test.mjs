import test from "node:test";
import assert from "node:assert/strict";
import { belongsToView, canClaimSelf, dueDay, localDay, selectTasks, taskStatusLabel } from "../src/task-view.ts";

const task = (id, fields = {}) => ({task_id: id, title: `事项 ${id}`, owner_type: "self", status: "pending", due_at: null, source_message_ids: [], revision: 1, ...fields});
const now = new Date(2026, 9, 3, 12);
const options = {view: "self", search: "", deadline: "all", sort: "original"};
const ids = items => items.map(item => item.task_id);

test("only unresolved active ownership offers the self-claim action", () => {
  for (const owner_type of ["unknown", "shared"]) {
    for (const status of ["needs_confirmation", "pending", "todo", "in_progress"]) {
      assert.equal(canClaimSelf(task("claim", {owner_type, status})), true);
    }
    for (const status of ["completed", "ignored", "cancelled", "deleted", "unexpected"]) {
      assert.equal(canClaimSelf(task("closed", {owner_type, status})), false);
    }
  }
  for (const owner_type of ["self", "other", "unexpected"]) {
    assert.equal(canClaimSelf(task("owned", {owner_type})), false);
  }
});

test("confirmed self task moves views without altering filters or other tasks", () => {
  const old = task("claim", {owner_type: "unknown", status: "needs_confirmation"});
  const confirmed = {...old, owner_type: "self", status: "pending", revision: 2};
  const unrelated = task("unrelated", {owner_type: "shared"});
  assert.deepEqual(ids(selectTasks([confirmed, unrelated], {...options, view: "ownership"}, now)), ["unrelated"]);
  assert.deepEqual(ids(selectTasks([confirmed, unrelated], options, now)), ["claim"]);
  assert.equal(canClaimSelf(confirmed), false);
  assert.equal(old.owner_type, "unknown");
});

test("default Tasks contains only unfinished self tasks without changing ownership", () => {
  const items = [task("self"), task("unknown", {owner_type:"unknown"}), task("shared", {owner_type:"shared"}), task("other", {owner_type:"other"}), task("done", {status:"completed"}), task("ignored", {status:"ignored"}), task("cancelled", {status:"cancelled"})];
  const original = structuredClone(items);
  assert.deepEqual(ids(selectTasks(items, options, now)), ["self"]);
  assert.deepEqual(ids(selectTasks(items, {...options, view:"ownership"}, now)), ["unknown", "shared"]);
  for (const [view,id] of [["waiting","other"],["completed","done"],["ignored","ignored"],["cancelled","cancelled"]]) assert.deepEqual(ids(selectTasks(items, {...options,view},now)),[id]);
  assert.deepEqual(items,original);
});
test("detail status keeps confirmation distinct and does not guess unknown states", () => {
  assert.equal(taskStatusLabel("needs_confirmation"), "待确认");
  assert.equal(taskStatusLabel("pending"), "待处理");
  assert.equal(taskStatusLabel("completed"), "已完成");
  assert.equal(taskStatusLabel("unexpected"), "状态未确认");
});
test("completed history includes all owners but not ignored or cancelled", () => {
  assert.equal(belongsToView(task("other", {owner_type:"other", status:"completed"}), "completed"), true);
  assert.equal(belongsToView(task("shared", {owner_type:"shared", status:"completed"}), "ownership"), false);
  assert.equal(belongsToView(task("x", {status:"ignored"}), "completed"), false);
});
test("search trims, matches Chinese and ignores Latin case without resetting view", () => {
  const items = [task("a", {title:"完善 PPT 并发送方案"}), task("b", {title:"PPT", owner_type:"unknown"})];
  assert.deepEqual(ids(selectTasks(items,{...options,search:" ppt "},now)),["a"]);
  assert.deepEqual(ids(selectTasks(items,{...options,search:"方案"},now)),["a"]);
  assert.deepEqual(selectTasks(items,{...options,search:"不存在"},now),[]);
});
test("calendar deadlines distinguish yesterday, today, seven days and unknown", () => {
  const items = [task("old",{due_at:"2026-10-02"}),task("today",{due_at:"2026-10-03"}),task("end",{due_at:"2026-10-10"}),task("later",{due_at:"2026-10-11"}),task("none"),task("invalid",{due_at:"not-a-date"})];
  for (const [deadline, expected] of [["overdue",["old"]],["today",["today"]],["week",["today","end"]],["undated",["none"]]]) assert.deepEqual(ids(selectTasks(items,{...options,deadline},now)),expected);
});
test("date-only deadline does not shift by timezone and invalid calendar days are not guessed", () => {
  assert.equal(dueDay("2026-10-03"),"2026-10-03");
  assert.equal(dueDay("2026-02-30"),null);
  assert.equal(dueDay("not-a-date"),null);
  assert.equal(dueDay(null),null);
  assert.equal(dueDay("2026-10-03T16:30:00Z"),localDay(new Date("2026-10-03T16:30:00Z")));
});
test("sorting is stable and nonmutating; unknown dates come last", () => {
  const items = [task("none"),task("later",{due_at:"2026-10-05"}),task("first",{due_at:"2026-10-03"}),task("second",{due_at:"2026-10-03"})];
  assert.deepEqual(ids(selectTasks(items,{...options,sort:"deadline"},now)),["first","second","later","none"]);
  assert.deepEqual(ids(selectTasks(items,options,now)),["none","later","first","second"]);
  assert.deepEqual(ids(items),["none","later","first","second"]);
});
test("empty and high-volume lists are not silently capped", () => {
  assert.deepEqual(selectTasks([],options,now),[]);
  const items = Array.from({length:2000},(_,i) => task(String(i),{title:"超长任务".repeat(40)+i}));
  assert.equal(selectTasks(items,options,now).length,2000);
});
