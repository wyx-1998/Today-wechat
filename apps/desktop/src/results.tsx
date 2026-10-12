import React, { useEffect, useRef, useState } from "react";
import { request, explainError } from "./bridge";
import type { State, Task, TaskPage, Message, TaskChange, Attention } from "./types";
import { dateTime, contentState, messageRelation } from "./types";
import { applyTaskUpdate, hasNewResults } from "./results-state";
import { loadSources, sourceIds, type SourceEntry } from "./source-view";
import { belongsToView, canClaimSelf, dueDay, localDay, selectTasks, taskStatusLabel, taskViews, type TaskView, type DeadlineFilter, type TaskSort } from "./task-view";
import { todayActions, attentionGroups, actionReason, todayTabs, todayTabForKey, type TodayTab } from "./today-view";
import { NewsReader } from "./news-reader";

export function Results({ page, state, configure, refreshToken = 0 }: {
  page: "today" | "tasks"; state: State | null; configure: () => void; refreshToken?: number;
}) {
  const [data, setData] = useState<TaskPage | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState<{title: string; total: number; entries: SourceEntry[]} | null>(null);
  const [loadingSource, setLoadingSource] = useState(false);
  const [view, setView] = useState<TaskView>("self");
  const [todayTab, setTodayTab] = useState<TodayTab>("actions");
  const contentScroll = useRef<HTMLElement>(null);
  const tabButtons = useRef<Partial<Record<TodayTab, HTMLButtonElement | null>>>({});
  const [search, setSearch] = useState("");
  const [deadline, setDeadline] = useState<DeadlineFilter>("all");
  const [sort, setSort] = useState<TaskSort>("original");
  const [detailId, setDetailId] = useState<string | null>(null);
  const [attentionId, setAttentionId] = useState<string | null>(null);
  const drawer = useRef<HTMLDialogElement>(null);
  const generation = useRef(0);
  const listGeneration = useRef(0);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const closeButton = useRef<HTMLButtonElement | null>(null);
  const account = state ? state.source?.account_id ?? "" : undefined;
  const lastRefreshToken = useRef(refreshToken);
  const currentAccount = useRef(account);
  currentAccount.current = account;
  useEffect(() => {
    let active = true;
    listGeneration.current++;
    setData(null); setSource(null); setLoadingSource(false); setError(""); setNotice(""); setBusy(false); generation.current++;
    setDetailId(null); setAttentionId(null); setView("self"); setTodayTab("actions"); setSearch(""); setDeadline("all"); setSort("original");
    void request<TaskPage>(page === "today" ? "today.list" : "tasks.list").then(value => { if (active) setData(value); })
      .catch(reason => { if (active) setError(explainError(reason)); });
    return () => { active = false; generation.current++; listGeneration.current++; };
  }, [account, page]);
  const sourceOpen = source !== null || detailId !== null || attentionId !== null;
  const showingSource = source !== null;
  useEffect(() => {
    if (!sourceOpen) { drawer.current?.close(); return; }
    if (!drawer.current?.open) drawer.current?.showModal();
    closeButton.current?.focus();
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") closeSource(); };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [sourceOpen, showingSource]);
  function closeSource() { generation.current++; setSource(null); setDetailId(null); setAttentionId(null); setLoadingSource(false); drawer.current?.close(); trigger.current?.focus(); }
  function openDetail(task: Task, button: HTMLButtonElement) {
    generation.current++; trigger.current = button; setNotice(""); setSource(null); setLoadingSource(false); setAttentionId(null); setDetailId(task.task_id);
  }
  function openAttention(item: Attention, button: HTMLButtonElement) {
    generation.current++; trigger.current = button; setNotice(""); setSource(null); setLoadingSource(false); setDetailId(null); setAttentionId(item.attention_id);
  }
  async function openSource(ids: string[], title: string, button: HTMLButtonElement) {
    if (account === undefined) return;
    if (!detailId && !attentionId) trigger.current = button;
    const version = ++generation.current;
    const unique = sourceIds(ids);
    setSource({title, total: unique.length, entries: []}); setLoadingSource(true);
    const isCurrent = () => version === generation.current && currentAccount.current === account;
    const entries = await loadSources(unique, account,
      id => request<Message>("message.detail", {message_id: id}), isCurrent);
    if (isCurrent()) { setSource({title, total: unique.length, entries}); setLoadingSource(false); }
  }
  async function mutateTask(task: Task, command: "task.update" | "task.claim_self", fields: {status?: string} = {}) {
    if (busy) return;
    const targetAccount = account;
    const version = listGeneration.current;
    setBusy(true); setError(""); setNotice("");
    try {
      const next = await request<TaskPage>(command, {task_id: task.task_id, revision: task.revision, ...fields});
      if (targetAccount === currentAccount.current && version === listGeneration.current) {
        setData(previous => applyTaskUpdate(previous, next, task.task_id));
        if (command === "task.claim_self") setNotice("已移到“我要做”");
      }
    } catch (reason) { if (targetAccount === currentAccount.current && version === listGeneration.current) setError(explainError(reason)); }
    finally { if (version === listGeneration.current) setBusy(false); }
  }
  function update(task: Task, status: string) { return mutateTask(task, "task.update", {status}); }
  function claimSelf(task: Task) { return mutateTask(task, "task.claim_self"); }
  async function resolveChange(task: Task, change: TaskChange, accept: boolean) {
    if (busy) return;
    const targetAccount = account;
    const version = listGeneration.current;
    setBusy(true); setError(""); setNotice("");
    try {
      const next = await request<TaskPage>("task.change.resolve", {change_id: change.change_id, task_revision: task.revision, accept});
      if (targetAccount === currentAccount.current && version === listGeneration.current) setData(previous => applyTaskUpdate(previous, next, task.task_id));
    } catch (reason) { if (targetAccount === currentAccount.current && version === listGeneration.current) setError(explainError(reason)); }
    finally { if (version === listGeneration.current) setBusy(false); }
  }
  const items = data?.items || [];
  const newResults = hasNewResults(data?.results_revision, state?.results_revision);
  async function reload() {
    if (busy) return;
    const targetAccount = account;
    const version = ++listGeneration.current;
    setBusy(true); setError(""); setNotice("");
    try {
      const next = await request<TaskPage>(page === "today" ? "today.list" : "tasks.list");
      if (targetAccount === currentAccount.current && version === listGeneration.current) setData(next);
    } catch (reason) {
      if (targetAccount === currentAccount.current && version === listGeneration.current) setError(explainError(reason));
    } finally { if (version === listGeneration.current) setBusy(false); }
  }
  useEffect(() => {if (lastRefreshToken.current !== refreshToken) {lastRefreshToken.current = refreshToken; void reload();}}, [refreshToken]);
  const active = items.filter(task => !["completed", "ignored", "cancelled"].includes(task.status));
  const actions = todayActions(items);
  const knowledge = data?.attention_items || [];
  const groups = attentionGroups(knowledge);
  const todayCounts: Record<TodayTab, number> = {actions: actions.length, information: groups[0].items.length, key_point: groups[1].items.length};
  function selectTodayTab(id: TodayTab) {
    if (id === todayTab) return;
    setTodayTab(id);
    if (contentScroll.current) contentScroll.current.scrollTop = 0;
  }
  const visibleTasks = selectTasks(items, {view, search, deadline, sort});
  const detailTask = items.find(task => task.task_id === detailId);
  const detailAttention = knowledge.find(item => item.attention_id === attentionId);
  const partial = !!(data?.last_analysis?.rejected_candidate_count || data?.last_analysis?.rejected_change_count || data?.last_analysis?.rejected_attention_count);
  const ownerLabel = (task: Task) => task.owner_type === "self" ? "我要做" : task.owner_type === "other" ? "我在等别人" : "归属需确认";
  const statusLabel = (task: Task) => taskStatusLabel(task.status);
  function taskActions(task: Task) {
    return <div className="actions task-actions">{["completed", "ignored", "cancelled"].includes(task.status) ? <button disabled={busy} onClick={() => void update(task, "pending")}>恢复待处理</button> : <>{canClaimSelf(task) && <button className="task-claim" disabled={busy} title="确认由我处理" onClick={() => void claimSelf(task)}>是我</button>}<button disabled={busy} onClick={() => void update(task, "completed")}>{task.owner_type === "other" ? "确认收到" : "完成"}</button><button className="task-ignore" disabled={busy} onClick={() => void update(task, "ignored")}>忽略</button></>}</div>;
  }
  function taskChanges(task: Task) {
    return task.changes?.map(change => <details className="task-change" key={change.change_id}>
      <summary>{change.kind === "details" ? "发现内容修正，待你确认" : change.kind === "completed" ? "聊天提到已完成，待你确认" : "聊天提到已取消，待你确认"}</summary>
      {change.kind === "details" ? <p>{change.proposed.title} · {change.proposed.owner_type === "self" ? "我要做" : change.proposed.owner_type === "other" ? "我在等别人" : "归属需确认"} · {change.proposed.due_at ? `期限 ${change.proposed.due_at}` : "未明确期限"}</p> : <p>当前状态未改变，请核对依据后选择。</p>}
      <div className="source-action"><button className="link-button source-trigger" disabled={!change.source_message_ids.length} onClick={event => void openSource(change.source_message_ids, task.title + " · 变化依据", event.currentTarget)}>查看变化依据<span>{sourceIds(change.source_message_ids).length} 条</span></button></div>
      <div className="actions"><button disabled={busy} onClick={() => void resolveChange(task, change, true)}>{change.kind === "details" ? "采用内容修正" : change.kind === "completed" ? "确认已完成" : "确认已取消"}</button><button disabled={busy} onClick={() => void resolveChange(task, change, false)}>保留当前事项</button></div>
    </details>);
  }
  return <section ref={contentScroll} className="content-scroll results-page">
    {error && <p role="alert" className="notice error">{error}</p>}
    {notice && <p role="status" className="field-note">{notice}</p>}
    {!data && !error && <p role="status">正在读取已保存结果…</p>}
    <div className="results-toolbar" aria-label="整理状态与结果操作" hidden={page === "today" && todayTab === "information"}>
      <p className="result-counts">{page === "today" ? "需关注" : "待处理"} <strong>{page === "today" ? actions.length : active.length}</strong><span>{page === "today" ? `要点 ${groups[1].items.length}` : `已完成 ${items.filter(task => task.status === "completed").length}`}</span></p>
      {data?.last_analysis && <details className={"analysis-status" + (data.last_analysis.status !== "success" || partial ? " attention" : "")}>
        <summary>{data.last_analysis.status === "success" ? (partial ? "部分结果待核对" : "整理已更新") : data.last_analysis.status === "running" ? "正在整理" : "未发布 · 原结果保留"}</summary>
        <div className="analysis-status-detail">
          <p>最近整理：{data.last_analysis.status === "success" ? (partial ? "有效事项已发布，部分结果待核对" : "已发布") : data.last_analysis.status === "running" ? "进行中，请稍后刷新" : "未发布"}</p>
          {data.last_analysis.finished_at && <p>{dateTime(data.last_analysis.finished_at)}</p>}
          {!!data.last_analysis.rejected_candidate_count && <p>{data.last_analysis.rejected_candidate_count} 条候选未发布，核对记录已保留</p>}
          {!!data.last_analysis.rejected_change_count && <p>{data.last_analysis.rejected_change_count} 条状态建议未发布</p>}
          {!!data.last_analysis.rejected_attention_count && <p>{data.last_analysis.rejected_attention_count} 条知晓内容未发布，核对记录已保留</p>}
          {data.last_analysis.error_code && <p>错误标识：{data.last_analysis.error_code}</p>}
          <button className="link-button" onClick={configure}>查看整理设置</button>
        </div>
      </details>}
      <button className={newResults ? "result-refresh has-new" : "result-refresh"} disabled={busy} onClick={() => void reload()}>{busy ? "正在更新…" : newResults ? "有新内容 · 更新结果" : "刷新结果"}</button>
    </div>
    {data && !items.length && !knowledge.length && !data.last_analysis && !state?.daily_news?.latest_day && !(page === "today" && todayTab === "information") && <div className="results-onboarding"><p>还没有整理结果。连接会话并确认文字发送范围后，整理内容会出现在这里。</p><button className="link-button" onClick={configure}>设置文字整理 →</button></div>}
    {page === "tasks" && <>
      <div className="task-viewbar" aria-label="任务视图">
        {(["self", "ownership", "completed"] as const).map(id => <button key={id} aria-pressed={view === id} className={"task-view-button" + (view === id ? " selected" : "")} onClick={() => setView(id)}>{taskViews[id]}<span>{items.filter(task => belongsToView(task, id)).length}</span></button>)}
        <label className="task-extra-view"><span className="sr-only">其他任务视图</span><select aria-label="其他任务视图" value={["waiting", "ignored", "cancelled"].includes(view) ? view : ""} onChange={event => { if (event.target.value) setView(event.target.value as TaskView); }}><option value="" disabled>更多视图</option>{(["waiting", "ignored", "cancelled"] as const).map(id => <option key={id} value={id}>{taskViews[id]} · {items.filter(task => belongsToView(task, id)).length}</option>)}</select></label>
      </div>
      <div className="task-filters">
        <label className="task-search"><span>搜索任务</span><input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索任务标题" /></label>
        <label><span>期限</span><select value={deadline} onChange={event => setDeadline(event.target.value as DeadlineFilter)}><option value="all">全部期限</option><option value="overdue">已逾期</option><option value="today">今天到期</option><option value="week">未来 7 天（含今天）</option><option value="undated">未明确期限</option></select></label>
        <label><span>排序</span><select value={sort} onChange={event => setSort(event.target.value as TaskSort)}><option value="original">保持当前顺序</option><option value="deadline">期限从近到远</option><option value="title">按任务标题</option></select></label>
      </div>
      <div className="task-list-heading"><h3>{taskViews[view]}<span>{visibleTasks.length} 项</span></h3>{(search || deadline !== "all" || sort !== "original") && <button className="link-button" onClick={() => {setSearch(""); setDeadline("all"); setSort("original");}}>清除筛选与排序</button>}</div>
      {data && !visibleTasks.length && <p className="task-empty" role="status">{search || deadline !== "all" ? "没有符合当前条件的任务，可清除筛选后查看。" : view === "self" ? "暂无已确认由我执行的未完成任务；归属不明确的事项请在“待确认归属”查看。" : "暂无此类任务。"}</p>}
      <div className="task-card-grid">
        {visibleTasks.map(task => <article className="task-card" key={task.task_id} aria-label={task.title}>
          <div className="task-card-meta"><span className="task-status">{view === "ownership" ? "归属待确认" : statusLabel(task)}</span><span className={"task-deadline" + (dueDay(task.due_at) && dueDay(task.due_at)! < localDay(new Date()) && !["completed", "ignored", "cancelled"].includes(task.status) ? " overdue" : "")}>{task.due_at ? `期限 ${task.due_at}` : "未明确期限"}</span></div>
          <h4><button className="task-title-button" onClick={event => openDetail(task, event.currentTarget)}>{task.title}</button></h4>
          <p className="task-card-owner">{ownerLabel(task)}</p>
          {!!task.changes?.length && <button className="task-change-hint link-button" onClick={event => openDetail(task, event.currentTarget)}>{task.changes.length} 条变化待确认</button>}
          <footer><button className="link-button source-trigger" disabled={!task.source_message_ids.length} onClick={event => void openSource(task.source_message_ids, task.title, event.currentTarget)}>查看原文<span>{sourceIds(task.source_message_ids).length} 条</span></button>{taskActions(task)}</footer>
        </article>)}
      </div>
    </>}
    {page === "today" && <>
      <div className="today-tabs" role="tablist" aria-label="今天内容分类">
        {todayTabs.map(tab => <button key={tab.id} type="button" role="tab" id={`today-tab-${tab.id}`}
          ref={button => {tabButtons.current[tab.id] = button;}} aria-selected={todayTab === tab.id}
          aria-controls={`today-panel-${tab.id}`} tabIndex={todayTab === tab.id ? 0 : -1}
          onClick={() => selectTodayTab(tab.id)} onKeyDown={event => {
            const next = todayTabForKey(tab.id, event.key);
            if (next === null) return;
            event.preventDefault(); selectTodayTab(next); tabButtons.current[next]?.focus({preventScroll: true});
          }}>
          {tab.title}{tab.id !== "information" && <span>{todayCounts[tab.id]}</span>}
        </button>)}
      </div>
      <section className="result-group today-group today-panel" role="tabpanel" id="today-panel-actions"
        aria-labelledby="today-tab-actions" hidden={todayTab !== "actions"} tabIndex={0}>
        {todayTab === "actions" && <>
        {data && !actions.length && <p className="today-empty">暂无临近、逾期、新发现或待确认的事项；全部任务仍可在待办中查看。</p>}
        <div className="task-card-grid today-action-grid">{actions.map(task => <article className="task-card" key={task.task_id} aria-label={task.title}>
          <div className="task-card-meta"><span className="task-status">{actionReason(task)}</span><span className="task-deadline">{task.due_at ? `期限 ${task.due_at}` : "未明确期限"}</span></div>
          <h4><button className="task-title-button" onClick={event => openDetail(task, event.currentTarget)}>{task.title}</button></h4>
          <p className="task-card-owner">{ownerLabel(task)}</p>
          {!!task.changes?.length && <button className="task-change-hint link-button" onClick={event => openDetail(task, event.currentTarget)}>{task.changes.length} 条变化待确认</button>}
          <footer><button className="link-button source-trigger" disabled={!task.source_message_ids.length} onClick={event => void openSource(task.source_message_ids, task.title, event.currentTarget)}>查看原文<span>{sourceIds(task.source_message_ids).length} 条</span></button>{taskActions(task)}</footer>
        </article>)}</div>
        </>}
      </section>
      <section className="today-panel" role="tabpanel" id="today-panel-information" aria-labelledby="today-tab-information" hidden={todayTab !== "information"} tabIndex={0}>
        {todayTab === "information" && <NewsReader state={state} configure={configure} openSource={(ids,title,button)=>void openSource(ids,title,button)}/>}
      </section>
      {groups.filter(group => group.kind === "key_point").map(group => <section className="result-group today-group today-panel" key={group.kind} role="tabpanel"
        id={`today-panel-${group.kind}`} aria-labelledby={`today-tab-${group.kind}`} hidden={todayTab !== group.kind} tabIndex={0}>
        {todayTab === group.kind && <>
        {data && !group.items.length && <p className="today-empty">暂无已整理的{group.title}。后续整理会从所选会话的文字中提取，不读取分享链接正文。</p>}
        <div className="task-card-grid today-knowledge-grid">{group.items.map(item => <article className="task-card knowledge-card" key={item.attention_id} aria-label={item.title}>
          <div className="task-card-meta"><span className="knowledge-chat">{item.source_chats.map(chat => chat.chat_name || "来源会话暂不可用").join(" · ")}</span><time className="task-deadline">{item.source_kind === "meeting" ? "加入" : "消息"} {dateTime(item.observed_at)}</time></div>
          <h4><button className="task-title-button" onClick={event => openAttention(item, event.currentTarget)}>{item.title}</button></h4>
          <p className="knowledge-summary">{item.summary}</p><p className="knowledge-reason">关注原因 · {item.attention_reason}</p>
          <footer><button className="link-button source-trigger" disabled={!item.source_message_ids.length} onClick={event => void openSource(item.source_message_ids, item.title, event.currentTarget)}>查看原文<span>{sourceIds(item.source_message_ids).length} 条</span></button><button className="link-button" onClick={event => openAttention(item, event.currentTarget)}>查看详情</button></footer>
        </article>)}</div>
        </>}
      </section>)}
    </>}
    <dialog ref={drawer} className="result-source task-drawer" aria-label={source ? "原文来源" : attentionId ? "知晓内容详情" : "任务详情"} onCancel={event => {event.preventDefault(); closeSource();}}>
    {source ? <>
      <header className="source-drawer-header"><div className="section-title"><h3>原文来源 <span>{source.total} 条</span></h3><button ref={closeButton} onClick={closeSource} aria-label="关闭原文" title="关闭原文">×</button></div><p className="source-task-title">{source.title}</p><p className="field-note">只读 · 按来源时间排列</p>{(detailId || attentionId) && <button className="link-button" onClick={() => {generation.current++; setSource(null); setLoadingSource(false);}}>{detailId ? "返回任务详情" : "返回知晓内容详情"}</button>}</header>
      <div className="source-drawer-body" aria-busy={loadingSource}>
      {loadingSource ? <p role="status">正在读取 {source.total} 条原文…</p> : <>
        {source.entries.some(entry => entry.unavailable) && <p className="source-unavailable" role="status">部分来源暂时不可用，其它原文仍可查看。</p>}
        {source.entries.map((entry, index) => <article className="source-entry" key={entry.id}>
          {entry.message ? <><div className="source-entry-meta"><strong>{entry.message.source_kind === "meeting" ? "会议纪要" : messageRelation(entry.message) === true ? "本人" : messageRelation(entry.message) === false ? entry.message.sender_name || "他人" : "发送者身份未确认"}</strong><time>{entry.message.source_kind === "meeting" ? `导入 ${dateTime(entry.message.imported_at)}` : dateTime(entry.message.sent_at)}</time></div>
            {entry.message.source_kind === "meeting" && <p className="field-note">会议日期：{entry.message.meeting_date || "未填写"}</p>}
            <p className="source-chat-name">{entry.message.chat_name}</p>
            {entry.message.content_state !== "text_available" && <p className="field-note">{contentState(entry.message.content_state)}</p>}
            <pre>{entry.message.content ?? "当前无可读正文，不将类型记录当成理解结果。"}</pre>
          </> : <p>来源暂不可用，未补造原文。</p>}
          <details className="source-id"><summary>来源标识 · {index + 1}</summary><p>{entry.id}</p></details>
        </article>)}
      </>}
      </div>
    </> : detailTask ? <>
      <header className="source-drawer-header"><div className="section-title"><h3>任务详情</h3><button ref={closeButton} onClick={closeSource} aria-label="关闭任务详情">×</button></div></header>
      <div className="source-drawer-body task-detail-body">
        <h2>{detailTask.title}</h2><dl className="task-detail-fields"><div><dt>归属</dt><dd>{ownerLabel(detailTask)}</dd></div><div><dt>状态</dt><dd>{statusLabel(detailTask)}</dd></div><div><dt>期限</dt><dd>{detailTask.due_at || "未明确期限"}</dd></div></dl>
        {error && <p role="alert" className="notice error">{error}</p>}
        {notice && <p role="status" className="field-note">{notice}</p>}
        {taskActions(detailTask)}
        <button className="link-button source-trigger" disabled={!detailTask.source_message_ids.length} onClick={event => void openSource(detailTask.source_message_ids, detailTask.title, event.currentTarget)}>查看原文<span>{sourceIds(detailTask.source_message_ids).length} 条</span></button>
        {!!detailTask.changes?.length && <section className="task-detail-changes"><h3>待确认的变化</h3><p>以下建议尚未改动当前事项。</p>{taskChanges(detailTask)}</section>}
      </div>
    </> : detailAttention ? <>
      <header className="source-drawer-header"><div className="section-title"><h3>{detailAttention.kind === "information" ? todayTabs[1].title : todayTabs[2].title}</h3><button ref={closeButton} onClick={closeSource} aria-label="关闭知晓内容详情">×</button></div></header>
      <div className="source-drawer-body task-detail-body knowledge-detail-body">
        <h2>{detailAttention.title}</h2><p className="knowledge-detail-summary">{detailAttention.summary}</p>
        <dl className="task-detail-fields"><div><dt>关注原因</dt><dd>{detailAttention.attention_reason}</dd></div><div><dt>{detailAttention.source_kind === "meeting" ? "会议日期" : "消息时间"}</dt><dd>{detailAttention.source_kind === "meeting" ? detailAttention.meeting_date || "未填写" : dateTime(detailAttention.observed_at)}</dd></div><div><dt>{detailAttention.source_kind === "meeting" ? "会议来源" : "来源会话"}</dt><dd>{detailAttention.source_chats.map(chat => chat.chat_name || "来源会话暂不可用").join(" · ")}</dd></div></dl>
        <p className="field-note">{detailAttention.source_kind === "meeting" ? "根据会议纪要整理，非独立事实核验；导入时间不等于会议时间。" : "根据聊天文字整理，非独立事实核验；消息时间不等于事件发生时间。"}</p>
        <button className="link-button source-trigger" disabled={!detailAttention.source_message_ids.length} onClick={event => void openSource(detailAttention.source_message_ids, detailAttention.title, event.currentTarget)}>查看原文<span>{sourceIds(detailAttention.source_message_ids).length} 条</span></button>
      </div>
    </> : (detailId || attentionId) ? <><header className="source-drawer-header"><button ref={closeButton} onClick={closeSource}>关闭详情</button></header><p>此内容当前不可用，请刷新后查看。</p></> : null}
    </dialog>
  </section>;
}
