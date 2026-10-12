import React, {useEffect,useState} from "react";
import {createRoot} from "react-dom/client";
import {MessageBrowser} from "../../src/message-browser";
import {request} from "./settings-bridge";
import type {State} from "../../src/types";
import "../../src/style.css";
function Harness() {
  const [state,setState]=useState<State|null>(null);
  const refresh=async()=>setState(await request<State>("state"));
  useEffect(()=>{void refresh();},[]);
  return <main style={{height:"100vh",display:"flex",flexDirection:"column",overflow:"hidden"}}>
    <div className="section-title" style={{padding:"12px 20px",flex:"none"}}><p>隔离 GUI 检查 · 2100 会话 / 80 已保存会话 · 不读取私人数据，不发云端请求</p><div className="actions"><button onClick={()=>void request("harness.delay")}>切换延迟响应</button><button onClick={()=>void request("harness.fail")}>下次查询失败</button><button onClick={async()=>{await request("harness.account");await refresh();}}>切换隔离账号</button></div></div>
    {state && <MessageBrowser state={state} onChooseChats={()=>{}}/>}
  </main>;
}
createRoot(document.getElementById("root")!).render(<Harness/>);
