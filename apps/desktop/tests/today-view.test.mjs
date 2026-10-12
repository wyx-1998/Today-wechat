import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { todayActions, attentionGroups, actionReason, todayTabs, todayTabForKey } from "../src/today-view.ts";
import { applyTaskUpdate } from "../src/results-state.ts";

const task = (id, fields = {}) => ({task_id:id, title:id, owner_type:"self", due_at:null, created_at:new Date(2026,9,3,10).toISOString(), status:"pending", revision:1, source_message_ids:[], ...fields});
test("Today includes action or confirmation, not completed history or passive waiting", () => {
  const items = [task("self"), task("unknown",{owner_type:"unknown"}),task("shared",{owner_type:"shared"}),
    task("wait",{owner_type:"other"}),task("change",{owner_type:"other",changes:[{}]}),
    task("done",{status:"completed"}),task("ignored",{status:"ignored"}),task("cancelled",{status:"cancelled"})];
  const before = structuredClone(items);
  assert.deepEqual(todayActions(items,new Date(2026,9,3,12)).map(x => x.task_id), ["self","unknown","shared","change"]);
  assert.deepEqual(items,before);
});
test("Today does not repeat the entire task ledger: old undated and far-future work stays in Tasks", () => {
  const fields = {created_at:new Date(2026,8,1,10).toISOString()};
  const items = [task("old",fields),task("later",{...fields,due_at:"2026-10-30"}),
    task("overdue",{...fields,due_at:"2026-10-02"}),task("near",{...fields,due_at:"2026-10-10"}),task("new")];
  assert.deepEqual(todayActions(items,new Date(2026,9,3,12)).map(x=>x.task_id),["overdue","near","new"]);
});
test("categories preserve stored order and do not cap high-volume knowledge", () => {
  const items = Array.from({length:2000},(_,i) => ({attention_id:String(i),kind:i%2?"key_point":"information"}));
  const groups = attentionGroups(items);
  assert.equal(groups[0].items.length,1000); assert.equal(groups[1].items.length,1000);
  assert.deepEqual(groups[0].items[1],items[2]); assert.deepEqual(attentionGroups([]).map(x=>x.items),[[],[]]);
});
test("attention labels use current deadline, with confirmation before deadline guesses", () => {
  const now = new Date(2026,9,3,12);
  assert.equal(actionReason(task("old",{due_at:"2026-10-02"}),now),"已逾期");
  assert.equal(actionReason(task("today",{due_at:"2026-10-03"}),now),"今天到期");
  assert.equal(actionReason(task("unknown",{owner_type:"unknown",due_at:"2026-10-02"}),now),"归属待确认");
  assert.equal(actionReason(task("change",{changes:[{}]}),now),"变化待确认");
  assert.equal(actionReason(task("future",{due_at:"2026-10-30"}),now),"待处理");
});
test("a task update preserves visible knowledge snapshot instead of pulling new results", () => {
  const knowledge = [{attention_id:"old",kind:"information"}];
  const before = {items:[task("t")],attention_items:knowledge,results_revision:1,last_analysis:null,synthetic:false};
  const response = {...before,items:[task("t",{status:"completed"}),task("new")],attention_items:[{attention_id:"new"}],results_revision:2};
  const result = applyTaskUpdate(before,response,"t");
  assert.equal(result.attention_items,knowledge); assert.equal(result.results_revision,1);
  assert.equal(result.items.length,1); assert.equal(result.items[0].status,"completed");
});
test("Today uses saved independent knowledge and same-page cards without analysis requests", () => {
  const results = readFileSync(new URL("../src/results.tsx",import.meta.url),"utf8");
  assert.ok(results.includes('page === "today" ? "today.list" : "tasks.list"'));
  assert.ok(results.includes('aria-labelledby="today-tab-actions"'));
  const knowledge = results.slice(results.indexOf('{groups.filter(group =>'),results.indexOf('<dialog ref={drawer}'));
  assert.doesNotMatch(knowledge,/taskActions|update\(|analysis\.|slice\(/);
  assert.ok(knowledge.includes('openAttention(item, event.currentTarget)'));
  assert.ok(results.includes("返回知晓内容详情"));
  assert.ok(results.includes("消息时间不等于事件发生时间"));
  assert.ok(results.includes("rejected_attention_count"));
});

test("Today tab labels and categories stay separate without changing knowledge data", () => {
  assert.deepEqual(todayTabs.map(tab => tab.id), ["actions", "information", "key_point"]);
  assert.deepEqual(todayTabs.map(tab => tab.title), ["需要行动", "资讯日报", "关键要点"]);
  assert.deepEqual(attentionGroups([]).map(group => group.title), ["资讯日报", "关键要点"]);
});
test("Today arrow navigation wraps, Home/End select boundaries, other keys stay native", () => {
  assert.equal(todayTabForKey("actions", "ArrowRight"), "information");
  assert.equal(todayTabForKey("information", "ArrowLeft"), "actions");
  assert.equal(todayTabForKey("key_point", "ArrowRight"), "actions");
  assert.equal(todayTabForKey("actions", "ArrowLeft"), "key_point");
  assert.equal(todayTabForKey("information", "Home"), "actions");
  assert.equal(todayTabForKey("actions", "End"), "key_point");
  for (const key of ["Tab", "Enter", " ", "Escape", "ArrowUp"]) assert.equal(todayTabForKey("actions", key), null);
});
test("Today panels render only the selected category, expose counts and retain tab on refresh", () => {
  const results = readFileSync(new URL("../src/results.tsx",import.meta.url),"utf8");
  const css = readFileSync(new URL("../src/style.css",import.meta.url),"utf8");
  assert.ok(results.includes('useState<TodayTab>("actions")'));
  assert.ok(results.includes('role="tablist" aria-label="今天内容分类"'));
  assert.ok(results.includes('aria-selected={todayTab === tab.id}'));
  assert.ok(results.includes('tabIndex={todayTab === tab.id ? 0 : -1}'));
  assert.ok(results.includes('aria-controls={`today-panel-${tab.id}`}'));
  assert.ok(results.includes('hidden={todayTab !== "actions"}'));
  assert.ok(results.includes('todayTab === "actions" &&'));
  assert.ok(results.includes('hidden={todayTab !== group.kind}'));
  assert.ok(results.includes('todayTab === group.kind &&'));
  assert.ok(results.includes('todayCounts[tab.id]'));
  assert.doesNotMatch(results.slice(results.indexOf('async function reload()'),results.indexOf('const active =')), /setTodayTab|sync\.now|analysis\./);
  assert.ok(css.includes('.today-panel[hidden] { display: none; }'));
});
