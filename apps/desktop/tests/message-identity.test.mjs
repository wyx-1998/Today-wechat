import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {messageRelation} from '../src/types.ts';

test('current true and false override old identity without modifying response', () => {
  for (const effective of [true, false]) {
    const row = {is_self: null, effective_is_self: effective};
    assert.equal(messageRelation(row), effective);
    assert.equal(row.is_self, null);
  }
});
test('explicit current unknown never falls back to stale confirmed identity', () => {
  assert.equal(messageRelation({is_self: true, effective_is_self: null}), null);
});
test('older response without current interpretation keeps reader identity', () => {
  for (const value of [true, false, null]) assert.equal(messageRelation({is_self: value}), value);
});
test('list detail context and task-source views share current relation helper', () => {
  for (const path of ['../src/message-browser.tsx', '../src/results.tsx']) {
    const text = readFileSync(new URL(path, import.meta.url), 'utf8');
    assert.match(text, /messageRelation/);
    assert.doesNotMatch(text, /(?:item|message|source)\.is_self\s*===/);
  }
});
