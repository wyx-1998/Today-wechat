import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
const read=name=>readFileSync(new URL("../src/"+name,import.meta.url),"utf8");
test("daily news replaces only information panel, leaving action and key points",()=>{
  const results=read("results.tsx");
  assert.ok(results.includes('todayTab === "information" && <NewsReader'));
  assert.ok(results.includes('groups.filter(group => group.kind === "key_point")'));
  assert.ok(results.includes('tab.id !== "information"'));
  assert.ok(results.includes('today-action-grid'));
});
test("reading and refreshing never request model generation",()=>{
  const source=read("news-reader.tsx");
  const load=source.slice(source.indexOf("async function load"),source.indexOf("async function prepare"));
  assert.match(load,/daily.detail/);assert.doesNotMatch(load,/daily.run|daily.enable|daily.preview_run/);
  assert.match(source,/readRevision/);assert.match(source,/查看新报/);
});
test("continuous enable and manual rebuild have separate confirmations",()=>{
  const source=read("news-reader.tsx");
  assert.match(source,/同意按此范围生成日报/);assert.match(source,/同意发送本次日报文字/);
  assert.match(source,/!preview \|\| !consent \|\| busy/);
  assert.match(source,/preview_token:preview.preview_token,authorize:true/);
  assert.match(source,/preview_token:p.preview_token,authorize:true/);
  assert.match(source,/setPreview\(null\);setConsent\(false\)/);
});
test("news is a continuous document with source links, not a card grid",()=>{
  const source=read("news-reader.tsx");
  assert.match(source,/news-overview/);assert.match(source,/news-section/);assert.match(source,/news-topic/);
  assert.doesNotMatch(source,/task-card|knowledge-card|dangerouslySetInnerHTML/);
  assert.match(source,/openSource\(topic.source_message_ids/);
  assert.match(source,/type="date"/);
});
