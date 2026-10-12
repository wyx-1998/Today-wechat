import React, { useState } from "react";
import type { Tag } from "./types";

export function TagPicker({ tags, selected, onChange, disabled = false, label = "选择标签" }: {
  tags: Tag[]; selected: string[]; onChange: (ids: string[]) => void; disabled?: boolean; label?: string;
}) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const matches = tags.filter(tag => tag.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const pages = Math.max(1, Math.ceil(matches.length / 25));
  const current = Math.min(page, pages - 1);
  const toggle = (id: string) => onChange(selected.includes(id) ? selected.filter(value => value !== id) : [...selected, id]);
  return <fieldset className="tag-picker" disabled={disabled}><legend>{label}</legend>
    <div className="selected-tags">{selected.map(id => <button type="button" key={id} className="tag" aria-label={`移除所选标签 ${tags.find(tag => tag.tag_id === id)?.name || "已失效"}`} onClick={() => toggle(id)}>{tags.find(tag => tag.tag_id === id)?.name || "已失效"} ×</button>)}</div>
    <input type="search" aria-label={`${label}：搜索`} placeholder="搜索标签" value={query} onChange={event => {setQuery(event.target.value); setPage(0);}}/>
    <div className="tag-picker-options">{matches.slice(current*25, (current+1)*25).map(tag => <label className="check-label" key={tag.tag_id}><input type="checkbox" checked={selected.includes(tag.tag_id)} onChange={() => toggle(tag.tag_id)}/><span>{tag.name}</span></label>)}</div>
    {!matches.length && <p className="muted">没有匹配的标签</p>}
    <div className="pager"><span>{matches.length} 个匹配 · {current+1}/{pages} 页</span><button type="button" disabled={current===0} onClick={() => setPage(current-1)}>上一页标签</button><button type="button" disabled={current+1>=pages} onClick={() => setPage(current+1)}>下一页标签</button></div>
  </fieldset>;
}
