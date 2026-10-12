import type { Message } from "./types";

export type SourceEntry = { id: string; message: Message | null; unavailable: boolean };

export const sourceIds = (ids: readonly string[]) => [...new Set(ids)];

export function chronologicalSources(entries: readonly SourceEntry[]): SourceEntry[] {
  return [...entries].sort((a, b) => {
    const first = a.message ? Date.parse(a.message.sent_at) : NaN;
    const second = b.message ? Date.parse(b.message.sent_at) : NaN;
    if (!Number.isFinite(first)) return Number.isFinite(second) ? 1 : 0;
    if (!Number.isFinite(second)) return -1;
    return first - second;
  });
}

// Only local, explicitly linked originals. One unavailable source does not hide the others.
export async function loadSources(
  ids: readonly string[], account: string,
  fetchMessage: (id: string) => Promise<Message>,
  isCurrent: () => boolean,
): Promise<SourceEntry[]> {
  const unique = sourceIds(ids);
  const entries: SourceEntry[] = [];
  for (let offset = 0; offset < unique.length && isCurrent(); offset += 4) {
    const batch = await Promise.all(unique.slice(offset, offset + 4).map(async id => {
      try {
        const message = await fetchMessage(id);
        if (message.message_id !== id || message.account_id !== account) throw new Error("source_mismatch");
        return { id, message, unavailable: false };
      } catch {
        // Do not echo exception text, or show a different account's original.
        return { id, message: null, unavailable: true };
      }
    }));
    if (!isCurrent()) return [];
    entries.push(...batch);
  }
  return isCurrent() ? chronologicalSources(entries) : [];
}
