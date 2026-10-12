import React, {useEffect, useState} from "react";
import {createRoot} from "react-dom/client";
import {ModelPanel} from "../../src/model-panel";
import type {State} from "../../src/types";
import "../../src/style.css";
async function api<T>(command: string, params = {}): Promise<T> {
  const response = await fetch('/api', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({command,params})});
  const value = await response.json(); if(value.error) throw value.error; return value.result;
}
Object.assign(window, {__TAURI_INTERNALS__:{invoke:async(_: string, args:{command:string;params:Record<string,unknown>})=>api(args.command,args.params)}});
function Harness() {
  const [state,setState] = useState<State|null>(null);
  const refresh = async()=>setState(await api<State>('state'));
  useEffect(()=>{void refresh();},[]);
  return <div style={{height:'100vh', display:'flex', flexDirection:'column'}}><header style={{padding:'12px 28px',borderBottom:'1px solid #e0e4ea',display:'flex',gap:16,alignItems:'center'}}><strong>设置 · 模型与用量</strong><span style={{fontSize:12,color:'#626c7a'}}>隔离生成数据 · 模拟模型 · 不读取微信 · 不改应用配置</span><button onClick={async()=>{await api('harness.add'); await refresh();}}>模拟新消息到来</button></header><ModelPanel state={state} refresh={refresh} onDirty={()=>{}} resetToken={0}/></div>;
}
createRoot(document.getElementById('root')!).render(<Harness/>);
