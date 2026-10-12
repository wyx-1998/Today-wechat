import React, {useEffect, useRef, useState} from "react";
import {request, explainError} from "./bridge";
import {Message, MessagePage, State, chatName, messageRelation, dateTime, typeName, contentState} from "./types";
import {emptyMessageFilter, messageQuery, messageDay, messageTime} from "./message-query";
import "./message-browser.css";

type SavedChat = {chat_id: string; chat_name: string; chat_type: string; message_count: number; content_preview: string | null};
type ChatPage = {items: SavedChat[]; total: number; offset: number; has_more: boolean};
type ReadState<T> = {key: string; data: T | null; loading: boolean; error: string};
function useRead<T>(command: string, params: Record<string, unknown>, version: string, enabled = true) {
  const [retry, setRetry] = useState(0);
  const key = JSON.stringify([command, params, version, retry, enabled]);
  const [value, setValue] = useState<ReadState<T>>({key:"",data:null,loading:true,error:""});
  useEffect(() => {
    let active = true;
    if(!enabled) {setValue({key,data:null,loading:false,error:""}); return;}
    setValue({key,data:null,loading:true,error:""});
    void request<T>(command, params).then(data => {if(active) setValue({key,data,loading:false,error:""});})
      .catch(error => {if(active) setValue({key,data:null,loading:false,error:explainError(error)});});
    return () => {active = false;};
  }, [key]);
  return {...(value.key === key ? value : {key,data:null,loading:true,error:""}), retry:() => setRetry(n => n + 1)};
}
const speaker = (message: Message) => messageRelation(message) === true ? "我" : message.sender_name || "身份未知";
const bodyText = (message: Message) => message.content_preview || `〔${typeName(message.message_type)}〕${contentState(message.content_state)}`;

