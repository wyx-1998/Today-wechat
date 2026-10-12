import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { ChatDirectory } from "../../src/chat-directory";
import { UsageDashboard } from "../../src/usage-dashboard";
import { request } from "./settings-bridge";
import type { State } from "../../src/types";
import "../../src/style.css";
function Harness() {
  const [state,setState]=useState<State|null>(null);
  const [tab,setTab]=useState("chats");
  const [busy,setBusy]=useState(false);
  const [notice,setNotice]=useState("");
  const refresh=async()=>setState(await request<State>("state"));
  useEffect(()=>{void refresh();},[]);
  return <main style={{height:"100vh",overflow:"auto",padding:"20px 28px",background:"#f9fafc"}}>
    <div className="section-title"><p>隔离 GUI 检查 · 2100 会话 / 300 标签 / 60 请求 · 不读取真实数据、不发云端请求</p><div className="actions"><button onClick={()=>setTab("chats")}>会话管理界面</button><button onClick={()=>setTab("usage")}>用量界面</button><button onClick={async()=>{await request("harness.change");await refresh();setNotice("隔离配置已变化");}}>模拟外部配置变化</button></div></div>
    <p role="status">{notice}</p>
    {tab==="chats" ? <ChatDirectory state={state} busy={busy} refresh={refresh} navigate={action=>action()} resetToken={0} onBusy={setBusy}
      onEdit={chat=>setNotice(chat.chat_name)} onRename={tag=>setNotice(tag.name)} onDelete={tag=>setNotice(tag.name)} editor={null} tagForm={<p>标签新建与改名沿用主程序的原有表单</p>}/>:<UsageDashboard state={state}/>}
  </main>;
}
createRoot(document.getElementById("root")!).render(<Harness/>);
