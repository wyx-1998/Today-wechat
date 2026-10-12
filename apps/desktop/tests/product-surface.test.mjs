import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";

const source = new URL("../src/", import.meta.url);
const read = name => readFileSync(new URL(name, source), "utf8");

test("product-authored screens contain no development-only copy", () => {
  for (const name of readdirSync(source).filter(name => name.endsWith(".tsx"))) {
    assert.doesNotMatch(read(name), /测试|演示|一次性|合成|验收|调试/, name);
  }
  const storage = readFileSync(new URL("../../../backend/storage.py", import.meta.url), "utf8");
  assert.doesNotMatch(storage, /测试|演示|一次性|合成|验收|调试/);
});

test("meeting import is a Today drawer, not a primary page or silent cloud action", () => {
  const main = read("main.tsx"), panel = read("meeting-panel.tsx");
  const navigation = main.match(/<nav aria-label="主导航">([\s\S]*?)<\/nav>/)[1];
  assert.doesNotMatch(navigation, /meeting|纪要/);
  assert.ok(main.includes('page === "today" && <button'));
  assert.ok(main.includes('>导入纪要</button>'));
  assert.ok(panel.includes('dialog.current?.showModal()'));
  assert.ok(panel.includes('同意发送这份纪要'));
  assert.ok(panel.includes('!preview || !consent || !readyModel'));
  assert.ok(panel.includes('preview_token: p.preview_token, authorize: true'));
  assert.ok(panel.includes('candidate_ids: selected, confirm: true'));
  assert.ok(panel.includes('!selected.length'));
  assert.ok(panel.includes('setPreview(null); setConsent(false)'));
  const native = readFileSync(new URL("../src-tauri/src/main.rs", import.meta.url), "utf8");
  for (const command of ["meeting.save", "meeting.list", "meeting.detail", "meeting.preview", "meeting.generate", "meeting.publish"]) assert.ok(native.includes(`"${command}"`));
});

test("desktop bridge does not expose fixture or connection-check actions", () => {
  const native = readFileSync(new URL("../src-tauri/src/main.rs", import.meta.url), "utf8");
  const allowed = native.match(/const ALLOWED:.*?= &\[([\s\S]*?)\];/)[1];
  for (const command of ["source.demo", "demo.append", "model.test", "model.migrate", "analysis.preview", "analysis.demo"]) {
    assert.ok(!allowed.includes(`"${command}"`), command);
  }
  assert.ok(allowed.includes('"analysis.real.preview"'));
  assert.ok(allowed.includes('"analysis.real"'));
});

test("removing development copy does not remove cloud consent or scope preview", () => {
  const panel = read("manual-panel.tsx");
  assert.ok(panel.includes("同意发送本次预览中的文字"));
  assert.ok(panel.includes("!consent"));
  assert.ok(panel.includes("preview_token: preview.preview_token"));
  assert.ok(panel.includes("preview.messages.map"));
  assert.ok(panel.includes("setConsent(false)"));
  assert.doesNotMatch(panel, /"model\.test"|"analysis\.demo"|"analysis\.preview"/);
});

test("continuous authorization and retry require separate explicit confirmation", () => {
  const panel = read("automatic-panel.tsx");
  assert.ok(panel.includes("!consent"));
  assert.ok(panel.includes("preview_token: preview.preview_token"));
  assert.ok(panel.includes("preview.chats.map"));
  assert.ok(panel.includes("preview.messages.map"));
  assert.ok(panel.includes("preview.base_url"));
  assert.ok(panel.includes("后续新增文字"));
  assert.ok(panel.includes("!retryConsent"));
  assert.ok(panel.includes("重复计费"));
  assert.ok(panel.includes("authorization.revoke"));
  assert.ok(panel.includes("setConsent(false)"));
});

