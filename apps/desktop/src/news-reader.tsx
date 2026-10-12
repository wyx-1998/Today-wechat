import React, {useEffect, useRef, useState} from "react";
import {request, explainError} from "./bridge";
import {dateTime, type State} from "./types";
import "./news-reader.css";

type Topic = {candidate_id: string; kind: "ai" | "work" | "other"; title: string; summary: string; source_message_ids: string[]};
type Report = {day: string; results_revision: number; edition: {revision: number; created_at: string; body: {topics: Topic[]; overview: string[];
  message_count: number; chat_count: number; coverage_warnings: string[]; rejected_count: number}} | null;
  run: {status: string; error_code: string | null} | null; late_messages: number};
type Setup = {preview_token: string; first_day: string; time_start: string; base_url: string; model: string; chats: {chat_id: string; name: string}[]};
type Preview = {preview_token: string; day: string; message_count: number; base_url: string; model: string;
  parts: {ref: string; content: string; chat: string; sent_at: string}[]};
const sections = [{kind:"ai",title:"AI 动态"},{kind:"work",title:"工作通知"},{kind:"other",title:"其他资讯"}] as const;

export function DailyControls({state, refresh, disabled=false}: {state: State; refresh: () => Promise<unknown>; disabled?: boolean}) {
  const [preview,setPreview]=useState<Setup | null>(null), [consent,setConsent]=useState(false);
  const [busy,setBusy]=useState(false), [error,setError]=useState("");
  const generation=useRef(0);
  const daily=state.daily_news;
  useEffect(() => {generation.current++; setPreview(null); setConsent(false); setBusy(false); setError("");},
    [state.source?.account_id,state.text_model?.revision,daily?.revision,state.revision]);
  useEffect(() => () => {generation.current++;},[]);
  async function action(command: string,params: Record<string,unknown>={}) {
    if(busy || disabled) return;
    const version=generation.current; setBusy(true); setError("");
    try {
      const result=await request<Setup>(command,params);
      if(version!==generation.current) return;
      if(command==="daily.preview") {setPreview(result); setConsent(false);}
      else {setPreview(null);setConsent(false);await refresh();}
    } catch(reason) {if(version===generation.current) {setError(explainError(reason));setPreview(null);setConsent(false);}}
    finally {if(version===generation.current) setBusy(false);}
  }
  return <section className="daily-controls" aria-label="资讯日报设置">
    <div className="section-title"><h2>资讯日报</h2><span>{daily?.enabled ? daily.valid ? daily.paused ? "同步已暂停" : "已开启" : "需确认范围" : "未开启"}</span></div>
    <p>每天 10:00 汇总前一天资讯。</p><p className="field-note">北京时间 · 应用退出后停止运行</p>
    {error && <p role="alert" className="notice error">{error}</p>}
    <div className="actions">
      <button disabled={busy || disabled || !state.text_model?.enabled || !state.chats.some(chat=>chat.sync_enabled && chat.analysis_enabled)}
        onClick={() => void action("daily.preview")}>{daily?.enabled ? "核对范围" : "开启日报"}</button>
      {daily?.enabled && <button disabled={busy || disabled} onClick={() => void action("daily.disable",{revision:daily.revision})}>关闭日报</button>}
    </div>
    {!state.chats.some(chat=>chat.sync_enabled && chat.analysis_enabled) && <p className="field-note">请先选择需要分析的会话。</p>}
    <p className="field-note">日报与自动整理分别授权。</p>
    {preview && <div className="daily-consent"><h3>确认范围</h3>
      <p>{preview.chats.length} 个会话 · 从 {preview.first_day} 起生成日报</p>
      <p className="field-note">读取起点：{dateTime(preview.time_start)}</p>
      <dl><dt>接收服务</dt><dd>{preview.base_url}</dd><dt>模型</dt><dd>{preview.model}</dd></dl>
      <details><summary>查看会话</summary>{preview.chats.map(chat=><p key={chat.chat_id}>{chat.name}</p>)}</details>
      <p>日报另行请求，可能产生费用。</p><p>发送前脱敏，仍可能漏检。</p>
      <label className="checkbox-row"><input type="checkbox" checked={consent} disabled={busy || disabled} onChange={e=>setConsent(e.currentTarget.checked)}/>同意按此范围生成日报</label>
      <div className="actions"><button className="primary" disabled={busy || disabled || !consent}
        onClick={()=>void action("daily.enable",{preview_token:preview.preview_token,authorize:true})}>确认开启</button>
        <button disabled={busy} onClick={()=>{setPreview(null);setConsent(false);}}>暂不开启</button></div>
    </div>}
  </section>;
}

