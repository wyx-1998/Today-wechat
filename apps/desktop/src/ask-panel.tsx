import React, {useEffect, useRef, useState} from 'react';
import Markdown from 'react-markdown';
import type {BaseEvent} from '@ag-ui/core';
import {request, explainError} from './bridge';
import type {State, Message} from './types';
import {chatName, dateTime, messageRelation} from './types';
import {TauriAgent} from './ask-agent';
import {askStatus, type AskThread, type AskTurn, type AskScope, type AskPreview, type AskStarted} from './ask-types';
import {loadSources, type SourceEntry} from './source-view';
import './ask.css';

const scopeInput = (scope:AskScope) => ({chat_ids:scope.chat_ids,time_start:scope.time_start,time_end:scope.time_end,person:scope.person,include_results:scope.include_results});
const localInput = (value:string) => {const date=new Date(value);return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16);};
const initialScope = (state:State | null):AskScope => ({chat_ids:state?.chats.filter(c=>c.analysis_enabled).map(c=>c.chat_id) || [],time_start:state?.time_start || new Date(Date.now()-7*86400000).toISOString(),time_end:new Date().toISOString(),person:'',include_results:true});

function AskAnswer({text,citations=[],openSource}: {text:string;citations?:AskTurn['citations'];openSource?:(citation:AskTurn['citations'][number],button:HTMLButtonElement)=>void}) {
  const content=text.replace(/\[(M\d+)\]/g,(match,label)=>citations.some(c=>c.label===label)?`[${label}](#ask-${label})`:match);
  return <Markdown skipHtml components={{a:({href,children})=>{
    const citation=citations.find(c=>href===`#ask-${c.label}`);
    return citation && openSource?<button className="ask-inline-citation" aria-label={`查看出处 ${citation.label}`} onClick={e=>openSource(citation,e.currentTarget)}>{children}</button>:<span>{children}</span>;
  },img:()=>null}}>{content}</Markdown>;
}

