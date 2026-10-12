import React, { useEffect, useRef, useState } from "react";
import { request, explainError } from "./bridge";
import type { State, TextModel } from "./types";
import { AutomaticPanel, ProcessingHistory } from "./automatic-panel";
import { ManualPanel } from "./manual-panel";
import { UsageDashboard } from "./usage-dashboard";
import { AskConfig } from "./ask-config";
import { DailyControls } from "./news-reader";

export function ModelPanel({ state, refresh, onDirty, resetToken }: {
  state: State | null; refresh: () => Promise<unknown>; onDirty: (value: boolean) => void; resetToken: number;
}) {
  const model = state?.text_model;
  const [askDirty, setAskDirty] = useState(false);
  const [draft, setDraft] = useState<{base_url: string; model: string; api_key: string} | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [failed, setFailed] = useState(false);
  const [deleteRevision, setDeleteRevision] = useState<number | null>(null);
  const deleteDialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (deleteRevision !== null) deleteDialog.current?.showModal();
    else deleteDialog.current?.close();
  }, [deleteRevision]);
  useEffect(() => { setDeleteRevision(null); }, [model?.revision, state?.source?.account_id, resetToken]);
  useEffect(() => { onDirty(!!draft || askDirty); }, [draft, askDirty, onDirty]);
  useEffect(() => { setDraft(null); }, [resetToken]);
  async function action<T>(command: string, params: Record<string, unknown>, success: string): Promise<T | undefined> {
    if (busy) return;
    setBusy(true); setNotice(""); setFailed(false);
    try {
      const result = await request<T>(command, params);
      await refresh(); setNotice(success); return result;
    } catch (error) { setFailed(true); setNotice(explainError(error)); }
    finally { setBusy(false); }
  }
  return <div className="content-scroll model-panel">
    <UsageDashboard state={state}/>
    <div className="section-title"><h2>整理服务</h2><span>Gemini 原生协议</span></div>
    <p className="muted">模型密钥在本机明文保存，页面不回显。请勿分享应用数据库或备份，其中包含密钥。保存配置不代表授权发送。</p>
    {notice && <p className={failed ? "notice error" : "notice"} role={failed ? "alert" : "status"}>{notice}</p>}
    {model ? <dl className="model-summary">
      <dt>服务地址</dt><dd>{model.base_url}</dd><dt>模型</dt><dd>{model.model}</dd>
      <dt>访问密钥</dt><dd>已保存 · 不回显</dd>
      <dt>状态</dt><dd>{model.enabled ? "已启用" : "已停用"}</dd>
    </dl> : <p>尚未配置文本模型。本地同步和原文查看仍可使用。</p>}
    {!draft ? <div className="actions">
      <button disabled={busy} onClick={() => setDraft({base_url: model?.base_url || "", model: model?.model || "", api_key: ""})}>{model ? "编辑文本配置" : "配置文本模型"}</button>
      {model && <button disabled={busy} onClick={() => void action("model.enable", {enabled: !model.enabled}, model.enabled ? "已停用文本模型；已发送的请求无法撤回。" : "已启用文本模型；整理前仍需确认发送范围。")}>{model.enabled ? "停用文本模型" : "启用文本模型"}</button>}
      {model && <button className="link-button danger" disabled={busy} onClick={() => setDeleteRevision(model.revision)}>删除文本配置</button>}
    </div> : <form onSubmit={event => { event.preventDefault(); void action<TextModel>("model.save", {...draft, api_key: draft.api_key || undefined, protocol: "gemini", enabled: true}, "文本配置已本地保存；未发送聊天内容。").then(result => { if (result) setDraft(null); }); }}>
      <label>文本服务地址<input type="url" required value={draft.base_url} placeholder="https://服务地址" onInput={event => setDraft({...draft, base_url: event.currentTarget.value})}/></label>
      <label>文本模型名<input required value={draft.model} placeholder="gemini-3.8-flash" onInput={event => setDraft({...draft, model: event.currentTarget.value})}/></label>
      <label>文本访问密钥<input type="password" autoComplete="off" spellCheck={false} value={draft.api_key} placeholder={model?.storage_mode === "sqlite_plaintext" ? "留空保留已保存密钥" : "请输入密钥，不会回显"} onInput={event => setDraft({...draft, api_key: event.currentTarget.value})}/></label>
      <p className="muted">保存后无需系统解锁。能读取本机数据库或备份的程序和他人，也可能取得密钥。</p>
      <div className="actions"><button type="submit" className="primary" disabled={busy || !draft.base_url || !draft.model || (model?.storage_mode !== "sqlite_plaintext" && !draft.api_key)}>{busy ? "保存中…" : "保存文本配置"}</button><button type="button" disabled={busy} onClick={() => setDraft(null)}>放弃文本修改</button></div>
    </form>}
    <dialog ref={deleteDialog} onCancel={() => setDeleteRevision(null)} aria-labelledby="delete-model-title">
      <h2 id="delete-model-title">删除文本配置？</h2>
      <p>将删除本机保存的服务地址、模型名和访问密钥，并结束文字持续授权。新的整理将停止；已发送的请求无法撤回，其结果不再发布。</p>
      <p>聊天、待办和历史用量保留。此操作无法撤销，重新配置后需要重新授权。</p>
      <div className="actions"><button autoFocus disabled={busy} onClick={() => setDeleteRevision(null)}>取消</button>
        <button className="danger" disabled={busy || deleteRevision === null} onClick={() => {
          const revision = deleteRevision; setDeleteRevision(null);
          if (revision !== null) void action("model.delete", {revision, confirm: true}, "文本配置已删除；聊天、待办和历史用量保留。");
        }}>确认删除配置</button></div>
    </dialog>
    <AskConfig state={state} refresh={refresh} onDirty={setAskDirty} resetToken={resetToken}/>
    {state?.source && <>
      <div className="processing-grid">
        <AutomaticPanel state={state} refresh={refresh} disabled={busy || !!draft}/>
        <ManualPanel state={state} refresh={refresh} disabled={busy || !!draft} resetToken={resetToken}/>
      </div>
      <DailyControls state={state} refresh={refresh} disabled={busy || !!draft}/>
      <ProcessingHistory state={state} refresh={refresh} disabled={busy || !!draft}/>
    </>}
  </div>;
}
