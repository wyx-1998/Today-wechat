import React, {useEffect, useState} from 'react';
import {request, explainError} from './bridge';
import type {State, TextModel} from './types';

export function AskConfig({state, refresh, onDirty, resetToken}: {state: State | null; refresh: () => Promise<unknown>; onDirty: (dirty: boolean) => void; resetToken: number}) {
  const model = state?.ask_model;
  const [draft,setDraft] = useState<{protocol:string;base_url:string;model:string;api_key:string} | null>(null);
  const [busy,setBusy] = useState(false), [notice,setNotice] = useState(''), [checkConsent,setCheckConsent] = useState(false), [deleting,setDeleting] = useState(false);
  useEffect(() => {onDirty(!!draft);}, [draft,onDirty]);
  useEffect(() => {setDraft(null);setCheckConsent(false);setDeleting(false);}, [resetToken]);
  useEffect(() => {setCheckConsent(false);setDeleting(false);}, [model?.revision]);
  async function act(command:string,params:Record<string,unknown>,message:string) {
    setBusy(true);setNotice('');
    try {const value = await request<TextModel>(command,params);await refresh();setNotice(message);return value;} catch(e) {setNotice(explainError(e));} finally {setBusy(false);}
  }
  const usage = state?.ask_usage?.today;
  return <section className="ask-config" aria-labelledby="ask-config-title">
    <div className="section-title"><h2 id="ask-config-title">问答服务</h2><span>独立配置与授权</span></div>
    <p className="muted">支持 OpenAI 兼容和 Gemini 原生接口。保存后，到“问我”选择聊天和时间并确认发送范围。</p>
    {notice && <p className="notice" role="status">{notice}</p>}
    {model ? <dl className="model-summary"><dt>接入方式</dt><dd>{model.protocol === 'openai' ? 'OpenAI 兼容' : 'Gemini 原生'}</dd><dt>服务地址</dt><dd>{model.base_url}</dd><dt>模型</dt><dd>{model.model}</dd><dt>访问密钥</dt><dd>已保存 · 不回显</dd><dt>状态</dt><dd>{model.enabled ? '已启用' : '已停用'} · 配置版本 {model.revision}</dd><dt>连接检查</dt><dd>{model.test_status === 'passed' ? '已通过' : model.test_status === 'failed' ? '未通过 · '+model.test_error : '尚未检查'}</dd></dl> : <p>尚未配置问答服务。</p>}
    {usage && <div className="field-note"><p>今日问答请求 {usage.requests} 次。用量按服务配置版本分别统计，有缺失时不代表完整用量。</p>{usage.services?.map(group=><p key={group.config_revision}>版本 {group.config_revision} · {group.requests} 次 · 已知输入 {group.known_input_tokens ?? '未知'} / 输出 {group.known_output_tokens ?? '未知'} / 总量 {group.known_total_tokens ?? '未知'} Token · 输入未知 {group.unknown_input_requests} 次、输出未知 {group.unknown_output_requests} 次、总量未知 {group.unknown_total_requests} 次</p>)}</div>}
    {!draft ? <div className="actions"><button disabled={busy} onClick={() => setDraft({protocol:model?.protocol || 'openai',base_url:model?.base_url || '',model:model?.model || '',api_key:''})}>{model ? '编辑问答配置' : '配置问答服务'}</button>{model && <><button disabled={busy} onClick={() => void act('ask.model.enable',{enabled:!model.enabled},model.enabled?'问答服务已停用。':'问答服务已启用，请重新确认发送范围。')}>{model.enabled?'停用问答':'启用问答'}</button><button className="link-button danger" disabled={busy} onClick={() => setDeleting(true)}>删除问答配置</button></>}</div> : <form onSubmit={e => {e.preventDefault();void act('ask.model.save',{...draft,api_key:draft.api_key || undefined,enabled:true},'问答配置已本地保存；未发送聊天。').then(result => {if(result)setDraft(null);});}}>
      <label>问答接入方式<select value={draft.protocol} onChange={e=>setDraft({...draft,protocol:e.target.value})}><option value="openai">OpenAI 兼容</option><option value="gemini">Gemini 原生</option></select></label>
      <label>问答服务地址<input required type="url" placeholder={draft.protocol==='openai'?'https://服务地址/v1':'https://服务地址/v1beta'} value={draft.base_url} onChange={e=>setDraft({...draft,base_url:e.target.value})}/></label>
      <label>问答模型名<input required value={draft.model} onChange={e=>setDraft({...draft,model:e.target.value})}/></label>
      <label>问答访问密钥<input type="password" autoComplete="off" spellCheck={false} placeholder={model?'留空保留密钥':'请输入密钥，不回显'} value={draft.api_key} onChange={e=>setDraft({...draft,api_key:e.target.value})}/></label>
      <p className="field-note">密钥在本机 SQLite 明文保存。请勿分享包含密钥的数据库或备份。</p>
      <div className="actions"><button className="primary" disabled={busy || !draft.base_url || !draft.model || (!model && !draft.api_key)}>保存问答配置</button><button type="button" disabled={busy} onClick={()=>setDraft(null)}>放弃修改</button></div>
    </form>}
    {deleting && model && <div className="ask-confirm" role="group" aria-label="删除问答配置确认"><p>删除问答服务地址、模型和密钥，并结束问答持续授权。聊天、待办和问答历史保留。</p><div className="actions"><button disabled={busy} onClick={()=>setDeleting(false)}>取消</button><button className="danger" disabled={busy} onClick={()=>void act('ask.model.delete',{revision:model.revision,confirm:true},'问答配置已删除。').then(()=>setDeleting(false))}>确认删除问答配置</button></div></div>}
    {model && !draft && <details className="ask-connection"><summary>检查连接</summary><label className="check"><input type="checkbox" checked={checkConsent} disabled={busy} onChange={e=>setCheckConsent(e.target.checked)}/>同意发送一条连接检查文字；可能产生费用，不含聊天内容。</label><button disabled={busy || !checkConsent || !model.enabled} onClick={()=>void act('ask.model.test',{authorize:true},'问答服务连接检查通过。').then(()=>setCheckConsent(false))}>检查问答连接</button></details>}
  </section>;
}