export function MessageBrowser({state, onChooseChats}: {state: State | null; onChooseChats: () => void}) {
  const account = state?.source?.account_id;
  if(!account) return <div className="empty-state"><h3>尚无已保存消息</h3><p>先连接本地来源，再选择需要同步的会话。</p><button onClick={onChooseChats}>选择会话</button></div>;
  return <SavedMessages key={account} state={state!} onChooseChats={onChooseChats}/>;
}
function SavedMessages({state, onChooseChats}: {state: State; onChooseChats: () => void}) {
  const [query,setQuery] = useState("");
  const [chatOffset,setChatOffset] = useState(0);
  const [selected,setSelected] = useState<SavedChat | null>(null);
  const [draft,setDraft] = useState(emptyMessageFilter);
  const [filter,setFilter] = useState(emptyMessageFilter);
  const [validation,setValidation] = useState("");
  const [offset,setOffset] = useState(0);
  const [message,setMessage] = useState<Message | null>(null);
  const [openedId,setOpenedId] = useState<string | null>(null);
  const [loadingDetail,setLoadingDetail] = useState(false);
  const [detailError,setDetailError] = useState("");
  const [copyStatus,setCopyStatus] = useState("");
  const generation = useRef(0);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const drawer = useRef<HTMLDialogElement>(null);
  const original = useRef<HTMLTextAreaElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const [facets,setFacets] = useState<{chat: string; data: MessagePage} | null>(null);
  const version = JSON.stringify([state.source?.account_id,state.message_count,state.revision]);
  const chats = useRead<ChatPage>("messages.chats", {query,limit:25,offset:chatOffset}, version);
  const selectedId = selected?.chat_id || null;
  const list = useRead<MessagePage>("messages.list", {chat_id:selectedId,limit:50,offset,order:"asc",...messageQuery(filter)}, version, !!selectedId);
  const metadata = list.data || (facets?.chat === selectedId ? facets.data : null);
  useEffect(() => {if(!selected && chats.data?.items.length) setSelected(chats.data.items[0]);}, [chats.data, selected]);
  useEffect(() => {if(list.data && selectedId) setFacets({chat:selectedId,data:list.data});}, [list.data,selectedId]);
  useEffect(() => {if(scroll.current) scroll.current.scrollTop = 0;}, [selectedId, offset, filter]);
  useEffect(() => {
    if(chats.data && chatOffset && chatOffset >= chats.data.total) setChatOffset(Math.max(0,Math.floor((chats.data.total - 1)/25)*25));
  }, [chats.data,chatOffset]);
  useEffect(() => {
    if(list.data && offset && offset >= list.data.total) setOffset(Math.max(0,Math.floor((list.data.total - 1)/50)*50));
  }, [list.data,offset]);
  useEffect(() => {
    if(openedId) {drawer.current?.showModal(); closeButton.current?.focus();}
    else drawer.current?.close();
  }, [openedId]);
  useEffect(() => () => {generation.current++;}, []);
  function closeSource() {
    generation.current++; setOpenedId(null); setMessage(null); setLoadingDetail(false); setDetailError(""); setCopyStatus("");
    drawer.current?.close(); trigger.current?.focus();
  }
  function choose(chat: SavedChat) {
    closeSource(); setSelected(chat); setOffset(0); setDraft(emptyMessageFilter()); setFilter(emptyMessageFilter()); setValidation(""); setFacets(null);
  }
  function reset() {closeSource(); setDraft(emptyMessageFilter()); setFilter(emptyMessageFilter()); setOffset(0); setValidation("");}
  async function loadDetail(id: string) {
    const token = ++generation.current;
    setOpenedId(id); setLoadingDetail(true); setMessage(null); setDetailError(""); setCopyStatus("");
    try {
      const result = await request<Message>("message.detail",{message_id:id});
      if(token === generation.current) {
        if(result.account_id !== state.source?.account_id || result.chat_id !== selectedId) throw new Error("消息来源已变化，请重新选择。");
        setMessage(result);
      }
    } catch(error) {if(token === generation.current) setDetailError(explainError(error));}
    finally {if(token === generation.current) setLoadingDetail(false);}
  }
  async function copy() {
    if(message?.content == null) return;
    try {await navigator.clipboard.writeText(message.content); setCopyStatus("原文已复制");}
    catch {original.current?.focus(); original.current?.select(); setCopyStatus("请按 ⌘C（Windows：Ctrl+C）复制已选中的原文。");}
  }
  const items = list.data?.items || [];
  const isFiltered = !!(filter.from || filter.to || filter.sender);
  const currentChat = state.chats.find(chat => chat.chat_id === selectedId);
  const title = currentChat ? chatName(currentChat) : selected?.chat_name || "选择会话";
  const senderNames = new Map<string,number>();
  for(const sender of metadata?.senders || []) {const name = sender.sender_name || "身份未知"; senderNames.set(name,(senderNames.get(name) || 0)+1);}
  return <div className="saved-message-browser">
    <aside className="saved-conversations" aria-label="已保存会话">
      <div className="saved-conversation-top"><h2>已保存会话 <span>{chats.data?.total ?? "—"}</span></h2><label>查找会话<input type="search" value={query} maxLength={200} placeholder="搜索会话名称" onChange={e=>{setQuery(e.target.value);setChatOffset(0);}}/></label></div>
      <div className="saved-conversation-scroll" aria-busy={chats.loading}>
        {chats.loading && <p className="browser-status" role="status">正在查找会话…</p>}
        {chats.error && <div className="browser-status" role="alert"><p>{chats.error}</p><button onClick={chats.retry}>重新查询</button></div>}
        {!chats.loading && !chats.error && !chats.data?.items.length && <p className="browser-status">{query ? "没有匹配的已保存会话，请换一个名称查找。" : "尚无已保存会话。"}</p>}
        {chats.data?.items.map(chat=><button key={chat.chat_id} className="saved-conversation" aria-current={chat.chat_id===selectedId ? "true":undefined} title={chat.chat_name} onClick={()=>choose(chat)}><span className="message-avatar" aria-hidden="true">{Array.from(chat.chat_name)[0]}</span><span className="saved-conversation-info"><span><strong>{chat.chat_name}</strong><small>{chat.message_count}</small></span><p>{chat.content_preview || "暂无可读文字预览"}</p></span></button>)}
      </div>
      <div className="saved-conversation-footer"><span>{chats.data ? `${chats.data.total ? chatOffset+1 : 0}–${chatOffset+chats.data.items.length} / ${chats.data.total} 个会话` : "会话列表"}</span><div><button aria-label="上一页会话" disabled={chats.loading || !chatOffset} onClick={()=>setChatOffset(n=>Math.max(0,n-25))}>‹</button><button aria-label="下一页会话" disabled={chats.loading || !chats.data?.has_more} onClick={()=>setChatOffset(n=>n+25)}>›</button></div></div>
    </aside>
    <section className="saved-reading" aria-label="会话消息">
      <header className="saved-chat-header"><span className="message-avatar" aria-hidden="true">{Array.from(title)[0]}</span><div><h2>{title}</h2><p>{metadata?.time_start && metadata?.time_end ? `${messageDay(metadata.time_start)} — ${messageDay(metadata.time_end)} · 已保存范围` : "仅展示已保存的消息"}</p></div><span className="message-readonly">只读</span></header>
      <form className="saved-filters" onSubmit={e=>{e.preventDefault();try{messageQuery(draft);closeSource();setFilter({...draft});setOffset(0);setValidation("");list.retry();}catch(error){setValidation(explainError(error));}}}>
        <label>时间范围（本地时间）<span className="message-dates"><input type="date" aria-label="开始日期" aria-invalid={!!validation} value={draft.from} onInput={e=>setDraft({...draft,from:e.currentTarget.value})} onChange={e=>setDraft({...draft,from:e.target.value})}/><span>—</span><input type="date" aria-label="结束日期" value={draft.to} onInput={e=>setDraft({...draft,to:e.currentTarget.value})} onChange={e=>setDraft({...draft,to:e.target.value})}/></span></label>
        <label>发送人<select aria-label="发送人" value={draft.sender} onChange={e=>setDraft({...draft,sender:e.target.value})}><option value="">全部发送人</option>{metadata?.senders?.map(sender=>{const name = sender.effective_is_self === true || sender.effective_is_self === 1 ? "我" : sender.sender_name || "身份未知";return <option key={sender.sender_id ?? "unknown:"} value={sender.sender_id ? "id:"+sender.sender_id : "unknown:"}>{name}{sender.sender_id && (senderNames.get(sender.sender_name || "身份未知") || 0)>1 ? " · "+sender.sender_id : ""}</option>;})}</select></label>
        <button className="primary" disabled={!selected || list.loading}>查找</button><button type="button" className="message-quiet" onClick={reset}>重置</button>
      </form>
      {validation && <p className="browser-error" role="alert">{validation}</p>}
      <div className="saved-result-summary"><span role="status">{list.loading ? "正在查询消息…" : list.error ? "消息查询失败" : `${isFiltered ? "找到" : "已保存"} ${list.data?.total ?? 0} 条消息`}</span><span>{isFiltered ? "筛选结果，不代表完整聊天记录" : "按时间排列 · 点击消息查看原文"}</span></div>
      <div className="saved-timeline" ref={scroll} aria-busy={list.loading}>
        {list.error && <div className="empty-state" role="alert"><h3>消息暂时无法读取</h3><p>{list.error}</p><button onClick={list.retry}>重新查询</button></div>}
        {!list.loading && !list.error && !items.length && <div className="empty-state"><h3>{isFiltered ? "没有匹配的消息" : "尚无已保存消息"}</h3><p>{isFiltered ? "试试扩大时间范围，或选择全部发送人。" : "先开启会话同步，再同步所选范围。"}</p><button onClick={isFiltered ? reset : onChooseChats}>{isFiltered ? "清除筛选" : "选择会话"}</button></div>}
        {items.map((item,index)=><React.Fragment key={item.message_id}>
          {(!index || messageDay(item.sent_at)!==messageDay(items[index-1].sent_at)) && <div className="saved-day-divider">{messageDay(item.sent_at)}</div>}
          <button className={"saved-message-row "+(openedId===item.message_id?"selected":"")} aria-label={`查看 ${speaker(item)} ${dateTime(item.sent_at)} 的消息原文`} onClick={e=>{trigger.current=e.currentTarget;void loadDetail(item.message_id);}}><span className="message-avatar" aria-hidden="true">{Array.from(speaker(item))[0]}</span><span className="saved-message-body"><span className="saved-message-meta"><strong>{speaker(item)}</strong>{messageRelation(item)===true && <span className="message-self">本人</span>}<time dateTime={item.sent_at}>{messageTime(item.sent_at)}</time></span><span className={"saved-message-content "+(item.content===null?"unavailable":"")}>{bodyText(item)}</span>{item.content!==null && (Array.from(item.content).length>100 || item.content.split("\n").length>3) && <span className="message-expand">展开原文 · {Array.from(item.content).length} 字</span>}</span></button>
        </React.Fragment>)}
      </div>
      <footer className="pagination"><span>{list.loading || list.error ? "—" : list.data?.total ? `${offset+1}–${offset+items.length} / ${list.data.total} 条` : "0 条"}</span><div className="actions"><button disabled={list.loading || !!list.error || !offset} onClick={()=>{closeSource();setOffset(n=>Math.max(0,n-50));}}>上一页</button><button disabled={list.loading || !!list.error || !list.data?.has_more} onClick={()=>{closeSource();setOffset(n=>n+50);}}>下一页</button></div></footer>
    </section>
    <dialog className="saved-source-drawer" ref={drawer} onCancel={e=>{e.preventDefault();closeSource();}} aria-labelledby="saved-source-title">
      <header className="saved-drawer-header"><div><span>原文与上下文</span><h2 id="saved-source-title">消息原文</h2></div><button ref={closeButton} aria-label="关闭原文详情" onClick={closeSource}>×</button></header>
      <div className="saved-drawer-body">
        {loadingDetail && <p role="status">正在打开所选消息原文…</p>}
        {detailError && <div role="alert"><p>{detailError}</p><button onClick={()=>openedId && void loadDetail(openedId)}>重新读取原文</button></div>}
        {message && <><p className="saved-source-meta">{message.chat_name}<br/>{speaker(message)} · {dateTime(message.sent_at)}{messageRelation(message)===null && <><br/>发送者本人关系尚未确认</>}</p><div className="saved-original-heading"><h3>完整原文</h3><button disabled={message.content===null} onClick={()=>void copy()}>复制原文</button></div>
          {message.content!==null ? <textarea className="original" ref={original} aria-label="完整消息原文（只读）" readOnly rows={6} value={message.content}/> : <p className="saved-unavailable">{message.content_state === "type_only" ? `当前仅记录${typeName(message.message_type)}类型，未取得或理解内容。` : "正文暂不可读，不用摘要或其他消息替代原文。"}</p>}
          <p className="saved-copy-status" role="status">{copyStatus}</p><h3>邻近上下文</h3><p className="saved-context-note">不受当前时间、发送人筛选限制；仅展示已保存的消息。</p>
          {message.context?.length ? message.context.map(item=><article className="context-row" key={item.message_id}><div className="saved-message-meta"><strong>{speaker(item)}</strong><time>{dateTime(item.sent_at)}</time></div><p>{item.content ?? `〔${typeName(item.message_type)}〕${contentState(item.content_state)}`}</p></article>) : <p className="muted">当前保存范围内没有其他邻近消息。</p>}
          <details className="advanced"><summary>来源标识</summary><dl><dt>消息标识</dt><dd>{message.message_id}</dd><dt>账号标识</dt><dd>{message.account_id}</dd><dt>会话标识</dt><dd>{message.chat_id}</dd><dt>分库标识</dt><dd>{message.stream_id}</dd><dt>发送人标识</dt><dd>{message.sender_id || "未知"}</dd><dt>本地消息编号</dt><dd>{message.local_id}</dd><dt>内容状态</dt><dd>{contentState(message.content_state)}</dd></dl></details></>}
      </div>
    </dialog>
  </div>;
}
