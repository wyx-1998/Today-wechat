import React, { useEffect, useRef, useState } from "react";
import { request, explainError } from "./bridge";
import type { State } from "./types";
import { chatName, dateTime } from "./types";

type Preview = {chat_id: string; preview_token: string; message_count: number; warning: string;
  time_start: string; time_end: string; mode?: "reanalysis";
  messages: {source_message_id: string; sent_at: string; speaker: string; content: string}[];
  existing_tasks?: {task_id: string; title: string; status: string}[]};
type Result = {status: string; task_count: number; attention_count?: number; change_count?: number;
  rejected_candidate_count?: number; rejected_change_count?: number; rejected_attention_count?: number};

export function ManualPanel({state, refresh, disabled, resetToken}: {
  state: State; refresh: () => Promise<unknown>; disabled: boolean; resetToken: number;
}) {
  const chats = state.chats.filter(chat => chat.sync_enabled);
  const [selected, setSelected] = useState("");
  const [search, setSearch] = useState("");
  const [review, setReview] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const generation = useRef(0);
  const chatId = chats.some(chat => chat.chat_id === selected) ? selected : chats[0]?.chat_id || "";
  const scopeKey = JSON.stringify([state.source, state.revision, state.time_start, state.text_model?.revision, state.paused, chatId, review, resetToken]);
  useEffect(() => {generation.current++; setPreview(null); setConsent(false); setError(""); setNotice("");}, [scopeKey]);
  useEffect(() => {if (!notice) return; const timer = setTimeout(() => setNotice(""), 7000); return () => clearTimeout(timer);}, [notice]);
  const matches = chats.filter(chat => chatName(chat).toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  const blocked = busy || disabled;
  const sendBlocked = blocked || !state.text_model?.enabled || state.text_model?.storage_mode !== "sqlite_plaintext";
  async function prepare() {
    const version = generation.current;
    setBusy(true); setError(""); setNotice("");
    try {
      const value = await request<Preview>(review ? "analysis.review.preview" : "analysis.real.preview", {chat_id: chatId});
      if (version === generation.current) {setPreview(value); setConsent(false);}
    } catch (reason) {if (version === generation.current) setError(explainError(reason));}
    finally {setBusy(false);}
  }
  async function run() {
    if (!preview || !consent || sendBlocked) return;
    const version = generation.current;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await request<Result>(preview.mode === "reanalysis" ? "analysis.review" : "analysis.real",
        {chat_id: preview.chat_id, authorize: true, preview_token: preview.preview_token});
      await refresh();
      if (version === generation.current) {
        const rejected = (result.rejected_candidate_count ?? 0) + (result.rejected_change_count ?? 0) + (result.rejected_attention_count ?? 0);
        setNotice(["no_messages", "no_new_messages", "already_processed"].includes(result.status) ? "没有需要处理的新文字，未重复发送。"
          : `整理完成：新增 ${result.task_count} 条待办、${result.attention_count ?? 0} 条知晓内容，${result.change_count ?? 0} 条变化待确认。${rejected ? `另有 ${rejected} 条建议未发布，核对记录已保留。` : ""}`);
      }
    } catch (reason) {if (version === generation.current) setError(explainError(reason));}
    finally {if (version === generation.current) {setPreview(null); setConsent(false);} setBusy(false);}
  }
  return <section className="processing-card manual-card" aria-labelledby="manual-title">
    <header className="processing-card-heading"><div><h3 id="manual-title">手动整理</h3><p>选择一个会话，仅处理本次确认的消息</p></div><span className="processing-status neutral">{busy ? "处理中" : "按需处理"}</span></header>
    <label className="processing-label" htmlFor="manual-chat-search">查找会话</label><input id="manual-chat-search" type="search" placeholder="输入会话名称" value={search} disabled={blocked} onChange={event => setSearch(event.currentTarget.value)}/>
    <label className="processing-label" htmlFor="manual-chat">整理会话</label><select id="manual-chat" value={chatId} disabled={blocked || !chats.length} onChange={event => setSelected(event.currentTarget.value)}>
      {!matches.some(chat => chat.chat_id === chatId) && chats.find(chat => chat.chat_id === chatId) && <option value={chatId}>{chatName(chats.find(chat => chat.chat_id === chatId)!)}</option>}
      {matches.map(chat => <option key={chat.chat_id} value={chat.chat_id}>{chatName(chat)}</option>)}
    </select>
    {search && <p className="field-note">匹配 {matches.length} 个会话；搜索不会自动更改所选会话。</p>}
    <label className="processing-label" htmlFor="manual-mode">处理方式</label><select id="manual-mode" disabled={blocked} value={review ? "review" : "new"} onChange={event => setReview(event.currentTarget.value === "review")}><option value="new">只整理未处理消息</option><option value="review">重新核对已处理内容</option></select>
    <p className={review ? "processing-warning" : "processing-description"}>{review ? "重新核对会再次发送当前范围内的文字和相关事项，可能重复计费；已有用户状态保留。" : "预览会固定本次消息清单，新收到的消息不混入本次整理；无需暂停同步。"}</p>
    <p className="processing-rule">仅处理 {dateTime(state.time_start)} 之后已同步的文字，不扩大当前时间范围。</p>
    {!chats.length && <p className="processing-warning">还没有开启同步的会话，请先在“会话与标签”选择。</p>}
    {!state.text_model?.enabled && <p className="processing-warning">请先配置并启用上方的文本模型。</p>}
    <div className="processing-actions"><button disabled={sendBlocked || !chatId} onClick={() => void prepare()}>{busy ? "正在处理…" : "预览本次消息"}</button></div>
    {error && <p role="alert" className="processing-feedback error">{error}</p>}
    {notice && <p role="status" className="processing-feedback">{notice}</p>}
    {preview && <div className="processing-preview">
      <header><h4>本次固定范围</h4><button className="link-button" disabled={blocked} onClick={() => {setPreview(null); setConsent(false);}}>取消本次整理</button></header>
      <p><strong>{preview.message_count} 条文字</strong> · 截至 {dateTime(preview.time_end)}</p>
      <p className="processing-recipient">接收服务：{state.text_model?.base_url}<br/>文本模型：{state.text_model?.model}</p>
      <p>预览不发送内容。确认后将发送到上述服务，可能产生费用；自动脱敏可能漏检。</p>
      {preview.mode === "reanalysis" && <details><summary>查看将发送的相关事项 · {preview.existing_tasks?.length ?? 0} 条</summary>{preview.existing_tasks?.map(task => <p key={task.task_id}>{task.title}</p>)}</details>}
      <details><summary>查看发送内容与说明</summary><p>{preview.warning}</p>{preview.messages.map(message => <article key={message.source_message_id}><strong>{message.speaker} · {dateTime(message.sent_at)}</strong><p className="preview-text">{message.content}</p></article>)}</details>
      {preview.message_count === 0 ? <p className="processing-description">当前没有符合本次范围的消息，未发送请求。</p> : <><label className="checkbox-row"><input type="checkbox" checked={consent} disabled={blocked} onChange={event => setConsent(event.currentTarget.checked)}/>同意发送本次预览中的文字{preview.mode === "reanalysis" ? "和相关事项" : ""}，可能产生费用</label><button className="primary" disabled={sendBlocked || !consent} onClick={() => void run()}>{busy ? "正在整理…" : preview.mode === "reanalysis" ? "确认并重新核对" : "确认并整理"}</button></>}
    </div>}
  </section>;
}
