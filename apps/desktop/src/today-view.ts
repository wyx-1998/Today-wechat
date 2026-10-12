import type { Attention, Task } from "./types.ts";
import { dueDay, localDay } from "./task-view.ts";

export const todayTabs = [
  {id: "actions", title: "需要行动"},
  {id: "information", title: "资讯日报"},
  {id: "key_point", title: "关键要点"},
] as const;
export type TodayTab = typeof todayTabs[number]["id"];

export function todayTabForKey(current: TodayTab, key: string): TodayTab | null {
  const index = todayTabs.findIndex(tab => tab.id === current);
  if (key === "Home") return todayTabs[0].id;
  if (key === "End") return todayTabs[todayTabs.length - 1].id;
  if (key !== "ArrowRight" && key !== "ArrowLeft") return null;
  const next = (index + (key === "ArrowRight" ? 1 : -1) + todayTabs.length) % todayTabs.length;
  return todayTabs[next].id;
}

// Today highlights action and knowledge; Tasks remains the full state ledger.
// Do not silently truncate candidates or turn someone else's promise into my work.
export function todayActions(items: Task[], now = new Date()): Task[] {
  const today = localDay(now), end = new Date(now);
  end.setDate(end.getDate() + 7);
  return items.filter(task => {
    if (["completed", "ignored", "cancelled"].includes(task.status)) return false;
    if (task.changes?.length || ["unknown", "shared"].includes(task.owner_type)) return true;
    if (task.owner_type !== "self") return false;
    const deadline = dueDay(task.due_at);
    const discovered = task.created_at ? new Date(task.created_at) : null;
    return (deadline !== null && deadline <= localDay(end))
      || (discovered !== null && Number.isFinite(discovered.getTime()) && localDay(discovered) === today);
  });
}
export function attentionGroups(items: Attention[]) {
  return [
    {kind: "information" as const, title: todayTabs[1].title, items: items.filter(item => item.kind === "information")},
    {kind: "key_point" as const, title: "关键要点", items: items.filter(item => item.kind === "key_point")},
  ];
}
export function actionReason(task: Task, now = new Date()): string {
  if (task.changes?.length) return "变化待确认";
  if (["unknown", "shared"].includes(task.owner_type)) return "归属待确认";
  const day = dueDay(task.due_at), today = localDay(now);
  if (day && day < today) return "已逾期";
  if (day === today) return "今天到期";
  return "待处理";
}