test("result polling does not reload the list when calls or revisions change", () => {
  const results = read("results.tsx");
  assert.doesNotMatch(results, /model_usage|\[account, page, state/);
  assert.ok(results.includes("applyTaskUpdate(previous, next, task.task_id)"));
  assert.ok(results.includes("有新内容 · 更新结果"));
  assert.ok(results.includes("hasNewResults(data?.results_revision, state?.results_revision)"));
});

test("chat analysis selection is independent from local synchronization and labels", () => {
  const main = read("main.tsx");
  assert.ok(main.includes("checked={draft.analysis_enabled}"));
  assert.ok(main.includes("纳入文字自动分析"));
  assert.ok(main.includes("disabled={!draft.sync_enabled}"));
});

test("full-window review uses the frozen preview mode and resets consent on mode changes", () => {
  const panel = read("manual-panel.tsx");
  assert.ok(panel.includes('useState(false)'));
  assert.ok(panel.includes("重新核对已处理内容"));
  assert.ok(panel.includes('review ? "analysis.review.preview" : "analysis.real.preview"'));
  assert.ok(panel.includes('preview.mode === "reanalysis" ? "analysis.review" : "analysis.real"'));
  assert.ok(panel.includes("chatId, review, resetToken"));
  assert.ok(panel.includes("preview.existing_tasks?.map"));
  assert.ok(panel.includes("preview.message_count === 0"));
});

test("task changes require explicit versioned acceptance or keeping the existing task", () => {
  const results = read("results.tsx");
  assert.ok(results.includes('"task.change.resolve"'));
  assert.ok(results.includes("task_revision: task.revision, accept"));
  assert.ok(results.includes("保留当前事项"));
  assert.ok(results.includes("确认已完成"));
  assert.ok(results.includes("确认已取消"));
  assert.ok(results.includes("applyTaskUpdate(previous, next, task.task_id)"));
  const native = readFileSync(new URL("../src-tauri/src/main.rs", import.meta.url), "utf8");
  for (const command of ["analysis.review.preview", "analysis.review", "task.change.resolve"]) {
    assert.ok(native.includes(`"${command}"`));
  }
});

test("unresolved ownership uses a versioned local self-claim from shared task actions", () => {
  const results = read("results.tsx");
  assert.ok(results.includes('canClaimSelf(task) && <button className="task-claim" disabled={busy}'));
  assert.ok(results.includes('onClick={() => void claimSelf(task)}>是我</button>'));
  assert.ok(results.includes('mutateTask(task, "task.claim_self")'));
  assert.ok(results.includes('revision: task.revision, ...fields'));
  assert.ok(results.includes('setNotice("已移到“我要做”")'));
  assert.ok(results.includes('taskActions(detailTask)'));
  const native = readFileSync(new URL("../src-tauri/src/main.rs", import.meta.url), "utf8");
  assert.ok(native.includes('"task.claim_self"'));
});

test("self-claim has a distinct blue treatment without changing other actions", () => {
  const css = read("style.css");
  assert.match(css, /\.task-actions \.task-claim \{[^}]*color: #175dbb;[^}]*background: #e8f0fc;/);
  assert.ok(css.includes('.task-actions .task-claim:hover:not(:disabled)'));
  assert.ok(css.includes('.task-actions .task-claim:active:not(:disabled)'));
  assert.ok(css.includes('button:focus-visible'));
  const results = read("results.tsx");
  assert.equal((results.match(/className="task-claim"/g) || []).length, 1);
  assert.ok(results.includes('<button disabled={busy} onClick={() => void update(task, "completed")}>'));
});

test("cancelled tasks are not active and remain available in Tasks", () => {
  const results = read("results.tsx");
  assert.ok(results.includes('["completed", "ignored", "cancelled"].includes(task.status)'));
  assert.ok(results.includes('["waiting", "ignored", "cancelled"] as const'));
  assert.ok(read("task-view.ts").includes('cancelled: "已取消"'));
});

test("Settings is a compact footer action outside primary navigation and keeps dirty confirmation", () => {
  const main = read("main.tsx");
  const navigation = main.match(/<nav aria-label="主导航">([\s\S]*?)<\/nav>/)[1];
  assert.doesNotMatch(navigation, /\["settings",/);
  const footer = main.slice(main.indexOf('className="sidebar-footer"'), main.indexOf('</aside>'));
  assert.ok(footer.includes('aria-label="设置"'));
  assert.ok(footer.includes('leave(() => setPage("settings"))'));
  assert.ok(main.includes('hidden={page !== "settings"}'));
});
test("results use a single original entry per task or change, never a row of numbered links", () => {
  const results = read("results.tsx");
  assert.doesNotMatch(results, /task\.source_message_ids\.map|change\.source_message_ids\.map/);
  assert.ok(results.includes("openSource(task.source_message_ids, task.title"));
  assert.ok(results.includes("openSource(change.source_message_ids"));
  assert.ok(results.includes("只读 · 按来源时间排列"));
  assert.ok(results.includes("部分来源暂时不可用，其它原文仍可查看。"));
  assert.ok(results.includes('event.key === "Escape"'));
  assert.ok(results.includes("trigger.current?.focus()"));
});
test("compact status keeps partial publication and errors discoverable without a hero introduction", () => {
  const results = read("results.tsx");
  assert.doesNotMatch(results, /page-intro|值得关注的待处理事项|从明确请求和承诺中发现待办/);
  assert.ok(results.includes("results-toolbar"));
  assert.ok(results.includes("analysis-status-detail"));
  assert.ok(results.includes("部分结果待核对"));
  assert.ok(results.includes("未发布 · 原结果保留"));
  assert.ok(results.includes("data.last_analysis.error_code"));
  assert.ok(results.includes("有新内容 · 更新结果"));
});

test("model deletion is separately confirmed with current revision and does not weaken storage", () => {
  const panel = read("model-panel.tsx");
  assert.ok(panel.includes("删除文本配置？"));
  assert.ok(panel.includes("deleteDialog.current?.showModal()"));
  assert.ok(panel.includes("聊天、待办和历史用量保留"));
  assert.ok(panel.includes('"model.delete", {revision, confirm: true}'));
  assert.ok(panel.includes('onCancel={() => setDeleteRevision(null)}'));
  assert.ok(panel.includes("本机明文保存，页面不回显"));
});

test("usage displays independent input/output/total and explicit missing counts", () => {
  const panel = read("usage-dashboard.tsx");
  assert.ok(panel.includes('["input", "output", "total"] as const'));
  assert.ok(panel.includes('== null ? "未知"'));
  assert.ok(panel.includes('unknown_${kind}_requests'));
  assert.ok(panel.includes("有缺失时不代表完整用量"));
  assert.ok(panel.includes("不用输入与输出相加替代"));
});

test("internal storage conversion is absent from product screens and desktop commands", () => {
  const panel = read("model-panel.tsx");
  assert.doesNotMatch(panel, /迁移|待迁移|升级|旧加密配置|model\.migrate/);
  assert.ok(panel.includes("已保存 · 不回显"));
  assert.ok(panel.includes("请勿分享应用数据库或备份"));
});

test("Ask exposes independent scope preview and read-only conversation", () => {
  const main=read("main.tsx"), ask=read("ask-panel.tsx");
  assert.ok(main.includes('<AskPanel state={state}'));
  assert.ok(ask.includes('同意发送本次问题'));
  assert.ok(ask.includes('ask.preview'));
  assert.ok(ask.includes('ask.revoke'));
  assert.ok(ask.includes('停止回答'));
  assert.ok(ask.includes('Markdown skipHtml'));
  assert.doesNotMatch(ask, /task.update|task.claim_self|sync.now/);
});
test("Tasks cards use saved results, real filters and accessible same-page details", () => {
  const results = read("results.tsx");
  assert.ok(results.includes('useState<TaskView>("self")'));
  assert.ok(results.includes('selectTasks(items, {view, search, deadline, sort})'));
  assert.ok(results.includes('className="task-card-grid"'));
  assert.ok(results.includes('onClick={event => openDetail(task, event.currentTarget)}'));
  assert.ok(results.includes('drawer.current?.showModal()'));
  assert.ok(results.includes('onCancel={event => {event.preventDefault(); closeSource();}}'));
  assert.ok(results.includes("返回任务详情"));
  assert.doesNotMatch(results, /slice\(0,\s*(8|10|20)\)|demo\.append|analysis\.real"/);
  const css = read("style.css");
  assert.ok(css.includes('.task-drawer:not([open]) { display: none; }'));
});

test("wrapped detail fields override global definition-list columns", () => {
  const css = read("style.css");
  const fields = css.match(/\.task-detail-fields\s*\{([^}]+)\}/)?.[1];
  assert.ok(fields, "detail fields need their own layout");
  assert.match(fields, /grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  const row = css.match(/\.task-detail-fields\s*>\s*div\s*\{([^}]+)\}/)?.[1];
  assert.match(row, /min-width:\s*0/);
  assert.ok(read("results.tsx").includes('className="task-detail-fields"'));
});

test("legacy migration never prevents withdrawing cloud authorization", () => {
  const automatic = read("automatic-panel.tsx");
  assert.ok(automatic.includes('disabled={blocked} onClick={() => void action("authorization.revoke"'));
  assert.ok(automatic.includes('disabled={sendBlocked || !consent}'));
  assert.ok(automatic.includes('disabled={blocked || !retryConsent'));
  const panel = read("model-panel.tsx");
  assert.ok(panel.includes('disabled={busy || !!draft}/>'));
});
