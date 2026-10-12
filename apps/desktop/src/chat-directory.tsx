import React, { useEffect, useMemo, useRef, useState } from "react";
import { request, explainError } from "./bridge";
import { chatWindow, matchingChats, type ChatFilters } from "./chat-list";
import { TagPicker } from "./tag-picker";
import { chatName, type Chat, type State, type Tag } from "./types";

type Preview = { preview_token: string; operation: string; selected: number; change: number; unchanged: number; unavailable: number };
type Applied = { changed: number; unchanged: number; unavailable: number; failed: number; failed_ids: string[]; unavailable_ids: string[] };
const operations: Record<string, string> = {sync_on: "开启本地同步", sync_off: "关闭本地同步", analysis_on: "纳入文字分析", analysis_off: "移出文字分析", tags_add: "添加标签", tags_remove: "移除标签"};

export function ChatDirectory({ state, busy, refresh, navigate, onEdit, onRename, onDelete, editor, tagForm, onBusy, resetToken }: {
  state: State | null; busy: boolean; refresh: () => Promise<unknown>; navigate: (action: () => void) => void;
  onEdit: (chat: Chat) => void; onRename: (tag: Tag) => void; onDelete: (tag: Tag) => void;
  editor: React.ReactNode; tagForm: React.ReactNode; onBusy: (value: boolean) => void; resetToken: number;
}) {
  const [tab, setTab] = useState("chats");
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<ChatFilters>({});
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [allSelected, setAllSelected] = useState(false);
  const [operation, setOperation] = useState("tags_add");
  const [tagIds, setTagIds] = useState<string[]>([]);
  const [tagQuery, setTagQuery] = useState("");
  const [tagPage, setTagPage] = useState(0);
  const [notice, setNotice] = useState("");
  const [failed, setFailed] = useState(false);
  const [working, setWorking] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const inFlight = useRef(false);
  const generation = useRef(0);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const disabled = busy || working;
  const chats = state?.chats || [];
  const tags = state?.tags || [];
  const list = useMemo(() => chatWindow(chats, query, page, filters), [chats, query, page, filters]);
  const matches = useMemo(() => matchingChats(chats, query, filters), [chats, query, filters]);
  const tagMatches = tags.filter(tag => tag.name.toLocaleLowerCase().includes(tagQuery.trim().toLocaleLowerCase()));
  const tagPages = Math.max(1, Math.ceil(tagMatches.length/25));
  const currentTagPage = Math.min(tagPage, tagPages-1);
  const tagCounts = useMemo(() => {
    const counts = new Map<string, {total: number; sync: number; analysis: number}>();
    for (const chat of chats) for (const tag of chat.tags) {
      const count = counts.get(tag.tag_id) || {total: 0, sync: 0, analysis: 0};
      count.total++; count.sync += +chat.sync_enabled; count.analysis += +chat.analysis_enabled; counts.set(tag.tag_id, count);
    } return counts;
  }, [chats]);
  useEffect(() => {onBusy(working);}, [working, onBusy]);
  useEffect(() => {
    generation.current++; setPreview(null); setSelected(new Set()); setAllSelected(false); setQuery(""); setFilters({}); setPage(0); setTagIds([]);
  }, [state?.source?.account_id, resetToken]);
  useEffect(() => {if (preview) dialog.current?.showModal(); else {dialog.current?.close(); trigger.current?.focus();}}, [preview]);
  const changeFilter = (next: ChatFilters, nextQuery = query) => {
    if (selected.size) {setFailed(false); setNotice("筛选已变化，已清空之前选择的会话。");}
    setFilters(next); setQuery(nextQuery); setPage(0); setSelected(new Set()); setAllSelected(false); setPreview(null);
  };
  const toggle = (ids: string[], checked: boolean) => {
    setSelected(previous => { const next = new Set(previous); for (const id of ids) checked ? next.add(id) : next.delete(id); return next; });
    setAllSelected(false);
  };
  async function execute(apply: boolean) {
    if (inFlight.current || disabled) return;
    inFlight.current = true; setWorking(true); setFailed(false); setNotice("");
    const current = generation.current;
    try {
      if (apply && preview) {
        const result = await request<Applied>("chat.bulk.apply", {preview_token: preview.preview_token, confirm: true});
        if (current !== generation.current) return;
        setPreview(null); setSelected(new Set([...result.failed_ids, ...result.unavailable_ids])); setAllSelected(false);
        setNotice(`已变更 ${result.changed} 个 · 无需变更 ${result.unchanged} 个 · 不可处理 ${result.unavailable} 个 · 保存失败 ${result.failed} 个。未保存的会话保留选择，可调整后重新预览。`);
        setFailed(result.failed > 0); await refresh();
      } else {
        const result = await request<Preview>("chat.bulk.preview", {chat_ids: [...selected], operation, tag_ids: operation.startsWith("tags_") ? tagIds : []});
        if (current === generation.current) setPreview(result);
      }
    } catch (error) {if (current === generation.current) {setFailed(true); setNotice(explainError(error)); setPreview(null);}}
    finally {inFlight.current = false; setWorking(false);}
  }
  return <section className="directory">
    <div className="segmented" aria-label="会话与标签视图"><button aria-pressed={tab==="chats"} onClick={() => navigate(() => setTab("chats"))}>会话 · {chats.length}</button><button aria-pressed={tab==="tags"} onClick={() => navigate(() => setTab("tags"))}>标签 · {tags.length}</button></div>
    {notice && <p className={failed ? "notice error" : "notice"} role={failed ? "alert" : "status"}>{notice}</p>}
    {tab === "chats" ? <>
      <div className="directory-filters"><input disabled={disabled} type="search" aria-label="搜索会话" placeholder="搜索会话名、显示名或标签" value={query} onChange={event => changeFilter(filters, event.target.value)}/>
        <select disabled={disabled} aria-label="会话类型" value={filters.type || ""} onChange={event => changeFilter({...filters, type: event.target.value})}><option value="">全部类型</option><option value="group">群聊</option><option value="direct">私聊</option></select>
        <select disabled={disabled} aria-label="本地同步筛选" value={filters.sync || ""} onChange={event => changeFilter({...filters, sync: event.target.value})}><option value="">全部同步状态</option><option value="on">已开启同步</option><option value="off">未开启同步</option></select>
        <select disabled={disabled} aria-label="文字分析筛选" value={filters.analysis || ""} onChange={event => changeFilter({...filters, analysis: event.target.value})}><option value="">全部分析状态</option><option value="on">已纳入分析</option><option value="off">未纳入分析</option></select>
        <details className="directory-tag-filter"><summary>标签筛选{filters.tags?.length ? ` · ${filters.tags.length}` : ""}</summary><div><select disabled={disabled} aria-label="标签匹配方式" value={filters.tagMode || "any"} onChange={event => changeFilter({...filters, tagMode: event.target.value as "any" | "all"})}><option value="any">包含任一所选标签</option><option value="all">同时包含所选标签</option></select><TagPicker disabled={disabled} tags={tags} selected={filters.tags || []} label="筛选标签" onChange={ids => changeFilter({...filters, tags: ids})}/></div></details>
        <button disabled={disabled} onClick={() => changeFilter({}, "")}>重置筛选</button>
      </div>
      <div className="selection-summary"><span role="status">匹配 {list.total} 个 · 已选 {selected.size} 个{allSelected ? "（固定的全部筛选结果）" : "（跨页保留）"}</span>
        <button disabled={disabled || !matches.length} onClick={() => {setSelected(new Set(matches.map(chat => chat.chat_id))); setAllSelected(true);}}>选择全部匹配的 {matches.length} 个会话</button>
        <button disabled={disabled || !selected.size} onClick={() => {setSelected(new Set()); setAllSelected(false);}}>清空选择</button></div>
      <div className="directory-table-scroll"><table className="directory-table"><thead><tr><th><input type="checkbox" aria-label="选择当前页会话" disabled={disabled || !list.items.length} checked={!!list.items.length && list.items.every(chat => selected.has(chat.chat_id))} onChange={event => toggle(list.items.map(chat => chat.chat_id), event.target.checked)}/></th><th>会话</th><th>标签</th><th>本地同步</th><th>文字分析</th><th><span className="sr-only">操作</span></th></tr></thead>
        <tbody>{list.items.map(chat => <tr key={chat.chat_id}><td><input type="checkbox" aria-label={`选择 ${chatName(chat)}`} disabled={disabled} checked={selected.has(chat.chat_id)} onChange={event => toggle([chat.chat_id], event.target.checked)}/></td><td><strong title={chatName(chat)}>{chatName(chat)}</strong><small>{chat.chat_type === "group" ? "群聊" : "私聊"} · 已保存 {chat.message_count} 条{chat.display_name ? ` · 原名 ${chat.chat_name}` : ""}</small></td><td><div className="table-tags">{chat.tags.slice(0,2).map(tag => <span key={tag.tag_id} className="tag">{tag.name}</span>)}{chat.tags.length>2 && <button className="link-button" title={chat.tags.map(tag => tag.name).join("、")} onClick={() => onEdit(chat)}>+{chat.tags.length-2}</button>}</div></td><td className={chat.sync_enabled ? "on-text" : "muted"}>{chat.sync_enabled ? "已开启" : "未开启"}</td><td className={chat.analysis_enabled ? "on-text" : "muted"}>{chat.analysis_enabled ? "已纳入" : "未纳入"}</td><td><button disabled={disabled} onClick={() => onEdit(chat)}>配置</button></td></tr>)}</tbody></table></div>
      {!list.total && <p className="empty-state">没有匹配的会话，试试其它筛选条件。</p>}
      <div className="pager" aria-label="会话分页"><span>显示 {list.first}–{list.last} / {list.total} 个 · 第 {list.page+1}/{list.pageCount} 页</span><button disabled={list.page===0} onClick={() => setPage(list.page-1)}>上一页会话</button><button disabled={list.page+1>=list.pageCount} onClick={() => setPage(list.page+1)}>下一页会话</button></div>
      {selected.size>0 && <div className="bulk-toolbar"><strong>批量配置 · {selected.size} 个会话</strong><select aria-label="批量操作" disabled={disabled} value={operation} onChange={event => {setOperation(event.target.value); setPreview(null);}}>{Object.entries(operations).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select>
        {operation.startsWith("tags_") && <details><summary>选择标签 · {tagIds.length}</summary><TagPicker tags={tags} selected={tagIds} onChange={setTagIds} disabled={disabled}/></details>}
        <button className="primary" disabled={disabled || (operation.startsWith("tags_") && !tagIds.length)} onClick={event => {trigger.current=event.currentTarget; void execute(false);}}>{working ? "正在处理…" : "预览影响"}</button><small>配置不代表授权云端发送</small></div>}
      {editor}
    </> : <>
      <div className="directory-filters"><input type="search" aria-label="搜索管理标签" value={tagQuery} placeholder="搜索标签名称" onChange={event => {setTagQuery(event.target.value); setTagPage(0);}}/><span>关联数量仅计当前微信账号</span></div>
      {tagForm}
      <div className="directory-table-scroll"><table className="directory-table tag-directory"><thead><tr><th>标签</th><th>关联会话</th><th>已同步</th><th>已纳入分析</th><th>操作</th></tr></thead><tbody>{tagMatches.slice(currentTagPage*25,(currentTagPage+1)*25).map(tag => {const count=tagCounts.get(tag.tag_id);return <tr key={tag.tag_id}><td><strong title={tag.name}>{tag.name}</strong></td><td>{count?.total || 0}</td><td>{count?.sync || 0}</td><td>{count?.analysis || 0}</td><td><div className="actions"><button disabled={disabled} onClick={() => navigate(() => {changeFilter({tags:[tag.tag_id]}); setTab("chats");})}>查看会话</button><button disabled={disabled} onClick={() => onRename(tag)}>改名</button><button className="link-button danger" disabled={disabled} onClick={() => onDelete(tag)}>删除</button></div></td></tr>;})}</tbody></table></div>
      {!tagMatches.length && <p className="empty-state">没有匹配的标签</p>}
      <div className="pager"><span>{tagMatches.length} 个标签 · 第 {currentTagPage+1}/{tagPages} 页</span><button disabled={currentTagPage===0} onClick={() => setTagPage(currentTagPage-1)}>上一页管理标签</button><button disabled={currentTagPage+1>=tagPages} onClick={() => setTagPage(currentTagPage+1)}>下一页管理标签</button></div>
    </>}
    <dialog ref={dialog} aria-labelledby="bulk-title" onCancel={event => {if (working) event.preventDefault(); else setPreview(null);}}><h2 id="bulk-title">核对批量配置</h2>{preview && <><p>{operations[preview.operation]} · 固定选择 {preview.selected} 个会话</p><div className="impact-counts"><span>将变更 <strong>{preview.change}</strong></span><span>无需变更 <strong>{preview.unchanged}</strong></span><span>不可处理 <strong>{preview.unavailable}</strong></span></div><p>预览有效期 5 分钟。期间会话、标签或服务配置变化，需要重新预览。新出现的会话不会自动加入。</p>{preview.operation === "sync_off" && <p>关闭本地同步也会移出文字分析；历史消息与整理结果保留。</p>}{preview.operation === "analysis_on" && <p>未开启本地同步的会话不可处理，不会自动替你开启。分析范围改变后，持续授权需要另行确认。</p>}<p>本次仅保存本地配置，不同步消息，不授权或调用云端模型。</p><div className="actions"><button autoFocus disabled={working} onClick={() => setPreview(null)}>取消</button><button className="primary" disabled={working || !preview.change} onClick={() => void execute(true)}>{working ? "保存中…" : "确认保存批量配置"}</button></div></>}</dialog>
  </section>;
}
