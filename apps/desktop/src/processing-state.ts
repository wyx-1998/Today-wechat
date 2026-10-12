import type { State } from "./types";

export function automaticStatus(state: State) {
  const automatic = state.automatic_text;
  const grant = automatic?.grant;
  if (!state.text_model?.enabled) return {label: "模型未启用", detail: "启用上方的文本模型后，再确认自动整理范围。", tone: "neutral"};
  if (grant?.status === "invalid") return {label: "需要重新确认", detail: "会话、时间或服务已变更。确认新的发送范围后继续整理。", tone: "warning"};
  if (!grant || grant.status !== "active") return {label: "未开启", detail: "开启前会展示发送范围和接收服务，确认后才发送文字。", tone: "neutral"};
  if (state.paused) return {label: "同步已暂停", detail: "自动整理随同步暂停，已有消息和结果保留。", tone: "neutral"};
  if ((automatic.running_batches ?? automatic.batches.filter(b => b.current_scope && b.status === "running").length) > 0)
    return {label: "正在整理", detail: "正在处理已固定的消息批次。新消息留给后续批次。", tone: "active"};
  if ((automatic.queued_batches ?? 0) > 0) return {label: "等待处理", detail: "已有消息批次排队，系统会依次处理，无需重复提交。", tone: "active"};
  if (automatic.pending_messages > 0) return {label: "等待聊天稳定", detail: "已收到新消息，聊天停止更新后会自动整理。", tone: "active"};
  return {label: "等待新消息", detail: "自动整理已开启，收到范围内的新文字后会按批次处理。", tone: "active"};
}

export const batchStatusLabel = (status: string, error: string | null) =>
  error === "analysis_scope_changed" || error === "chat_scope_changed" || error === "time_range_changed"
    ? "范围已变更 · 本批次已停止"
    : ({success: "已发布", queued: "等待处理", running: "正在整理", unknown: "结果待核实", failed: "未发布", cancelled: "已停止"} as Record<string, string>)[status] || "状态待核实";
