"""Generated isolated sources and controlled model responses; no live requests."""
from contextlib import closing
import io
import json
import sqlite3
import tempfile
import unittest

from backend import attention
from backend.analysis import SCHEMA
from backend.contracts import ServiceError
from backend.gemini import GeminiClient
from backend.model_references import compact_prompt
from backend.storage import Store
from tests import automatic_smoke


def row(mid="fresh", **fields):
    return {"message_id": mid, "account_id": "generated-account", "chat_id": "first",
        "message_type": "text", "content_state": "text_available", "analysis_eligible": 1,
        "content": "本周评审改到线上举行", "sent_at": "2026-10-02T03:00:00Z", "sent_epoch": 1790910000,
        **fields}


def item(**fields):
    return {"kind": "information", "title": "评审安排调整", "summary": "群内通知评审改为线上。",
        "attention_reason": "需要知晓参会方式变化。", "source_message_ids": ["fresh"], **fields}


class AttentionUnitTests(unittest.TestCase):
    def split(self, items, rows=None, context=()):
        return attention.split({"tasks": [], "attention_items": items}, rows or [row()], context)

    def test_valid_categories_keep_explicit_sources_and_message_time(self):
        base, accepted, rejected = self.split([item(), item(kind="key_point", title="参会方式结论")])
        self.assertEqual(base, {"tasks": []})
        self.assertEqual(rejected, [])
        self.assertEqual(accepted[0]["observed_at"], row()["sent_at"])
        self.assertEqual(accepted[0]["related_chat_ids"], ["first"])
        self.assertNotIn("event_at", accepted[0])

    def test_invalid_independent_evidence_leaves_valid_items(self):
        _, accepted, rejected = self.split([item(source_message_ids=["outside"]), item()])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(rejected, [(0, "sources")])

    def test_empty_unknown_alias_duplicates_and_nontext_sources_rejected(self):
        for refs in ([], ["fresh", "fresh"], [{}], ["unknown"]):
            self.assertEqual(self.split([item(source_message_ids=refs)])[1], [])
        for fields in ({"message_type": "image"}, {"content_state": "type_only"},
                       {"analysis_eligible": 0}, {"content": None}):
            self.assertEqual(self.split([item()], [row(**fields)])[1], [])

    def test_context_requires_fresh_evidence_and_can_span_chats(self):
        old = row("old", chat_id="second", sent_epoch=1790900000, sent_at="2026-10-02T00:13:20Z")
        self.assertEqual(self.split([item(source_message_ids=["old"])], context=[old])[1], [])
        accepted = self.split([item(source_message_ids=["old", "fresh"])], context=[old])[1][0]
        self.assertEqual(accepted["related_chat_ids"], ["first", "second"])
        self.assertEqual(accepted["observed_at"], row()["sent_at"])

    def test_cross_account_is_rejected(self):
        result = self.split([item(source_message_ids=["fresh", "foreign"])],
            context=[row("foreign", account_id="foreign")])
        self.assertEqual(result[1], [])
        self.assertEqual(result[2], [(0, "account")])

    def test_link_only_is_not_treated_as_read_article(self):
        _, accepted, rejected = self.split([item()], [row(content="https://generated.invalid/news")])
        self.assertEqual(accepted, [])
        self.assertEqual(rejected, [(0, "link_only")])

    def test_extra_fields_or_bad_kind_are_not_silently_adopted(self):
        for bad in (item(kind="task"), item(event_at="2026-10-03"), item(title="  "), item(summary="x"*2001)):
            self.assertEqual(self.split([bad])[2], [(0, "fields")])

    def test_sensitive_output_redacted_and_duplicate_diagnosed(self):
        accepted = self.split([item(summary="通知联系电话13800138000")])[1]
        self.assertNotIn("13800138000", accepted[0]["summary"])
        self.assertEqual(self.split([item(), item()])[2], [(1, "duplicate")])

    def test_old_response_compatible_but_malformed_envelope_fails(self):
        self.assertEqual(attention.split({"tasks": []}, [row()]), ({"tasks": []}, [], []))
        with self.assertRaises(ServiceError):
            self.split(None)

    def test_wire_aliases_restore_only_references_not_summary(self):
        payload = {"new_messages": [{"source_message_id": "fresh", "participant": None,
            "content": "消息正文 M1", "sent_at": row()["sent_at"]}], "context_messages": []}
        _, refs = compact_prompt("输入："+json.dumps(payload), attention.schema(SCHEMA))
        answer = refs.restore({"tasks": [], "attention_items": [item(summary="正文含 M1", source_message_ids=["M1"])]})
        self.assertEqual(answer["attention_items"][0]["summary"], "正文含 M1")
        self.assertEqual(answer["attention_items"][0]["source_message_ids"], ["fresh"])
        bad = refs.restore({"attention_items": [item(source_message_ids=["M99"])]})
        self.assertEqual(attention.split(bad, [row()])[2], [(0, "sources")])


