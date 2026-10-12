import type { TextModel } from './types';
export type AskScope = {chat_ids: string[]; time_start: string; time_end: string; person: string; include_results: boolean; account_id?: string; source_hash?: string};
export type Coverage = {total_messages: number; readable_messages: number; evidence_messages: number; matched_messages: number; complete: boolean; exhaustive: boolean; truncated: boolean; result_count: number};
export type Citation = {message_id: string; label: string; paragraph_index: number; chat_name: string; sender_name: string | null; sent_at: string};
export type AskThread = {ask_thread_id: string; title: string; updated_at: string; turns?: AskTurn[]};
export type AskTurn = {turn_id: string; run_id: string; question: string; conclusion: string; answer_status: string; error_code: string | null; query_scope: AskScope; coverage: Coverage; citations: Citation[]; run_status: string};
export type AskPreview = {preview_token: string; authorized: boolean; service: TextModel; scope: AskScope; coverage: Coverage; warning: string; evidence: {label: string; chat_name: string; sender_name: string; sent_at: string; content: string}[]};
export type AskStarted = {run_id: string; turn_id: string; ask_thread_id: string};
export const askStatus = (status: string, code?: string | null) => code === 'ask_stopped' || status === 'stopped' ? '已停止 · 回答未完成' : ({waiting:'等待回答',generating:'正在回答 · 草稿',answered:'回答完成',partial:'部分回答 · 需核对',no_evidence:'依据不足',failed:'回答失败',interrupted:'回答中断'})[status] || '回答中断';
// Duplicate packets are ignored; gaps require recovery from saved state, never a paid replay.
export class AskSequence {
  sequence = 0;
  runId = '';
  accept(runId: string, sequence: number) {
    if (this.runId && runId !== this.runId) throw new Error('ask_run_mismatch');
    if (!Number.isInteger(sequence) || sequence < 1) throw new Error('ask_event_invalid');
    if (sequence <= this.sequence) return false;
    if (sequence !== this.sequence + 1) throw new Error('ask_event_gap');
    this.runId = runId; this.sequence = sequence; return true;
  }
}
