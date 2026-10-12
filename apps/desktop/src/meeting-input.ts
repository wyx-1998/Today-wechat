export const MEETING_MAX_BYTES = 512 * 1024;
export const MEETING_MAX_CHARS = 120_000;
export function meetingFileError(name: string, size: number): string | null {
  if (!/\.(txt|md|markdown)$/i.test(name)) return "请选择 TXT 或 Markdown 文件。";
  if (size > MEETING_MAX_BYTES) return "文件超过 512 KB，请分篇导入。";
  return null;
}
export function decodeMeetingFile(bytes: ArrayBuffer): string {
  let content: string;
  try { content = new TextDecoder("utf-8", {fatal: true}).decode(bytes); }
  catch { throw new Error("请将文件保存为 UTF-8 编码。 "); }
  if (!content.trim() || content.includes("\0")) throw new Error("文件没有可读文字，请检查内容。");
  if (content.length > MEETING_MAX_CHARS) throw new Error("单篇超过 12 万字，请分篇导入。");
  return content;
}
export type MeetingCandidate = {candidate_id: string; kind: "task" | "key_point"; title: string;
  responsible?: string | null; due_at?: string | null; due_text?: string | null; date_warning?: string | null;
  summary?: string; reason?: string; evidence: {segment_id: string; quote: string}[]};
export type MeetingNote = {meeting_id: string; title: string; meeting_date: string | null; file_name: string | null;
  status: "draft" | "generating" | "ready" | "published" | "failed"; revision: number; created_at: string;
  error_code: string | null; original?: string; duplicate?: boolean; source_message_id?: string | null;
  result?: {candidates: MeetingCandidate[]; rejected_count: number; published_ids?: string[]; task_count?: number; key_point_count?: number} | null};
export type MeetingPreview = {meeting_id: string; revision: number; preview_token: string; title: string;
  meeting_date: string | null; base_url: string; model: string; segments: {segment_id: string; content: string}[]};
export const meetingStatus: Record<MeetingNote["status"], string> = {
  draft: "未生成", generating: "生成中", ready: "待加入", published: "已加入", failed: "未生成",
};
