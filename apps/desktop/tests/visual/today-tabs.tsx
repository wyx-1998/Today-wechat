import React from "react";
import { createRoot } from "react-dom/client";
import { Results } from "../../src/results";
import type { Attention, Message, State, Task, TaskPage } from "../../src/types";
import "../../src/style.css";

// This check is not a production entry. All requests stay in memory; there is
// no backend, private source, network client, real configuration or timer.
const items: Task[] = Array.from({length: 12}, (_, i) => ({
  task_id: `isolated-task-${i}`, title: `行动 ${i + 1} · 确认资料并安排下一步`, owner_type: "unknown", due_at: null,
  status: "pending", source_message_ids: [`evidence-${i}`], revision: 1,
}));
const attention_items: Attention[] = ["information", "key_point"].flatMap((kind, group) =>
  Array.from({length: group ? 14 : 18}, (_, i) => ({attention_id: `${kind}-${i}`, kind: kind as Attention["kind"],
    title: `${group ? "要点" : "资讯"} ${i + 1} · ${group ? "资料确认规则与讨论结论" : "近期行业动态与项目消息"}`,
    summary: "卡片摘要只展示当前分类。" + (i === 0 ? "详情保留全部内容。".repeat(180) : "可打开详情及原文。"),
    attention_reason: "与当前讨论有关，供了解和核对。", source_message_ids: [`evidence-${i}`],
    related_chat_ids: ["isolated-chat"], source_chats: [{chat_id: "isolated-chat", chat_name: "资料讨论"}],
    observed_at: new Date().toISOString(), event_at: null, evidence_state: "available",
  })));
let page: TaskPage = {items, attention_items, synthetic: true, results_revision: 1, last_analysis: null};
const state = {source: {account_id: "isolated"}, results_revision: 1} as State;
Object.assign(window, {__TAURI_INTERNALS__: {invoke: async (_: string, {command, params}: {command: string; params: Record<string, unknown>}) => {
  if (command === "today.list") return structuredClone(page);
  if (command === "message.detail") return {message_id: params.message_id, account_id: "isolated", chat_id: "isolated-chat",
    chat_name: "资料讨论", sender_name: "生成发送人", is_self: false, sent_at: new Date().toISOString(), content_state: "text_available",
    content: "只用于本地界面检查的原文。".repeat(220)} as Message;
  if (command === "task.update") {
    page = {...page, items: page.items.map(task => task.task_id === params.task_id ? {...task, status: String(params.status), revision: task.revision + 1} : task)};
    return structuredClone(page);
  }
  throw new Error("Unexpected isolated request: " + command);
}}});
createRoot(document.getElementById("root")!).render(<main style={{display: "flex", flexDirection: "column", height: "100vh", padding: "0 28px"}}>
  <header style={{padding: "12px 0", borderBottom: "1px solid #ddd", fontSize: 12}}>隔离 Tab 检查 · 生成数据／无真实来源／无模型请求／不随应用打包</header>
  <Results page="today" state={state} configure={() => {}}/>
</main>);
