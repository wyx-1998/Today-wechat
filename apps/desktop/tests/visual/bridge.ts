// Isolated UI harness only. This file is never imported by the product build.
import type { Attention, Message, Task, TaskPage } from "../../src/types";
const now = new Date(2026,9,3,10).toISOString();
const knowledge: Attention[] = Array.from({length:24}, (_,i) => ({attention_id:`a${i}`,
  kind:i<12?"information":"key_point", title:i<12?`评审方式调整 · ${i+1}`:`资料交接要求 · ${i-11}`,
  summary:"群内通知本周评审改为线上，原有参会人员与资料准备要求不变。".repeat(i===0?8:2),
  attention_reason:"参会方式已经变化，需按消息中的安排准备。",
  source_message_ids:i===0?Array.from({length:8},(_,j)=>`m${j}`):[`m${i}`], related_chat_ids:["isolated-chat"],
  source_chats:[{chat_id:"isolated-chat",chat_name:"隔离输入会话"}], observed_at:now, event_at:null, evidence_state:"supported"}));
let tasks: Task[] = Array.from({length:12},(_,i)=>({task_id:`t${i}`,title:`准备评审资料 · ${i+1}`,
  owner_type:"self",status:"pending",due_at:"2026-10-03",created_at:now,revision:1,source_message_ids:[`m${i}`]}));
const data = (): TaskPage => ({items:structuredClone(tasks),attention_items:structuredClone(knowledge),
  synthetic:true,results_revision:1,last_analysis:{status:"success",finished_at:now,error_code:null,rejected_attention_count:1}});
export async function request<T>(command:string, params:Record<string,unknown> = {}):Promise<T> {
  if (command==="today.list" || command==="tasks.list") return data() as T;
  if (command==="task.update") {
    const task=tasks.find(task=>task.task_id===params.task_id);
    if(!task || task.revision!==params.revision) throw new Error("版本已变化");
    task.status=params.status as string;task.revision++;
    return data() as T;
  }
  if(command==="message.detail") {
    const index=Number(String(params.message_id).slice(1));
    return {message_id:params.message_id,account_id:"isolated",chat_id:"isolated-chat",stream_id:"isolated",
      chat_name:"隔离输入会话",sender_id:"isolated-self",sender_name:"隔离参与人",is_self:true,effective_is_self:true,
      sent_at:new Date(new Date(now).getTime()+index*60000).toISOString(),message_type:"text",
      content:`本周评审改为线上，资料需准备完整。消息 ${index+1}`,content_state:"text_available",local_id:String(index),content_preview:"",context:[]} as Message as T;
  }
  throw new Error("该检查不允许真实请求");
}
export const explainError = (reason:unknown) => reason instanceof Error?reason.message:"读取失败";
