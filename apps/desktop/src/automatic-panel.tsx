import React, { useEffect, useRef, useState } from "react";
import { request, explainError } from "./bridge";
import type { State, TextScope } from "./types";
import { dateTime } from "./types";
import { automaticStatus, batchStatusLabel } from "./processing-state";

type GrantPreview = TextScope & {
  preview_token: string; warning: string; message_count: number; categories: string[];
  chats: {chat_id: string; name: string; chat_type: string}[];
  messages: {source_message_id: string; sent_at: string; speaker: string; content: string}[];
};

export function AutomaticPanel({ state, refresh, disabled }: {
  state: State; refresh: () => Promise<unknown>; disabled: boolean;
}) {
  const [preview, setPreview] = useState<GrantPreview | null>(null);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [failed, setFailed] = useState(false);
  const generation = useRef(0);
  const automatic = state.automatic_text;
  const grant = automatic?.grant;
  const selected = state.chats.filter(chat => chat.sync_enabled && chat.analysis_enabled);
  const status = automaticStatus(state);
  const scopeKey = JSON.stringify([state.source, state.time_start, selected.map(chat => chat.chat_id).sort(),
    state.text_model?.base_url, state.text_model?.model, state.text_model?.protocol, state.text_model?.revision]);
  useEffect(() => { generation.current++; setPreview(null); setConsent(false); setNotice(""); }, [scopeKey]);
  useEffect(() => {
    if (!notice || failed) return;
    const timer = setTimeout(() => setNotice(""), 5000);
    return () => clearTimeout(timer);
  }, [notice, failed]);
  async function action<T>(command: string, params: Record<string, unknown>, success: string) {
    if (busy || disabled) return;
    const version = generation.current;
    setBusy(true); setNotice(""); setFailed(false);
    try {
      const value = await request<T>(command, params);
      await refresh();
      if (version === generation.current) {setNotice(success); return value;}
    } catch (error) { if (version === generation.current) {setFailed(true); setNotice(explainError(error));} }
    finally { setBusy(false); }
  }
  const blocked = busy || disabled;
  const sendBlocked = blocked || state.text_model?.storage_mode !== "sqlite_plaintext";
  const active = grant?.status === "active";
  return <section className="processing-card automatic-card" aria-labelledby="automatic-title">
    <header className="processing-card-heading"><div><h3 id="automatic-title">自动整理</h3><p>持续发现新消息里的待办、资讯和要点</p></div><span className={"processing-status " + status.tone}>{status.label}</span></header>
    <dl className="processing-facts"><div><dt>处理会话</dt><dd>{selected.length} 个</dd></div><div><dt>待处理文字</dt><dd>{automatic?.pending_messages ?? 0} 条</dd></div><div className="processing-full"><dt>处理起点</dt><dd>{dateTime(active ? grant.scope.time_start : state.time_start)}</dd></div></dl>
    <p className="processing-description" role="status">{status.detail}</p>
    <p className="processing-rule">聊天停止更新 {Math.round((automatic?.quiet_seconds ?? 600) / 60)} 分钟后整理，最长等待 {Math.round((automatic?.max_wait_seconds ?? 1800) / 60)} 分钟。新结果通过 Today／待办的“更新结果”查看。</p>
    <div className="processing-actions"><button disabled={blocked || !selected.length || !state.text_model?.enabled}
      onClick={() => void action<GrantPreview>("authorization.preview", {}, "").then(value => { if (value) { setPreview(value); setConsent(false); } })}>{active ? "查看处理范围" : "设置并开启"}</button>
      {active && <button className="link-button" disabled={blocked} onClick={() => void action("authorization.revoke", {}, "自动整理已关闭；本地同步和已有结果保留。").then(() => { setPreview(null); setConsent(false); })}>关闭自动整理</button>}</div>
    {notice && <p className={failed ? "processing-feedback error" : "processing-feedback"} role={failed ? "alert" : "status"}>{notice}</p>}
    {preview && <div className="processing-preview">
      <header><h4>自动整理的发送范围</h4><button className="link-button" disabled={blocked} onClick={() => setPreview(null)}>收起范围</button></header>
      <p>{preview.chats.length} 个会话 · 从 {dateTime(preview.time_start)} 起的文字及后续新增文字</p>
      <p className="processing-recipient">接收服务：{preview.base_url}<br/>文本模型：{preview.model}</p>
      <details><summary>查看会话名单</summary><ul className="processing-chat-list">{preview.chats.map(chat => <li key={chat.chat_id}>{chat.name}</li>)}</ul></details>
      <p>目前有 {preview.message_count} 条未提交文字。内容会发送到上述服务，可能产生费用；自动脱敏可能漏检。</p>
      <details><summary>查看发送说明与脱敏预览</summary><p>{preview.warning}</p>{preview.messages.map(message => <article key={message.source_message_id}><strong>{message.speaker} · {dateTime(message.sent_at)}</strong><p className="preview-text">{message.content}</p></article>)}</details>
      {!active && <><label className="checkbox-row"><input type="checkbox" checked={consent} disabled={blocked} onChange={event => setConsent(event.currentTarget.checked)}/>同意持续发送上述范围内的文字，可能产生费用</label>
        <button className="primary" disabled={sendBlocked || !consent} onClick={() => void action("authorization.save", {authorize: true, preview_token: preview.preview_token}, "自动整理已开启。").then(() => { setPreview(null); setConsent(false); })}>确认并开启自动整理</button></>}
    </div>}
  </section>;
}

