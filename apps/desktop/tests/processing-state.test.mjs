import test from "node:test";
import assert from "node:assert/strict";
import {automaticStatus, batchStatusLabel} from "../src/processing-state.ts";
import {readFileSync} from "node:fs";

const state = (overrides = {}, automatic = {}) => ({text_model: {enabled: true}, paused: false,
  automatic_text: {grant: {status: "active"}, pending_messages: 0, running_batches: 0, queued_batches: 0,
    batches: [{status: "failed", error_code: "analysis_scope_changed", current_scope: false}], ...automatic}, ...overrides});
test("old scope failure never becomes the current automatic status", () => {
  assert.equal(automaticStatus(state()).label, "等待新消息");
  assert.equal(automaticStatus(state({}, {pending_messages: 12})).label, "等待聊天稳定");
  assert.equal(batchStatusLabel("failed", "analysis_scope_changed"), "范围已变更 · 本批次已停止");
});
test("running and queued totals take precedence over wait and historical records", () => {
  assert.equal(automaticStatus(state({}, {running_batches: 1, pending_messages: 12})).label, "正在整理");
  assert.equal(automaticStatus(state({}, {queued_batches: 2, pending_messages: 12})).label, "等待处理");
});
test("authorization, model and pause states remain distinguishable", () => {
  assert.equal(automaticStatus(state({}, {grant: null})).label, "未开启");
  assert.equal(automaticStatus(state({}, {grant: {status: "invalid"}})).label, "需要重新确认");
  assert.equal(automaticStatus(state({}, {grant: {status: "revoked"}})).label, "未开启");
  assert.equal(automaticStatus(state({paused: true})).label, "同步已暂停");
  assert.equal(automaticStatus(state({text_model: {enabled: false}})).label, "模型未启用");
});
test("manual and automatic panels are peers and do not require pause or automatically authorize", () => {
  const read = name => readFileSync(new URL(`../src/${name}`, import.meta.url), "utf8");
  const parent = read("model-panel.tsx"), manual = read("manual-panel.tsx");
  assert.ok(parent.includes('className="processing-grid"'));
  assert.ok(parent.includes("<AutomaticPanel")); assert.ok(parent.includes("<ManualPanel"));
  assert.doesNotMatch(manual, /!state\.paused|sync\.pause|authorization\.save/);
  assert.ok(manual.includes("generation.current"));
  assert.ok(manual.includes("sendBlocked || !consent"));
  assert.ok(manual.includes('useState(false)'));
});
