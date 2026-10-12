import React, {useEffect, useRef, useState} from "react";
import {request, explainError} from "./bridge";
import {dateTime, type State} from "./types";
import {decodeMeetingFile, meetingFileError, meetingStatus, MEETING_MAX_CHARS,
  type MeetingNote, type MeetingPreview} from "./meeting-input";
import "./meeting-panel.css";

type History = {items: MeetingNote[]; total: number; offset: number; has_more: boolean};
export function MeetingPanel({state, close, published, configure, onDirty, onBusy}: {
  state: State; close: () => void; published: () => Promise<unknown>; configure: () => void;
  onDirty: (value: boolean) => void; onBusy: (value: boolean) => void;
}) {
  const [view, setView] = useState<"form" | "history">("form");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [date, setDate] = useState("");
  const [fileName, setFileName] = useState<string | null>(null);
  const [note, setNote] = useState<MeetingNote | null>(null);
  const [preview, setPreview] = useState<MeetingPreview | null>(null);
  const [consent, setConsent] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [history, setHistory] = useState<History | null>(null);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const generation = useRef(0);
  const historyGeneration = useRef(0);
  const account = state.source?.account_id ?? "";
  const previousAccount = useRef(account);
  useEffect(() => {
    const trigger = document.activeElement as HTMLElement | null;
    dialog.current?.showModal(); closeButton.current?.focus();
    return () => {generation.current++; historyGeneration.current++; onDirty(false); onBusy(false); trigger?.focus();};
  }, []);
  useEffect(() => {onDirty(!!(content || title || date));}, [content, title, date]);
  useEffect(() => {onBusy(busy);}, [busy]);
  useEffect(() => {
    if (previousAccount.current === account) return;
    previousAccount.current = account; generation.current++; historyGeneration.current++;
    setNote(null); setTitle(""); setContent(""); setDate(""); setFileName(null); setHistory(null);
    setSelected([]); setError(""); setNotice(""); setPreview(null); setConsent(false); setBusy(false);
  }, [account]);
  useEffect(() => {setPreview(null); setConsent(false);}, [state.text_model?.revision, state.text_model?.enabled, account]);
  const readyModel = state.text_model?.enabled && state.text_model.protocol === "gemini";

  function showNote(value: MeetingNote) {
    setNote(value); setSelected(value.result?.candidates.map(c => c.candidate_id) ?? []);
    setPreview(null); setConsent(false); setView("form");
  }
  async function act(action: (isCurrent: () => boolean) => Promise<void>) {
    if (busy) return;
    const version = generation.current;
    const isCurrent = () => version === generation.current;
    setBusy(true); setError(""); setNotice("");
    try {await action(isCurrent);}
    catch (reason) {if (isCurrent()) setError(explainError(reason));}
    finally {if (isCurrent()) setBusy(false);}
  }
  async function save(prepare: boolean) {
    await act(async current => {
      const value = note ?? await request<MeetingNote>("meeting.save", {title, content, meeting_date: date || null, file_name: fileName});
      if (!current()) return;
      showNote(value);
      setTitle(""); setContent(""); setDate(""); setFileName(null);
      if (value.duplicate) setNotice("这份纪要已保存，未重复导入。");
      if (prepare && ["draft", "failed"].includes(value.status)) {
        const p = await request<MeetingPreview>("meeting.preview", {meeting_id: value.meeting_id, revision: value.revision});
        if (current()) {setPreview(p); setConsent(false);}
      }
    });
  }
  async function generate() {
    if (!preview || !consent || !readyModel) return;
    const p = preview;
    await act(async current => {
      // A failed request consumes its send confirmation; a retry needs a fresh preview.
      setPreview(null); setConsent(false);
      try {
        const value = await request<MeetingNote>("meeting.generate", {meeting_id: p.meeting_id, revision: p.revision,
          preview_token: p.preview_token, authorize: true});
        if (current()) showNote(value);
      } catch (reason) {
        const value = await request<MeetingNote>("meeting.detail", {meeting_id: p.meeting_id}).catch(() => null);
        if (current() && value) showNote(value);
        throw reason;
      }
    });
  }
  async function publish() {
    if (!note || !selected.length) return;
    const target = note;
    await act(async current => {
      const value = await request<MeetingNote>("meeting.publish", {meeting_id: target.meeting_id,
        revision: target.revision, candidate_ids: selected, confirm: true});
      if (!current()) return;
      showNote(value); setNotice(`已加入 ${value.result?.task_count ?? 0} 条待办、${value.result?.key_point_count ?? 0} 条要点。`);
      try {await published();} catch {if (current()) setError("内容已保存，请稍后刷新 Today。");}
    });
  }
  async function readFile(file: File) {
    await act(async current => {
      const invalid = meetingFileError(file.name, file.size);
      if (invalid) throw new Error(invalid);
      const value = decodeMeetingFile(await file.arrayBuffer());
      if (current()) {setContent(value); setFileName(file.name); setPreview(null); setConsent(false);}
    });
  }
  async function loadHistory(offset = 0) {
    const version = ++historyGeneration.current;
    try {
      const value = await request<History>("meeting.list", {query, limit: 20, offset});
      if (version === historyGeneration.current) setHistory(value);
    } catch (reason) {if (version === historyGeneration.current) setError(explainError(reason));}
  }
  useEffect(() => {if (view === "history") void loadHistory();}, [view, query, account]);
  function newNote() {
    // Reading saved notes must not discard an unsaved input draft.
    setNote(null);
    setView("form"); setPreview(null); setConsent(false); setError(""); setNotice("");
  }
  const candidates = note?.result?.candidates ?? [];
  return <dialog ref={dialog} className="meeting-drawer" aria-label="导入纪要"
    onCancel={event => {event.preventDefault(); if (!busy) close();}}>
    <header className="meeting-header"><div><h2>导入纪要</h2><p>提取会议要点和待办</p></div>
      <button ref={closeButton} disabled={busy} aria-label="关闭纪要" onClick={close}>×</button></header>
    <div className="meeting-tabs" aria-label="纪要查看方式">
      <button disabled={busy} aria-pressed={view === "form"} onClick={newNote}>新纪要</button>
      <button disabled={busy} aria-pressed={view === "history"} onClick={() => {setView("history"); setError(""); setNotice("");}}>导入记录</button>
    </div>
    <div className="meeting-body" aria-busy={busy}>
      {error && <p className="meeting-feedback error" role="alert">{error}</p>}
      {notice && <p className="meeting-feedback" role="status">{notice}</p>}
      {busy && <p className="meeting-feedback" role="status">正在处理，请稍候…</p>}
      {view === "history" ? <>
        <label>查找纪要<input type="search" value={query} onChange={event => setQuery(event.currentTarget.value)} placeholder="输入会议标题"/></label>
        <p className="field-note">{history ? `共 ${history.total} 份纪要` : "正在读取记录…"}</p>
        {history?.items.map(item => <button className="meeting-history-row" key={item.meeting_id} disabled={busy}
          onClick={() => void act(async current => {const value = await request<MeetingNote>("meeting.detail", {meeting_id: item.meeting_id}); if (current()) showNote(value);})}>
          <span><strong>{item.title}</strong><small>{item.meeting_date || "会议日期未填写"} · 导入 {dateTime(item.created_at)}</small></span>
          <span>{meetingStatus[item.status]}</span></button>)}
        {history?.total === 0 && <p className="field-note">还没有导入纪要。</p>}
        {history && history.total > 20 && <div className="meeting-actions"><button disabled={!history.offset} onClick={() => void loadHistory(history.offset - 20)}>上一页</button><button disabled={!history.has_more} onClick={() => void loadHistory(history.offset + 20)}>下一页</button></div>}
      </> : !note ? <>
        <div className="meeting-form-meta"><label>会议标题<input value={title} maxLength={200} disabled={busy} onChange={event => setTitle(event.currentTarget.value)} placeholder="选填，例如：项目评审"/></label>
          <label>会议日期<input type="date" value={date} disabled={busy} onChange={event => setDate(event.currentTarget.value)}/></label></div>
        <p className="field-note">不确定日期时可留空。</p>
        <label>纪要原文<textarea value={content} rows={12} disabled={busy} onChange={event => {setContent(event.currentTarget.value); setFileName(null);}} placeholder="粘贴会议纪要，保留责任人和期限。"/></label>
        <div className="meeting-file-row"><label className="meeting-file-button">选择文件<input type="file" accept=".txt,.md,.markdown,text/plain,text/markdown" disabled={busy} onChange={event => {const file = event.currentTarget.files?.[0]; event.currentTarget.value = ""; if (file) void readFile(file);}}/></label><span>{fileName || "TXT／Markdown · UTF-8"}</span></div>
        <p className="field-note">{content.length.toLocaleString()} / 120,000 字</p>
        <div className="meeting-actions"><button disabled={busy || !content.trim() || content.length > MEETING_MAX_CHARS} onClick={() => void save(false)}>保存纪要</button>
          <button className="primary" disabled={busy || !readyModel || !content.trim() || content.length > MEETING_MAX_CHARS} onClick={() => void save(true)}>预览发送</button></div>
        <p className="field-note">保存仅在本机，不发送内容。</p>
        {!readyModel && <p className="meeting-model-help">请先启用 Gemini 整理服务。<button className="link-button" onClick={configure}>配置服务</button></p>}
      </> : <>
        <div className="meeting-note-heading"><h3>{note.title}</h3><span>{meetingStatus[note.status]}</span></div>
        <p className="field-note">会议日期：{note.meeting_date || "未填写"} · 导入 {dateTime(note.created_at)}</p>
        <details className="meeting-original"><summary>查看原文</summary><pre>{note.original}</pre></details>
        {note.status === "failed" && <p className="meeting-feedback">上次生成未完成，重试可能再次计费。</p>}
        {["draft", "failed"].includes(note.status) && !preview && <div className="meeting-actions"><button className="primary" disabled={busy || !readyModel} onClick={() => void save(true)}>预览发送</button>{!readyModel && <button onClick={configure}>配置服务</button>}</div>}
        {note.status === "generating" && <div className="meeting-actions"><span>正在生成，请稍后刷新。</span><button disabled={busy} onClick={() => void act(async current => {const value = await request<MeetingNote>("meeting.detail", {meeting_id: note.meeting_id}); if (current()) showNote(value);})}>刷新记录</button></div>}
        {preview && <section className="meeting-consent" aria-label="确认发送纪要"><h3>确认发送</h3><dl><dt>接收服务</dt><dd>{preview.base_url}</dd><dt>使用模型</dt><dd>{preview.model}</dd></dl>
          <p>将发送到上述服务，可能产生费用。</p><p>已自动脱敏，仍可能漏检。</p>
          <details><summary>查看发送内容</summary><p>标题：{preview.title}</p><p>会议日期：{preview.meeting_date || "未填写"}</p>{preview.segments.map(part => <pre key={part.segment_id}>{part.content}</pre>)}</details>
          <label className="checkbox-row"><input type="checkbox" checked={consent} disabled={busy} onChange={event => setConsent(event.currentTarget.checked)}/>同意发送这份纪要</label>
          <div className="meeting-actions"><button className="primary" disabled={busy || !consent || !readyModel} onClick={() => void generate()}>生成内容</button><button disabled={busy} onClick={() => {setPreview(null); setConsent(false);}}>暂不发送</button></div>
        </section>}
        {note.result && <section className="meeting-candidates" aria-label="会议生成内容"><h3>{note.status === "published" ? "已生成内容" : "预览结果"}</h3>
          <p className="field-note">{candidates.filter(c => c.kind === "key_point").length} 条要点 · {candidates.filter(c => c.kind === "task").length} 条待办</p>
          {!!note.result.rejected_count && <p className="meeting-feedback">{note.result.rejected_count} 条内容未通过核对，未加入预览。</p>}
          {note.status === "ready" && <p className="field-note">选择需要加入的内容。</p>}
          {candidates.map(candidate => <article className="meeting-candidate" key={candidate.candidate_id}>
            <label>{note.status === "ready" && <input type="checkbox" checked={selected.includes(candidate.candidate_id)} disabled={busy} onChange={event => {const checked = event.currentTarget.checked; setSelected(ids => checked ? [...ids, candidate.candidate_id] : ids.filter(id => id !== candidate.candidate_id));}}/>}
              <span><small>{candidate.kind === "task" ? "待办" : "关键要点"}{note.status === "published" && !note.result?.published_ids?.includes(candidate.candidate_id) ? " · 未加入" : ""}</small><strong>{candidate.title}</strong></span></label>
            {candidate.kind === "task" ? <p>负责人：{candidate.responsible || "未明确"} · {candidate.due_at ? `期限 ${candidate.due_at}` : candidate.due_text ? `期限待确认：${candidate.due_text}` : "未明确期限"}</p> : <><p>{candidate.summary}</p><p className="field-note">{candidate.reason}</p></>}
            <details><summary>查看依据</summary>{candidate.evidence.map((e, i) => <blockquote key={i}>{e.quote}</blockquote>)}</details>
          </article>)}
          {note.status === "ready" && <><p className="field-note">待办先进入“待确认归属”。</p><div className="meeting-actions"><button disabled={busy || !candidates.length} onClick={() => setSelected(selected.length === candidates.length ? [] : candidates.map(c => c.candidate_id))}>{selected.length === candidates.length ? "取消全选" : "全部选择"}</button><button className="primary" disabled={busy || !selected.length} onClick={() => void publish()}>加入所选</button><span>已选 {selected.length} 条</span></div>{!candidates.length && <p>没有找到可加入的内容，原文已保留。</p>}</>}
          {note.status === "published" && <p className="field-note">要点在 Today，任务在待办查看。</p>}
        </section>}
      </>}
    </div>
  </dialog>;
}
