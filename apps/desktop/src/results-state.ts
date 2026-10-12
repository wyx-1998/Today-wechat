import type { TaskPage } from "./types";

export function applyTaskUpdate(previous: TaskPage | null, next: TaskPage, taskId: string): TaskPage | null {
  if (!previous) return previous;
  const changed = next.items.find(task => task.task_id === taskId);
  if (!changed) return previous;
  // A user action updates its target without importing newly published rows.
  return {...previous, items: previous.items.map(task => task.task_id === taskId ? changed : task)};
}

export function hasNewResults(visible: number | undefined, current: number | undefined): boolean {
  return visible !== undefined && current !== undefined && current > visible;
}
