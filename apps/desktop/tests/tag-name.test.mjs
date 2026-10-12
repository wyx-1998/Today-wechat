import assert from "node:assert/strict";
import { test } from "node:test";
import { validateTagName } from "../src/tag-name.ts";

test("empty and whitespace-only names cannot save", () => {
  for (const name of ["", "  ", "\t\n", "\u001c\u0085\u3000"]) {
    assert.deepEqual(validateTagName(name), { name: "", length: 0, valid: false });
  }
});
test("one and thirty Chinese characters can save", () => {
  for (const length of [1, 30]) {
    assert.deepEqual(validateTagName("项".repeat(length)), {
      name: "项".repeat(length), length, valid: true,
    });
  }
});
test("long pasted input remains intact and cannot save", () => {
  for (const length of [31, 50]) {
    const input = "项".repeat(length);
    assert.deepEqual(validateTagName(input), { name: input, length, valid: false });
    assert.equal(input.length, length);
  }
});
test("emoji count as code points rather than UTF-16 units", () => {
  assert.equal(validateTagName("😀".repeat(30)).valid, true);
  assert.equal(validateTagName("😀".repeat(31)).valid, false);
  assert.equal(validateTagName("👨‍👩‍👧‍👦").length, 7);
});
test("combining marks are not silently normalized", () => {
  assert.deepEqual(validateTagName("e\u0301"), { name: "e\u0301", length: 2, valid: true });
});
test("trim agrees with Python for less common whitespace", () => {
  assert.equal(validateTagName("\u0085\u001c工作\u3000").name, "工作");
  assert.equal(validateTagName("\ufeff工作\ufeff").length, 4);
  assert.equal(validateTagName("  " + "项".repeat(30) + "  ").valid, true);
});
