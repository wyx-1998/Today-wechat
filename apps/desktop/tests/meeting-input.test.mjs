import test from "node:test";
import assert from "node:assert/strict";
import {decodeMeetingFile, meetingFileError, MEETING_MAX_BYTES, MEETING_MAX_CHARS, meetingStatus} from "../src/meeting-input.ts";

test("meeting files allow text formats only with a bounded file size", () => {
  for (const name of ["会议.txt", "会议.MD", "会议.markdown"]) assert.equal(meetingFileError(name, MEETING_MAX_BYTES), null);
  for (const name of ["会议.pdf", "会议.docx", "会议.txt.exe"]) assert.ok(meetingFileError(name, 20));
  assert.ok(meetingFileError("会议.txt", MEETING_MAX_BYTES + 1));
});
test("UTF8 text preserves full Unicode and newlines; BOM is an encoding marker", () => {
  const original = "会议纪要\r\n李明：发送资料📎\n　保留空格";
  assert.equal(decodeMeetingFile(new TextEncoder().encode(original).buffer), original);
  assert.equal(decodeMeetingFile(new TextEncoder().encode("\ufeff" + original).buffer), original);
});
test("bad encoding, empty or binary text and oversized text are refused, not truncated", () => {
  for (const bytes of [new Uint8Array([255]), new TextEncoder().encode("  \n"), new TextEncoder().encode("x\0y"), new TextEncoder().encode("字".repeat(MEETING_MAX_CHARS + 1))]) {
    assert.throws(() => decodeMeetingFile(bytes.buffer));
  }
});
test("meeting statuses distinguish generation from publication", () => {
  assert.equal(meetingStatus.draft, "未生成"); assert.equal(meetingStatus.ready, "待加入");
  assert.equal(meetingStatus.generating, "生成中"); assert.equal(meetingStatus.published, "已加入");
});