export function NewsReader({state,configure,openSource}: {state: State | null; configure: () => void;
  openSource: (ids: string[], title: string, button: HTMLButtonElement) => void}) {
  const [report,setReport]=useState<Report | null>(null),[day,setDay]=useState("");
  const [busy,setBusy]=useState(false),[error,setError]=useState("");
  const [preview,setPreview]=useState<Preview | null>(null),[consent,setConsent]=useState(false);
  const [readRevision,setReadRevision]=useState(0);
  const generation=useRef(0), requests=useRef(0);
  const daily=state?.daily_news;
  const account=state?.source?.account_id ?? "";
  useEffect(()=>{generation.current++;setReport(null);setDay("");setPreview(null);setConsent(false);setError("");setBusy(false);void load();
    return ()=>{generation.current++;requests.current++;};},[account]);
  useEffect(()=>{setPreview(null);setConsent(false);},[daily?.revision,state?.text_model?.revision,state?.revision]);
  async function load(target?: string) {
    const version=++requests.current,scope=generation.current; setError("");
    try {
      const value=await request<Report>("daily.detail",target ? {day:target} : {});
      if(version===requests.current && scope===generation.current) {setReport(value);setDay(value.day);setReadRevision(value.results_revision);}
    } catch(reason) {if(version===requests.current && scope===generation.current) setError(explainError(reason));}
  }
  async function prepare() {
    if(busy || !day) return;
    const version=generation.current; setBusy(true);setError("");setConsent(false);
    try {const p=await request<Preview>("daily.preview_run",{day});if(version===generation.current) setPreview(p);}
    catch(reason){if(version===generation.current)setError(explainError(reason));}
    finally {if(version===generation.current)setBusy(false);}
  }
  async function generate() {
    if(!preview || !consent || busy) return;
    const p=preview,version=generation.current; setBusy(true);setPreview(null);setConsent(false);setError("");
    try {const value=await request<Report>("daily.run",{day:p.day,preview_token:p.preview_token,authorize:true});
      if(version===generation.current) {setReport(value);setReadRevision(value.results_revision);}}
    catch(reason){if(version===generation.current){await load(p.day);setError(explainError(reason));}}
    finally{if(version===generation.current)setBusy(false);}
  }
  const edition=report?.edition, topics=edition?.body.topics ?? [];
  return <div className="news-reader" aria-busy={busy}>
    <header className="news-heading"><div><span className="news-kicker">资讯日报</span><h2>{day || "昨日资讯"}</h2>
      <p>{edition ? `更新 ${dateTime(edition.created_at)}` : "每天 10:00 更新 · 北京时间"}</p></div>
      <div className="news-date-actions"><label>选择日期<input type="date" value={day} min={daily?.first_day || undefined} max={daily?.yesterday} disabled={busy}
        onChange={e=>{const value=e.currentTarget.value;if(value){setPreview(null);setConsent(false);void load(value);}}}/></label>
        <button disabled={busy} onClick={()=>void load(day)}>刷新日报</button></div>
    </header>
    {error && <p className="notice error" role="alert">{error}</p>}
    {(daily?.results_revision ?? 0)>readRevision && <button className="news-new link-button" disabled={busy} onClick={()=>void load()}>日报已更新 · 查看新报</button>}
    {!daily?.valid && <p className="news-state">{daily?.enabled ? "日报范围需要重新确认。" : "开启后，每天生成一份日报。"}<button className="link-button" onClick={configure}>设置日报</button></p>}
    {daily?.paused && <p className="field-note">同步已暂停，已有日报仍可阅读。</p>}
    {busy && <p role="status">正在处理日报，请稍候…</p>}
    {report?.run?.status==="running" && <p role="status">正在生成，完成后刷新查看。</p>}
    {["failed","unknown"].includes(report?.run?.status ?? "") && <p className="news-warning">本次未更新，已有日报保留。重试可能再次计费。</p>}
    {report?.late_messages ? <p className="news-warning">补入 {report.late_messages} 条记录，可重新生成。</p> : null}
    {edition ? <>
      <p className="news-basis">依据 {edition.body.chat_count} 个会话、{edition.body.message_count} 条已同步文字</p>
      {!!edition.body.coverage_warnings.length && <p className="news-warning">{edition.body.coverage_warnings.join(" · ")}</p>}
      {!!edition.body.rejected_count && <p className="news-warning">{edition.body.rejected_count} 条内容未通过核对。</p>}
      {!!topics.length && <section className="news-overview"><h3>今日速览</h3><ul>{edition.body.overview.map(id=>topics.find(t=>t.candidate_id===id)).filter(Boolean).map(t=><li key={t!.candidate_id}>{t!.title}</li>)}</ul></section>}
      {sections.map(section=>{const items=topics.filter(t=>t.kind===section.kind);return items.length ? <section key={section.kind} className="news-section"><h3>{section.title}</h3>
        {items.map(topic=><article key={topic.candidate_id} className="news-topic"><h4>{topic.title}</h4><p>{topic.summary}</p>
          <button className="link-button" onClick={event=>openSource(topic.source_message_ids,topic.title,event.currentTarget)}>查看来源<span> {topic.source_message_ids.length} 条</span></button></article>)}
      </section>:null;})}
      {!topics.length && <p className="news-empty">{edition.body.rejected_count ? "没有通过核对的资讯。" : "该日暂无可整理资讯。"}</p>}
      <p className="news-footnote">根据聊天文字整理，未读取链接正文。</p>
    </> : report && !busy && <p className="news-empty">{daily?.valid ? "该日暂无日报，到时会自动生成。" : "该日还没有保存的日报。"}</p>}
    {daily?.valid && <details className="news-rebuild"><summary>补齐日报</summary><p>重新核对该日已同步文字。</p>
      <button disabled={busy || report?.run?.status==="running"} onClick={()=>void prepare()}>预览发送</button></details>}
    {preview && <section className="daily-consent"><h3>确认发送</h3><p>{preview.day} · {preview.message_count} 条文字</p>
      <dl><dt>接收服务</dt><dd>{preview.base_url}</dd><dt>模型</dt><dd>{preview.model}</dd></dl>
      <p>可能再次计费，原日报保留。</p><p>已自动脱敏，仍可能漏检。</p>
      <details><summary>查看内容</summary>{preview.parts.map(part=><pre key={part.ref}>{part.chat} · {dateTime(part.sent_at)}{"\n"}{part.content}</pre>)}</details>
      <label className="checkbox-row"><input type="checkbox" checked={consent} disabled={busy} onChange={e=>setConsent(e.currentTarget.checked)}/>同意发送本次日报文字</label>
      <div className="actions"><button className="primary" disabled={busy || !consent} onClick={()=>void generate()}>生成日报</button>
        <button disabled={busy} onClick={()=>{setPreview(null);setConsent(false);}}>暂不发送</button></div>
    </section>}
  </div>;
}
