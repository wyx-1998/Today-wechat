export type Tag = { tag_id: string; name: string };
export type Chat = {
  account_id: string;
  chat_id: string;
  chat_name: string;
  display_name: string | null;
  chat_type: string;
  sync_enabled: boolean;
  analysis_enabled: boolean;
  tags: Tag[];
  message_count: number;
};
export type Report = {
  run_id: string;
  outcome: string;
  started_at: string;
  finished_at: string;
  time_start: string;
  time_end: string;
  inserted: number;
  duration_ms: number;
  errors: { chat_id: string; code: string; message: string }[];
};
export type State = {
  daily_news?: {enabled: boolean; valid: boolean; reason: string | null; revision: number; first_day: string | null;
    latest_day: string | null; results_revision: number; yesterday: string; timezone: string; paused: boolean; schedule: string};
  source: { mode: "demo" | "local" | "encrypted"; root: string; account_id: string; keys_file?: string } | null;
  paused: boolean;
  busy: boolean;
  time_start: string | null;
  chats: Chat[];
  tags: Tag[];
  message_count: number;
  last_sync: Report | null;
  revision: number;
  text_model: TextModel | null;
  ask_model?: TextModel | null;
  ask_usage?: Record<"today" | "month", State["model_usage"]["today"] & {services?: {config_revision: number;requests: number;known_input_tokens: number | null;known_output_tokens: number | null;known_total_tokens: number | null;unknown_input_requests: number;unknown_output_requests: number;unknown_total_requests: number}[]}>;
  automatic_text: AutomaticTextState;
  results_revision: number;
  model_usage: Record<"today" | "month", { requests: number; success: number; failed_or_unknown: number;
    failed?: number; unknown?: number; sending?: number;
    known_total_tokens: number | null; known_input_tokens: number | null; known_output_tokens: number | null;
    unknown_usage_requests: number; unknown_input_requests: number; unknown_output_requests: number }>;
};
export type TextScope = { account_id: string; source_hash: string; chat_ids: string[];
  time_start: string; message_types: string[]; protocol: string; base_url: string; model: string };
export type AutomaticTextState = {
  grant: {status: string; scope: TextScope; created_at: string; reason: string | null} | null;
  pending_messages: number; running_batches?: number; queued_batches?: number; quiet_seconds: number; max_wait_seconds: number;
  batches: {batch_id: string; status: string; created_at: string; finished_at: string | null; error_code: string | null;
    chat_name: string; message_count: number; retryable: boolean; current_scope?: boolean}[];
};
export type TextModel = {
  storage_mode: "sqlite_plaintext" | "keychain_encrypted";
  base_url: string; model: string; protocol: string; enabled: boolean;
  revision: number; key_saved: boolean; test_status: string; test_error: string | null;
};
export type TaskChange = {change_id: string; kind: "details" | "completed" | "cancelled";
  proposed: {title?: string; owner_type?: string; due_at?: string | null; status?: string};
  source_message_ids: string[]; created_at: string};
export type Task = {
  task_id: string; title: string; owner_type: string; due_at: string | null;
  status: string; source_message_ids: string[]; revision: number;
  created_at?: string;
  changes?: TaskChange[];
};
export type Attention = {
  source_kind?: "meeting"; meeting_date?: string | null; meeting_title?: string;
  attention_id: string; kind: "information" | "key_point";
  title: string; summary: string; attention_reason: string;
  source_message_ids: string[]; related_chat_ids: string[];
  source_chats: {chat_id: string; chat_name: string | null}[];
  observed_at: string; event_at: string | null; evidence_state: string;
};
export type TaskPage = { items: Task[]; attention_items?: Attention[]; synthetic: boolean; results_revision: number; last_analysis: {status: string; finished_at: string | null; error_code: string | null; rejected_candidate_count?: number; rejected_change_count?: number; rejected_attention_count?: number} | null };
export type Message = {
  source_kind?: "meeting"; meeting_date?: string | null; meeting_title?: string; imported_at?: string;
  message_id: string;
  account_id: string;
  chat_id: string;
  stream_id: string;
  chat_name: string;
  sender_id: string | null;
  sender_name: string | null;
  is_self: boolean | null;
  effective_is_self?: boolean | null;
  sent_at: string;
  message_type: string;
  content: string | null;
  content_preview: string;
  content_state: string;
  local_id: string;
  context?: Message[];
};
export type MessagePage = {
  items: Message[];
  total: number;
  offset: number;
  has_more: boolean;
  saved_total?: number;
  time_start?: string | null;
  time_end?: string | null;
  senders?: {sender_id: string | null; sender_name: string | null; effective_is_self: boolean | number | null; message_count: number}[];
};
export const chatName = (chat: Chat) => chat.display_name || chat.chat_name;
// Explicit null means currently unknown; do not fall back to a stale flag.
export const messageRelation = (message: Pick<Message, "is_self" | "effective_is_self">) =>
  message.effective_is_self === undefined ? message.is_self : message.effective_is_self;
export const dateTime = (value: string | null | undefined) =>
  value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "尚未同步";
export const typeName = (type: string) =>
  ({
    text: "文字",
    image: "图片",
    audio: "语音",
    file: "文件",
    video: "视频",
    system: "系统消息",
    other: "其他",
  })[type] || "未知类型";
export const contentState = (state: string) =>
  ({
    text_available: "原文可读",
    type_only: "仅记录类型 · 尚未理解",
    read_failed: "正文读取失败",
  })[state] || "内容状态未知";
export const reportText = (report: Report) =>
  report.outcome === "no_selection"
    ? "请先选择需要同步的会话。"
    : report.outcome === "cancelled"
      ? `同步已停止，已保存 ${report.inserted} 条；成功进度保留。`
      : report.outcome === "partial"
        ? `部分完成 · 本轮新增 ${report.inserted} 条，${report.errors.length} 个会话失败。`
        : `同步完成 · 本轮新增 ${report.inserted} 条。`;
