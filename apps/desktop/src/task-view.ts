import type { Task } from "./types";

export type TaskView = "self" | "ownership" | "waiting" | "completed" | "ignored" | "cancelled";
export type DeadlineFilter = "all" | "overdue" | "today" | "week" | "undated";
export type TaskSort = "original" | "deadline" | "title";
export const taskViews: Record<TaskView, string> = {
  self: "我要做", ownership: "待确认归属", waiting: "我在等别人",
  completed: "已完成", ignored: "已忽略", cancelled: "已取消",
};
export function isActiveTask(task: Task): boolean {
  return !["completed", "ignored", "cancelled"].includes(task.status);
}
export function canClaimSelf(task: Task): boolean {
  return ["unknown", "shared"].includes(task.owner_type)
    && ["needs_confirmation", "pending", "todo", "in_progress"].includes(task.status);
}
export function taskStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    pending: "待处理", needs_confirmation: "待确认", todo: "待处理", in_progress: "进行中",
    completed: "已完成", ignored: "已忽略", cancelled: "已取消", deleted: "已删除",
  };
  return labels[status] || "状态未确认";
}
export function belongsToView(task: Task, view: TaskView): boolean {
  if (["completed", "ignored", "cancelled"].includes(view)) return task.status === view;
  if (!isActiveTask(task)) return false;
  if (view === "self") return task.owner_type === "self";
  if (view === "waiting") return task.owner_type === "other";
  return ["unknown", "shared"].includes(task.owner_type);
}
// Date-only deadlines remain calendar dates, not UTC midnight instants.
export function dueDay(value: string | null): string | null {
  if (!value) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const parsed = new Date(value + "T12:00:00Z");
    return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value ? value : null;
  }
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) ? localDay(parsed) : null;
}
export function localDay(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
export function selectTasks(items: Task[], options: {
  view: TaskView; search: string; deadline: DeadlineFilter; sort: TaskSort;
}, now = new Date()): Task[] {
  const today = localDay(now);
  const end = new Date(now); end.setDate(end.getDate() + 7);
  const query = options.search.trim().toLocaleLowerCase();
  const selected = items.filter(task => {
    if (!belongsToView(task, options.view) || !task.title.toLocaleLowerCase().includes(query)) return false;
    const day = dueDay(task.due_at);
    switch (options.deadline) {
      case "overdue": return day !== null && day < today;
      case "today": return day === today;
      case "week": return day !== null && day >= today && day <= localDay(end);
      case "undated": return !task.due_at;
      default: return true;
    }
  });
  if (options.sort === "deadline") selected.sort((a, b) => (dueDay(a.due_at) || "9999").localeCompare(dueDay(b.due_at) || "9999"));
  if (options.sort === "title") selected.sort((a, b) => a.title.localeCompare(b.title, "zh-CN"));
  return selected;
}