class AttentionFlowTests(unittest.TestCase):
    def setUp(self):
        self.h = automatic_smoke.AutomaticTests("runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.service = self.h.service
        self.h.add("news", content="本周评审改到线上举行")
        self.news = item(source_message_ids=["news"])
        self.task = {"title": "发送评审资料", "owner_type": "unknown", "due_date": None,
            "source_message_ids": ["initial"]}

    def run_batch(self, answer=None):
        self.h.answer(answer or {"tasks": [self.task], "attention_items": [self.news]})
        self.h.ready()
        result = self.service.automatic.tick()
        self.service.pause()
        return result

    def test_automatic_publishes_task_and_knowledge_with_one_call(self):
        self.assertEqual(self.run_batch(), {"status": "success", "task_count": 1})
        today = self.service.dispatch("today.list", {})
        self.assertEqual(len(today["items"]), 1)
        self.assertEqual(len(today["attention_items"]), 1)
        self.assertEqual(len(self.h.client.calls), 1)
        news = today["attention_items"][0]
        self.assertEqual(news["source_message_ids"], ["news"])
        self.assertEqual(news["source_chats"], [{"chat_id": "first", "chat_name": "Generated first"}])
        self.assertIsNone(news["event_at"])
        self.assertNotIn("attention_items", self.service.dispatch("tasks.list", {}))

    def test_invalid_knowledge_item_does_not_block_other_knowledge_or_task(self):
        self.run_batch({"tasks": [self.task], "attention_items": [item(), self.news]})
        today = self.service.dispatch("today.list", {})
        self.assertEqual(len(today["items"]), 1)
        self.assertEqual(len(today["attention_items"]), 1)
        self.assertEqual(today["last_analysis"]["rejected_attention_count"], 1)

    def test_task_completion_preserves_knowledge_and_revision(self):
        self.run_batch()
        before = self.service.dispatch("today.list", {})
        task = before["items"][0]
        self.service.dispatch("task.update", {"task_id": task["task_id"], "revision": task["revision"], "status": "completed"})
        after = self.service.dispatch("today.list", {})
        self.assertEqual(after["attention_items"], before["attention_items"])
        self.assertEqual(after["results_revision"], before["results_revision"])
        self.assertEqual(after["items"][0]["status"], "completed")

    def test_full_window_review_deduplicates_knowledge_without_task_overwrite(self):
        self.run_batch()
        before = self.service.dispatch("today.list", {})
        self.h.client.generate = lambda *args, **kwargs: ({"tasks": [], "changes": [],
            "attention_items": [self.news]}, {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130})
        preview = self.service.dispatch("analysis.review.preview", {"chat_id": "first"})
        self.service.dispatch("analysis.review", {"chat_id": "first", "authorize": True, "preview_token": preview["preview_token"]})
        after = self.service.dispatch("today.list", {})
        self.assertEqual(after["attention_items"], before["attention_items"])
        self.assertEqual(after["items"], before["items"])

    def test_manual_first_analysis_also_generates_sourced_knowledge(self):
        self.h.answer({"tasks": [], "attention_items": [self.news]})
        preview = self.service.dispatch("analysis.real.preview", {"chat_id": "first"})
        self.service.dispatch("analysis.real", {"chat_id": "first", "authorize": True, "preview_token": preview["preview_token"]})
        self.assertEqual(len(self.service.dispatch("today.list", {})["attention_items"]), 1)

    def test_actual_gemini_adapter_uses_attention_schema_and_restores_alias(self):
        owner = self
        class Opener:
            def open(self, request, timeout):
                wire = json.loads(request.data)
                owner.assertIn("attention_items", wire["generationConfig"]["responseSchema"]["properties"])
                payload = json.loads(wire["contents"][0]["parts"][0]["text"].partition("输入：")[2])
                alias = next(m["source_message_id"] for m in payload["new_messages"] if "线上" in m["content"])
                answer = {"tasks": [], "attention_items": [item(source_message_ids=[alias])]}
                return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {
                    "parts": [{"text": json.dumps(answer)}]}}], "usageMetadata": {"totalTokenCount": 100}}).encode())
        self.service.models.client = GeminiClient(opener=Opener())
        self.h.ready(); self.service.automatic.tick(); self.service.pause()
        self.assertEqual(self.service.dispatch("today.list", {})["attention_items"][0]["source_message_ids"], ["news"])

    def test_failed_knowledge_storage_rolls_back_tasks_in_same_transaction(self):
        with self.service.store.database() as db:
            db.execute("CREATE TRIGGER reject_attention BEFORE INSERT ON attention_items BEGIN SELECT RAISE(ABORT,'generated failure'); END")
        result = self.run_batch()
        self.assertEqual(result["status"], "failed")
        with self.service.store.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attention_items").fetchone()[0], 0)

    def test_failed_runs_or_other_account_items_do_not_leak_into_today(self):
        self.run_batch()
        with self.service.store.database() as db:
            db.execute("UPDATE analysis_runs SET status='failed'")
        self.assertEqual(self.service.dispatch("today.list", {})["attention_items"], [])
        with self.service.store.database() as db:
            db.execute("UPDATE analysis_runs SET status='success'")
            db.execute("UPDATE attention_items SET account_id='other-account'")
        self.assertEqual(self.service.dispatch("today.list", {})["attention_items"], [])

    def test_publish_rechecks_changed_evidence(self):
        self.run_batch()
        with self.service.store.database() as db:
            batch_id = db.execute("SELECT batch_id FROM analysis_runs").fetchone()[0]
            rows = [dict(db.execute("SELECT * FROM messages WHERE message_id='news'").fetchone())]
            accepted = attention.split({"tasks": [], "attention_items": [self.news]}, rows)[1]
            db.execute("UPDATE messages SET analysis_eligible=0 WHERE message_id='news'")
            with self.assertRaises(ServiceError) as error:
                attention.publish(db, batch_id=batch_id, account="generated-account", items=accepted,
                    rejected=[], clock=lambda: self.h.now)
            self.assertEqual(error.exception.code, "attention_source_changed")

    def test_manual_storage_failure_marks_failed_and_does_not_leave_tasks(self):
        self.h.answer({"tasks": [self.task], "attention_items": [self.news]})
        with self.service.store.database() as db:
            db.execute("CREATE TRIGGER reject_attention BEFORE INSERT ON attention_items BEGIN SELECT RAISE(ABORT,'generated failure'); END")
        preview = self.service.dispatch("analysis.real.preview", {"chat_id": "first"})
        with self.assertRaises(ServiceError) as error:
            self.service.dispatch("analysis.real", {"chat_id": "first", "authorize": True, "preview_token": preview["preview_token"]})
        self.assertEqual(error.exception.code, "analysis_store_failed")
        with self.service.store.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM analysis_runs").fetchone()[0], "failed")


