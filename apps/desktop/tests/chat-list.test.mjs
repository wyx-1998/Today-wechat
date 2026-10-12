import test from "node:test";
import assert from "node:assert/strict";
import { chatWindow, matchingChats, CHAT_PAGE_SIZE } from "../src/chat-list.ts";

const make = (i, extra = {}) => ({ account_id: "synthetic", chat_id: `synthetic-${i}`,
  chat_name: `合成会话 ${i}`, display_name: null, chat_type: "group",
  sync_enabled: false, analysis_enabled: false, tags: [], message_count: 0, ...extra });

test("large list is paged without changing order or selection", () => {
  const chats = Array.from({ length: 2216 }, (_, i) => make(i));
  const before = JSON.stringify(chats);
  const first = chatWindow(chats, "", 0);
  assert.equal(first.total, 2216);
  assert.equal(first.items.length, CHAT_PAGE_SIZE);
  assert.equal(first.pageCount, 89);
  assert.equal(chatWindow(chats, "", 1).items[0], chats[25]);
  const last = chatWindow(chats, "", 1000);
  assert.equal(last.page, 88);
  assert.equal(last.items.length, 16);
  assert.equal(last.last, 2216);
  assert.equal(JSON.stringify(chats), before);
});
test("name search is case insensitive, supports multiple terms and original names", () => {
  const match = make(1, { chat_name: "Agent 合成小队", display_name: "测试项目" });
  assert.deepEqual(chatWindow([make(0), match], "  AGENT 小队  ", 20).items, [match]);
  assert.equal(chatWindow([match], "测试项目", 0).total, 1);
  assert.equal(chatWindow([match], "agent 不存在", 0).total, 0);
});
test("labels and identifiers can locate a chat but never enable it", () => {
  const chat = make(2, { tags: [{ tag_id: "synthetic-tag", name: "合成重点" }] });
  assert.equal(chatWindow([chat], "合成重点", 0).items[0], chat);
  assert.equal(chatWindow([chat], "synthetic-2", 0).total, 1);
  assert.equal(chat.sync_enabled, false);
});
test("empty results and invalid page requests have stable boundaries", () => {
  for (const page of [-2, NaN, Infinity]) {
    const result = chatWindow([], "不存在", page);
    assert.equal(result.page, 0); assert.equal(result.pageCount, 1);
    assert.equal(result.first, 0); assert.equal(result.last, 0);
    assert.deepEqual(result.items, []);
  }
  const chat = make(0);
  assert.equal(chatWindow([chat], " \t\n ", 0).items[0], chat);
});

test("combined filters and any/all label modes never alter data", () => {
  const a={tag_id:"a",name:"工作"},b={tag_id:"b",name:"重点"};
  const chats=[make(0,{sync_enabled:true,analysis_enabled:true,tags:[a,b]}),make(1,{sync_enabled:true,tags:[a]}),make(2,{chat_type:"direct",tags:[b]})];
  assert.equal(matchingChats(chats,"",{tags:["a","b"],tagMode:"any"}).length,3);
  assert.equal(matchingChats(chats,"",{tags:["a","b"],tagMode:"all"}).length,1);
  assert.equal(matchingChats(chats,"",{type:"direct",sync:"off",analysis:"off"})[0],chats[2]);
  assert.equal(chatWindow(chats,"",0,{sync:"on",analysis:"on"}).total,1);
  assert.equal(chats[2].analysis_enabled,false);
});
test("frozen matching identifiers do not include newly arriving chats", () => {
  const chats=Array.from({length:2100},(_,i)=>make(i));
  const ids=new Set(matchingChats(chats,"").map(chat=>chat.chat_id));
  chats.push(make(2100));
  assert.equal(ids.size,2100);assert.ok(!ids.has(chats.at(-1).chat_id));
  assert.equal(chatWindow(chats,"",1).items.length,25);
});