export function ProcessingHistory({ state, refresh, disabled }: {state: State; refresh: () => Promise<unknown>; disabled: boolean}) {
  const [retry, setRetry] = useState<string | null>(null);
  const [retryConsent, setRetryConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const grant = state.automatic_text?.grant;
  const batches = state.automatic_text?.batches || [];
  useEffect(() => {setRetry(null); setRetryConsent(false);}, [grant?.status, grant?.created_at]);
  const blocked = busy || disabled || state.text_model?.storage_mode !== "sqlite_plaintext";
  return <details className="processing-history"><summary>自动整理记录<span>最近 {batches.length} 个批次</span></summary>
    <p className="field-note">记录保留各批次当时的状态，不代表现在的自动整理状态。未发布或结果待核实的批次不会自动重发。</p>
    {error && <p role="alert" className="processing-feedback error">{error}</p>}
    {!batches.length && <p>还没有整理批次，收到新消息并达到等待时间后会开始处理。</p>}
    <div className="processing-history-list">{batches.map(batch => <div className="processing-history-row" key={batch.batch_id}><div><strong>{batch.chat_name}</strong><p>{dateTime(batch.created_at)} · {batch.message_count} 条文字{batch.current_scope === false ? " · 历史范围" : ""}</p></div><span>{batchStatusLabel(batch.status, batch.error_code)}</span>{["failed", "unknown"].includes(batch.status) && <button disabled={blocked || !batch.retryable} onClick={() => {setRetry(batch.batch_id); setRetryConsent(false);}}>核对重试</button>}</div>)}</div>
    {retry && <div className="processing-preview"><p>此操作会重新发送该批次，可能重复计费；已发送请求无法撤回。</p><label className="checkbox-row"><input type="checkbox" checked={retryConsent} disabled={blocked} onChange={event => setRetryConsent(event.currentTarget.checked)}/>同意在当前授权范围下重新发送此批次</label><button className="primary" disabled={blocked || !retryConsent || grant?.status !== "active"} onClick={async () => {
      setBusy(true); setError("");
      try {await request("analysis.batch.retry", {batch_id: retry, authorize: true}); await refresh(); setRetry(null); setRetryConsent(false);}
      catch (reason) {setError(explainError(reason));} finally {setBusy(false);}
    }}>确认重试此批次</button></div>}
  </details>;
}