class AttentionMigrationTests(unittest.TestCase):
    def test_v8_upgrade_preserves_all_existing_rows_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Store(directory)
            with old.database() as db:
                for table in ("ask_citations","ask_runs","ask_turns","ask_threads","ask_grants","ask_fts","ask_index"): db.execute("DROP TABLE "+table)
                for table in ("news_settings","news_runs","news_editions"): db.execute("DROP TABLE "+table)
                db.execute("DROP TABLE meeting_notes")
                db.execute("DROP TABLE attention_rejections")
                db.execute("DROP TABLE attention_items")
                db.execute("PRAGMA user_version=8")
                tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                before = {table: sorted(map(tuple, db.execute(f'SELECT * FROM "{table}"'))) for table in tables}
            for _ in range(2):
                fresh = Store(directory)
                with fresh.database() as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 12)
                    self.assertEqual({t: sorted(map(tuple, db.execute(f'SELECT * FROM "{t}"'))) for t in tables}, before)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM attention_items").fetchone()[0], 0)

    def test_v8_conflict_rolls_back_new_table_and_keeps_version(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Store(directory)
            with old.database() as db:
                db.execute("DROP TABLE attention_items")
                db.execute("PRAGMA user_version=8")
            with self.assertRaises(ServiceError):
                Store(directory)
            with closing(sqlite3.connect(old.path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 8)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='attention_items'").fetchone())
