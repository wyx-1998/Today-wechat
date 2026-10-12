import type { Chat } from "./types";

export const CHAT_PAGE_SIZE = 25;

/** Filtering changes presentation only, never synchronization permissions. */
export type ChatFilters = { type?: string; sync?: string; analysis?: string; tags?: string[]; tagMode?: "any" | "all" };
export function matchingChats(chats: Chat[], query: string, filters: ChatFilters = {}) {
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return chats.filter((chat) => {
    const fields = [chat.chat_name, chat.display_name || "", chat.chat_id,
      ...chat.tags.map((tag) => tag.name)].map((value) => value.toLocaleLowerCase());
    const tags = new Set(chat.tags.map(tag => tag.tag_id));
    return terms.every((term) => fields.some((value) => value.includes(term)))
      && (!filters.type || chat.chat_type === filters.type)
      && (!filters.sync || chat.sync_enabled === (filters.sync === "on"))
      && (!filters.analysis || chat.analysis_enabled === (filters.analysis === "on"))
      && (!filters.tags?.length || (filters.tagMode === "all"
        ? filters.tags.every(id => tags.has(id)) : filters.tags.some(id => tags.has(id))));
  });
}
export function chatWindow(chats: Chat[], query: string, requestedPage: number, filters: ChatFilters = {}) {
  const matches = matchingChats(chats, query, filters);
  const pageCount = Math.max(1, Math.ceil(matches.length / CHAT_PAGE_SIZE));
  const page = Math.min(pageCount - 1, Math.max(0,
    Number.isFinite(requestedPage) ? Math.floor(requestedPage) : 0));
  return {
    items: matches.slice(page * CHAT_PAGE_SIZE, (page + 1) * CHAT_PAGE_SIZE),
    total: matches.length,
    page,
    pageCount,
    first: matches.length ? page * CHAT_PAGE_SIZE + 1 : 0,
    last: Math.min(matches.length, (page + 1) * CHAT_PAGE_SIZE),
  };
}