export function AskPanel({state,configure}: {state:State | null;configure:()=>void}) {
  const account=state?.source?.account_id;
  const [threads,setThreads]=useState<AskThread[]>([]), [thread,setThread]=useState<AskThread | null>(null);
  const [scope,setScope]=useState<AskScope>(()=>initialScope(state));
  const [question,setQuestion]=useState(''), [notice,setNotice]=useState(''), [busy,setBusy]=useState(false);
  const [preview,setPreview]=useState<AskPreview | null>(null), [consent,setConsent]=useState(false), [persistent,setPersistent]=useState(false);
  const [live,setLive]=useState<{question:string;answer:string;status:string} | null>(null), [progress,setProgress]=useState('');
  const [sources,setSources]=useState<SourceEntry[] | null>(null), [loadingSources,setLoadingSources]=useState(false);
  const drawer=useRef<HTMLDialogElement>(null), trigger=useRef<HTMLButtonElement | null>(null);
  const generation=useRef(0), sourceGeneration=useRef(0), currentAccount=useRef(account), run=useRef<AskStarted | null>(null), agent=useRef<TauriAgent | null>(null);
  currentAccount.current=account;
  const scroll=useRef<HTMLDivElement>(null);
  async function list(expected=account) {const result=await request<AskThread[]>('ask.list');if(currentAccount.current===expected)setThreads(result);}
  useEffect(()=>{
    generation.current++;sourceGeneration.current++;agent.current?.abortRun();agent.current=null;run.current=null;
    setThread(null);setLive(null);setPreview(null);setConsent(false);setPersistent(false);setBusy(false);setSources(null);setQuestion('');setNotice('');setScope(initialScope(state));
    if(account)void list().catch(e=>setNotice(explainError(e)));
    return ()=>{generation.current++;sourceGeneration.current++;agent.current?.abortRun();};
  },[account]);
  useEffect(()=>{setPreview(null);setConsent(false);setPersistent(false);if(agent.current)void stop();},[state?.ask_model?.revision]);
  useEffect(()=>{scroll.current?.scrollTo({top:scroll.current.scrollHeight});},[live?.answer,thread?.turns?.length]);
  useEffect(()=>{if(sources!==null)drawer.current?.showModal();else drawer.current?.close();},[sources]);
  const recovering=!!thread?.turns?.some(t=>['waiting','generating'].includes(t.answer_status));
  useEffect(()=>{
    if(!recovering || busy || !thread)return;
    let active=true;
    const timer=setInterval(()=>{void request<AskThread>('ask.detail',{ask_thread_id:thread.ask_thread_id}).then(t=>{if(active)setThread(t);}).catch(e=>{if(active)setNotice(explainError(e));});},1500);
    return ()=>{active=false;clearInterval(timer);};
  },[recovering,busy,thread?.ask_thread_id,account]);
  function clearPreview(){setPreview(null);setConsent(false);setPersistent(false);}
  async function openThread(id:string, preserveNotice=false){
    const version=++generation.current;if(!preserveNotice)setNotice('');clearPreview();setLive(null);
    try{const data=await request<AskThread>('ask.detail',{ask_thread_id:id});if(version!==generation.current)return;setThread(data);const last=data.turns?.at(-1);if(last)setScope(scopeInput(last.query_scope));}catch(e){if(version===generation.current)setNotice(explainError(e));}
  }
  async function send(p:AskPreview,authorize:boolean){
    const version=generation.current, targetAccount=account;
    setBusy(true);setLive({question,answer:'',status:'generating'});setProgress('查找消息');setNotice('');
    const next=new TauriAgent({preview_token:p.preview_token,authorize,persistent,ask_thread_id:thread?.ask_thread_id},ids=>{
      if(version!==generation.current)return;
      run.current=ids;setThread(previous=>previous?.ask_thread_id===ids.ask_thread_id?previous:{ask_thread_id:ids.ask_thread_id,title:question,updated_at:new Date().toISOString()});
    });agent.current=next;
    clearPreview();
    let activeThread=thread?.ask_thread_id;
    try{
      await next.runAgent({}, {onEvent:({event})=>{
        if(version!==generation.current || currentAccount.current!==targetAccount)return;
        const e=event as BaseEvent & {delta?:string;message?:string;threadId?:string;runId?:string;stepName?:string;snapshot?:{status?:string;answer?:string}};
        if(e.type==='RUN_STARTED') {activeThread=e.threadId;run.current={run_id:e.runId!,turn_id:'',ask_thread_id:e.threadId!};}
        if(e.type==='TEXT_MESSAGE_CONTENT')setLive(previous=>previous?{...previous,answer:previous.answer+(e.delta || '')}:previous);
        if(e.type==='STEP_STARTED')setProgress(e.stepName || '');
        if(e.type==='STATE_SNAPSHOT')setLive(previous=>previous?{...previous,status:e.snapshot?.status || previous.status,answer:e.snapshot?.answer ?? previous.answer}:previous);
        if(e.type==='RUN_ERROR')setNotice(e.message || '回答中断；请查看已保存内容。');
      }});
    }catch(e){if(version===generation.current)setNotice(explainError(e));}
    finally{
      if(version===generation.current && currentAccount.current===targetAccount){
        setBusy(false);agent.current=null;run.current=null;setQuestion('');
        if(activeThread)await openThread(activeThread,true);
        await list(targetAccount);
      }
    }
  }
  async function prepare(){
    if(busy || recovering || !question.trim())return;
    setBusy(true);setNotice('');const version=generation.current;
    try{const p=await request<AskPreview>('ask.preview',{question,scope:scopeInput(scope)});if(version!==generation.current)return;if(p.authorized){setBusy(false);await send(p,false);}else{setPreview(p);setConsent(false);setPersistent(false);}}catch(e){if(version===generation.current)setNotice(explainError(e));}finally{if(version===generation.current)setBusy(false);}
  }
  async function stop(){
    const ids=run.current, loaded=thread?.turns?.find(t=>['waiting','generating'].includes(t.answer_status));
    generation.current++;setBusy(false);setLive(previous=>previous?{...previous,status:'stopped'}:previous);setProgress('');
    agent.current?.abortRun();agent.current=null;
    const id=ids?.run_id || loaded?.run_id;
    if(id){try{const t=await request<AskThread>('ask.stop',{run_id:id});setThread(t);setLive(null);}catch(e){setNotice(explainError(e));}}
    run.current=null;
  }
  function closeSources(){sourceGeneration.current++;setSources(null);setLoadingSources(false);trigger.current?.focus();}
  async function openSource(citation:AskTurn['citations'][number],button:HTMLButtonElement){
    trigger.current=button;const version=++sourceGeneration.current, expected=account;
    setSources([]);setLoadingSources(true);
    const entries=await loadSources([citation.message_id],expected || '',id=>request<Message>('message.detail',{message_id:id}),()=>version===sourceGeneration.current && currentAccount.current===expected);
    if(version===sourceGeneration.current){setSources(entries);setLoadingSources(false);}
  }
  const disabled=busy || recovering;
  return <div className="ask-layout">
    <aside className="ask-history" aria-label="问答历史"><button className="ask-new" disabled={disabled} onClick={()=>{generation.current++;setThread(null);setLive(null);setQuestion('');setScope(initialScope(state));clearPreview();setNotice('');}}>＋ 新建对话</button>
      <div className="ask-thread-list">{threads.map(t=><button key={t.ask_thread_id} disabled={disabled} className={thread?.ask_thread_id===t.ask_thread_id?'selected':''} onClick={()=>void openThread(t.ask_thread_id)}><strong>{t.title}</strong><time>{dateTime(t.updated_at)}</time></button>)}{!threads.length && <p className="muted">提问后，对话会保存在这里。</p>}</div>
    </aside>
    <section className="ask-conversation" aria-label="聊天问答">
      <div className="ask-turns" ref={scroll}>
        {!thread?.turns?.length && !live && <div className="ask-empty"><p className="eyebrow">从聊天中找答案</p><h2>想了解哪件事？</h2><p>查看项目讨论、追溯约定，或整理某段时间的进展。每次回答都会显示依据和实际查询范围。</p><div className="ask-examples">{['上周有哪些需要跟进的约定？','这个项目最近讨论了什么？'].map(q=><button disabled={disabled} key={q} onClick={()=>{setQuestion(q);clearPreview();}}>{q}</button>)}</div><p className="field-note">仅查询本地已保存的文字、待办和整理结果。Ask 不修改待办，也不触发微信同步。</p>{!state?.ask_model?.enabled && <button className="primary" onClick={configure}>配置问答服务</button>}</div>}
        {thread?.turns?.map(t=><article className="ask-turn" key={t.turn_id}><p className="ask-question">{t.question}</p><p className="ask-answer-state">{askStatus(t.answer_status,t.error_code)} · {t.coverage.evidence_messages}/{t.coverage.readable_messages} 条可读消息{t.coverage.complete?' · 全部覆盖':' · 相关片段或部分覆盖'}</p><div className="ask-answer"><AskAnswer text={t.conclusion || '尚未生成回答。'} citations={t.citations} openSource={(citation,button)=>void openSource(citation,button)}/></div><div className="ask-citations">{[...new Map(t.citations.map(c=>[c.label,c])).values()].map(c=><button key={c.label} className="link-button" onClick={e=>void openSource(c,e.currentTarget)}>[{c.label}] {c.chat_name} · {c.sender_name || '发送者未知'} · {dateTime(c.sent_at)}</button>)}</div><details className="ask-turn-scope"><summary>本轮查询范围</summary><p>{dateTime(t.query_scope.time_start)} — {dateTime(t.query_scope.time_end)} · {t.query_scope.chat_ids.length} 个聊天{t.query_scope.person?' · 发送者 '+t.query_scope.person:''}</p></details></article>)}
        {live && <article className="ask-turn"><p className="ask-question">{live.question}</p><p className="ask-answer-state" role="status">{askStatus(live.status)}{busy?' · '+progress:''}</p><div className="ask-answer"><AskAnswer text={live.answer || '正在准备回答…'}/></div></article>}
      </div>
      <div className="ask-composer">
        {notice && <p className="notice error" role="alert">{notice}</p>}
        <details className="ask-scope"><summary>查询范围 · {scope.chat_ids.length} 个聊天 · {new Date(scope.time_start).toLocaleDateString('zh-CN')} — {new Date(scope.time_end).toLocaleDateString('zh-CN')}</summary>
          <div className="ask-scope-fields"><label>开始时间<input type="datetime-local" disabled={disabled} value={localInput(scope.time_start)} onChange={e=>{if(e.target.value){setScope({...scope,time_start:new Date(e.target.value).toISOString()});clearPreview();}}}/></label><label>结束时间<input type="datetime-local" disabled={disabled} value={localInput(scope.time_end)} onChange={e=>{if(e.target.value){setScope({...scope,time_end:new Date(e.target.value).toISOString()});clearPreview();}}}/></label><label>人物筛选（可选）<input disabled={disabled} placeholder="发送者昵称或明确账号" value={scope.person} onChange={e=>{setScope({...scope,person:e.target.value});clearPreview();}}/></label></div>
          <button type="button" disabled={disabled} className="link-button" onClick={()=>{setScope({...scope,time_end:new Date().toISOString()});clearPreview();}}>将结束时间更新至现在</button>
          <div className="ask-chat-options">{state?.chats.map(c=><label className="check" key={c.chat_id}><input type="checkbox" checked={scope.chat_ids.includes(c.chat_id)} disabled={disabled} onChange={e=>{setScope({...scope,chat_ids:e.target.checked?[...scope.chat_ids,c.chat_id]:scope.chat_ids.filter(id=>id!==c.chat_id)});clearPreview();}}/>{chatName(c)}</label>)}</div>
          <label className="check"><input type="checkbox" checked={scope.include_results} disabled={disabled} onChange={e=>{setScope({...scope,include_results:e.target.checked});clearPreview();}}/>包含本轮原文可支持的历史待办与整理结果</label>
        </details>
        {preview && <div className="ask-confirm"><strong>确认发送范围</strong><p>{preview.service.base_url} · {preview.service.model}</p><p>{preview.warning}</p><p>{preview.coverage.evidence_messages} 条原文，当前范围共有 {preview.coverage.total_messages} 条消息。{preview.coverage.truncated?'单次范围较大，只能提供部分回答。':''}</p><details><summary>查看脱敏证据</summary><div className="ask-evidence">{preview.evidence.map(e=><article key={e.label}><strong>[{e.label}] {e.chat_name} · {e.sender_name}</strong><time>{dateTime(e.sent_at)}</time><p>{e.content}</p></article>)}</div></details><label className="check"><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)}/>同意发送本次问题、必要追问历史和上述范围内的脱敏资料</label><label className="check"><input type="checkbox" checked={persistent} onChange={e=>setPersistent(e.target.checked)}/>持续允许向此问答服务发送同一范围；更换服务或扩大范围时重新确认</label><div className="actions"><button disabled={!consent || busy} className="primary" onClick={()=>void send(preview,true)}>确认并发送问题</button><button disabled={busy} onClick={clearPreview}>取消</button></div></div>}
        <form className="ask-input" onSubmit={e=>{e.preventDefault();void prepare();}}><label className="sr-only" htmlFor="ask-question">输入问题</label><textarea id="ask-question" maxLength={4000} placeholder="输入问题，可继续追问…" value={question} disabled={disabled} onChange={e=>{setQuestion(e.target.value);clearPreview();}}/>{disabled?<button key="stop" type="button" className="danger" onClick={e=>{e.preventDefault();void stop();}}>停止回答</button>:<button key="send" type="submit" className="primary" disabled={!account || !state?.ask_model?.enabled || !question.trim() || !scope.chat_ids.length}>发送问题</button>}</form>
        <div className="ask-composer-note"><span>原文引用可追溯；结论仍需结合原文判断。</span><button className="link-button" onClick={()=>void request('ask.revoke').then(()=>{clearPreview();if(disabled)void stop();setNotice('问答持续授权已撤销。');}).catch(e=>setNotice(explainError(e)))}>撤销问答授权</button></div>
      </div>
    </section>
    <dialog className="result-source task-drawer" ref={drawer} aria-label="问答原文" onCancel={e=>{e.preventDefault();closeSources();}}><header className="source-drawer-header"><div className="section-title"><h3>原文来源</h3><button autoFocus onClick={closeSources} aria-label="关闭原文">×</button></div><p className="field-note">只读 · 原文未脱敏</p></header><div className="source-drawer-body">{loadingSources?<p role="status">正在读取原文…</p>:sources?.map(entry=><article className="source-entry" key={entry.id}>{entry.message?<><div className="source-entry-meta"><strong>{messageRelation(entry.message)===true?'本人':entry.message.sender_name || '发送者未知'}</strong><time>{dateTime(entry.message.sent_at)}</time></div><p className="source-chat-name">{entry.message.chat_name}</p><p className="original">{entry.message.content || '原文不可用'}</p></>:<p>该来源当前不可用，未补造证据。</p>}</article>)}</div></dialog>
  </div>;
}
