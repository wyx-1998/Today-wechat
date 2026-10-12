import React,{useEffect,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {AskPanel} from '../../src/ask-panel';
import {AskConfig} from '../../src/ask-config';
import type {State} from '../../src/types';
import '../../src/style.css';
const callbacks=new Map<number,(data:unknown)=>void>();let counter=0;
async function api<T>(command:string,params={}) {const data=await fetch('/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command,params})}).then(r=>r.json());if(data.error)throw data.error;return data.result as T;}
Object.assign(window,{__TAURI_INTERNALS__:{transformCallback:(callback:(data:unknown)=>void)=>{const id=++counter;callbacks.set(id,callback);return id;},unregisterCallback:(id:number)=>callbacks.delete(id),invoke:async(cmd:string,args:Record<string,any>)=>{
  if(cmd!=='ask_stream')return api(args.command,args.params);
  const ids=await api<{run_id:string}>('ask.start',args.params);const callbackId=args.onEvent.id;let sequence=0;
  const timer=setInterval(async()=>{const packets=await api<{sequence:number;event:{type:string}}[]>('harness.events',{run_id:ids.run_id,after:sequence});for(const packet of packets){sequence=packet.sequence;callbacks.get(callbackId)?.({message:packet,index:sequence-1});if(['RUN_FINISHED','RUN_ERROR'].includes(packet.event.type))clearInterval(timer);}},150);
  return ids;
}}});
function Harness(){const[state,setState]=useState<State|null>(null),[settings,setSettings]=useState(false);const refresh=async()=>setState(await api<State>('state'));useEffect(()=>{void refresh();},[]);
return <div style={{height:'100vh',display:'flex',flexDirection:'column'}}><header style={{padding:'12px 24px',borderBottom:'1px solid #e0e4ea',display:'flex',justifyContent:'space-between'}}><strong>问我 · 隔离生成数据</strong><button onClick={()=>setSettings(!settings)}>{settings?'返回问答':'服务设置'}</button></header>{settings?<div className="content-scroll" style={{padding:24}}><AskConfig state={state} refresh={refresh} onDirty={()=>{}} resetToken={0}/></div>:<AskPanel state={state} configure={()=>setSettings(true)}/>}</div>}
createRoot(document.getElementById('root')!).render(<Harness/>);
