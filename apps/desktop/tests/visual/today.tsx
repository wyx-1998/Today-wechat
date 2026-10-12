import React from "react";
import { createRoot } from "react-dom/client";
import { Results } from "../../src/results";
import type { State } from "../../src/types";
import "../../src/style.css";
const state = {source:{account_id:"isolated"},results_revision:1} as State;
createRoot(document.getElementById("root")!).render(<main style={{display:"flex",flexDirection:"column",height:"100vh",padding:"0 28px"}}>
  <header style={{padding:"12px 0",borderBottom:"1px solid #ddd",fontSize:12}}>隔离界面检查 · 仅生成输入，无真实账号／聊天／网络请求，不随应用打包</header>
  <Results page="today" state={state} configure={()=>{}}/>
</main>);
