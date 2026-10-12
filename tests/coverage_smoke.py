"""Independent source metadata audit, generated SQLite only, no model/keychain."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from backend.contracts import ReadScope, ServiceError, parse_time, stable_id
from backend.reader import SourceReader
from backend.storage import Store
from scripts.audit_text_coverage import audit_application, content_hash, source_inventory
from tests.fixture_support import database, fixture_environment, load_suite, message_table


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.case = load_suite()[0]["selected_chat"]
        self.paths = self.enterContext(fixture_environment(self.case))
        self.root = self.paths.message_db.parent.parent
        self.reader = SourceReader(self.root, "generated-audit-account", self.case["self_id"])
        self.scope = ReadScope(self.reader.account_id, tuple(self.case["selected_chats"]), self.case["window"]["start"], self.case["window"]["end"])
        self.store = Store(self.root / "app-audit")
        self.messages = []
        for chat in self.scope.chat_ids:
            for stream in self.reader.streams(chat):
                self.messages.extend(self.reader.read_batch(self.scope, chat, stream).messages)
        with self.store.database() as db:
            for m in self.messages:
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (m.message_id,m.account_id,m.chat_id,m.stream_id,m.local_id,m.sender_id,m.sender_name,m.is_self,m.sent_at,parse_time(m.sent_at),m.message_type,m.content,m.content_state,m.analysis_eligible))
        self.ids = [m.message_id for m in self.messages]

    def successful(self, name="generated-run", ids=None):
        with self.store.database() as db:
            db.execute("INSERT INTO analysis_runs VALUES(?,?,?,?,?,?,?,?,?)", (name,self.scope.account_id,json.dumps(self.ids if ids is None else ids),1,1,"success",self.scope.time_end,self.scope.time_end,None))

    def audit(self, **kwargs):
        inventory, failures = source_inventory(self.reader, self.scope)
        return audit_application(self.store.path, self.scope, inventory=inventory, failures=failures, **kwargs)

    def context_fixture(self, *, only_context=False):
        chat = self.scope.chat_ids[0]
        with database(self.paths.message_db) as db:
            db.execute(f"INSERT INTO [{message_table(chat)}] SELECT local_id+1,real_sender_id,create_time+1,local_type,status,'Generated fresh acceptance' FROM [{message_table(chat)}]")
        found = []
        for stream in self.reader.streams(chat):
            found.extend(self.reader.read_batch(self.scope, chat, stream).messages)
        fresh = next(m for m in found if m.message_id not in self.ids)
        with self.store.database() as db:
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (fresh.message_id,fresh.account_id,fresh.chat_id,fresh.stream_id,fresh.local_id,fresh.sender_id,fresh.sender_name,fresh.is_self,fresh.sent_at,parse_time(fresh.sent_at),fresh.message_type,fresh.content,fresh.content_state,fresh.analysis_eligible))
            context = dict(db.execute("SELECT * FROM messages WHERE message_id=?", (self.ids[0],)).fetchone())
            digest = stable_id([(context["message_id"],context["content"],context["sent_at"],context["is_self"],context["message_type"],context["analysis_eligible"],context["sender_id"],context["account_id"],context["chat_id"])])
            db.execute("INSERT INTO text_grants VALUES(?,?,?,?,?,?,?)", ("generated-grant",json.dumps({"chat_ids":[chat],"time_start":self.scope.time_start}),"generated-scope","active",self.scope.time_start,None,None))
            db.execute("INSERT INTO text_batches VALUES(?,?,?,?,?,?,?,?,?,?)", ("new-run","generated-grant",self.scope.account_id,chat,json.dumps([fresh.message_id]),"unused-audit-fresh-hash","success",self.scope.time_end,self.scope.time_end,None))
            db.execute("INSERT INTO text_batch_contexts VALUES(?,?,?,1)", ("new-run",json.dumps([self.ids[0]]),digest))
            refs = [self.ids[0]] if only_context else [fresh.message_id,self.ids[0]]
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("generated-context-task",self.scope.account_id,"new-run","PRIVATE-AUDIT-CONTEXT-MARKER","unknown",None,None,"unknown","needs_confirmation",json.dumps(refs),self.scope.time_end,1))
        self.successful("old-run", ids=self.ids)
        self.successful("new-run", ids=[fresh.message_id])
        return fresh.message_id

    def test_legitimate_context_evidence_is_not_duplicate_fresh_submission(self):
        self.context_fixture()
        result = self.audit()
        self.assertTrue(result["text_input_coverage_complete"])
        self.assertEqual(result["context_text_uses"], 1)
        self.assertEqual(result["invalid_context_manifests"], 0)
        self.assertEqual(result["text_in_multiple_successful_runs"], 0)
        self.assertNotIn("PRIVATE-AUDIT-CONTEXT-MARKER", json.dumps(result))

    def test_context_only_task_is_not_valid_new_task_evidence(self):
        self.context_fixture(only_context=True)
        result = self.audit()
        self.assertFalse(result["text_input_coverage_complete"])
        self.assertEqual(result["invalid_task_source_refs"], 1)

    def test_missing_context_manifest_fails_audit_not_legacy_fallback(self):
        self.context_fixture()
        with self.store.database() as db:
            db.execute("DELETE FROM text_batch_contexts")
        result = self.audit()
        self.assertFalse(result["text_input_coverage_complete"])
        self.assertEqual(result["invalid_context_manifests"], 1)

    def test_context_sender_tamper_fails_hash_audit(self):
        self.context_fixture()
        with self.store.database() as db:
            db.execute("UPDATE messages SET sender_id='different-generated-person' WHERE message_id=?", (self.ids[0],))
        result = self.audit()
        self.assertFalse(result["text_input_coverage_complete"])
        self.assertEqual(result["invalid_context_manifests"], 1)

    def test_context_grant_scope_mismatch_is_detected(self):
        self.context_fixture()
        with self.store.database() as db:
            db.execute("UPDATE text_grants SET scope_json=?", (json.dumps({"chat_ids":["different-generated-chat"],"time_start":self.scope.time_start}),))
        result = self.audit()
        self.assertFalse(result["text_input_coverage_complete"])
        self.assertEqual(result["invalid_context_manifests"], 1)

    def review_fixture(self):
        self.context_fixture()
        with self.store.database() as db:
            rows = db.execute("SELECT * FROM messages ORDER BY sent_epoch,message_id").fetchall()
            ids = [r["message_id"] for r in rows]
        self.successful("review-run", ids=ids)
        with self.store.database() as db:
            digest = stable_id([(r["message_id"],r["account_id"],r["chat_id"],r["sender_id"],r["sent_at"],
                r["sent_epoch"],r["content"],r["is_self"],r["analysis_eligible"],r["message_type"],r["content_state"]) for r in rows])
            db.execute("INSERT INTO review_runs VALUES(?,?,?,?,?,1)", ("review-run",self.scope.chat_ids[0],self.scope.time_start,self.scope.time_end,digest))
            db.execute("INSERT INTO task_evidence_links SELECT task_id,batch_id,source_message_ids FROM tasks")
            db.execute("INSERT INTO task_evidence_links VALUES(?,?,?)", ("generated-context-task","review-run",json.dumps(ids)))

    def test_intentional_review_is_counted_separately_not_duplicate_primary_input(self):
        self.review_fixture()
        result = self.audit()
        self.assertTrue(result["text_input_coverage_complete"])
        self.assertEqual(result["review_text_uses"], 2)
        self.assertEqual(result["invalid_review_manifests"], 0)
        self.assertEqual(result["text_in_multiple_successful_runs"], 0)

    def test_review_manifest_participant_tamper_fails_independent_audit(self):
        self.review_fixture()
        with self.store.database() as db:
            db.execute("UPDATE messages SET sender_id='different' WHERE message_id=?", (self.ids[0],))
        result = self.audit()
        self.assertFalse(result["text_input_coverage_complete"])
        self.assertEqual(result["invalid_review_manifests"], 1)

    def test_linked_task_evidence_must_belong_to_its_recorded_input(self):
        self.review_fixture()
        with self.store.database() as db:
            db.execute("UPDATE task_evidence_links SET source_message_ids='[\"outside\"]' WHERE batch_id='review-run'")
        result = self.audit()
        self.assertFalse(result["text_input_coverage_complete"])
        self.assertEqual(result["invalid_task_source_refs"], 1)

    def test_failed_review_cannot_support_linked_evidence(self):
        self.review_fixture()
        with self.store.database() as db:
            db.execute("UPDATE analysis_runs SET status='failed' WHERE batch_id='review-run'")
        result = self.audit()
        self.assertFalse(result["text_input_coverage_complete"])
        self.assertEqual(result["invalid_task_source_refs"], 1)

    def test_complete_input_does_not_claim_semantic_correctness(self):
        self.successful()
        result = self.audit()
        self.assertTrue(result["text_input_coverage_complete"])
        self.assertEqual(result["semantic_task_completeness"], "not_evaluated")
        self.assertEqual(result["content_consistency"], "matched")
        self.assertEqual(result["tasks_with_in_scope_evidence"], 0)

    def test_missing_message_is_not_hidden_by_processed_count(self):
        self.successful()
        with self.store.database() as db:
            db.execute("DELETE FROM messages WHERE message_id=?", (self.ids[0],))
        result = self.audit()
        self.assertFalse(result["text_input_coverage_complete"])
        self.assertEqual(result["chats"][0]["missing_from_application"], 1)

    def test_read_but_not_analyzed_is_explicit(self):
        result = self.audit()
        self.assertFalse(result["text_input_coverage_complete"])
        self.assertEqual(result["chats"][0]["unprocessed_text"], len(self.ids))

    def test_source_missing_is_unknown_not_zero(self):
        absent = replace(self.scope, chat_ids=("generated-absent",))
        inventory, errors = source_inventory(self.reader, absent)
        result = audit_application(self.store.path, absent, inventory=inventory, failures=errors)
        self.assertFalse(result["text_input_coverage_complete"])
        self.assertIsNone(result["chats"][0]["source_messages"])
        self.assertEqual(result["chats"][0]["source_error"], "missing_table")

    def test_duplicate_successful_inputs_are_detected(self):
        self.successful("one"); self.successful("two")
        result = self.audit()
        self.assertFalse(result["text_input_coverage_complete"])
        self.assertEqual(result["text_in_multiple_successful_runs"], len(self.ids))

    def test_invalid_task_evidence_rejects_complete_coverage(self):
        self.successful()
        with self.store.database() as db:
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("generated-task",self.scope.account_id,"generated-run","PRIVATE-AUDIT-MARKER","unknown",None,None,"unknown","needs_confirmation",json.dumps([self.ids[0],"nonexistent"]),self.scope.time_end,1))
        result = self.audit()
        self.assertEqual(result["invalid_task_source_refs"], 1)
        self.assertFalse(result["text_input_coverage_complete"])
        self.assertNotIn("PRIVATE-AUDIT-MARKER", json.dumps(result))
        for mid in self.ids:
            self.assertNotIn(mid, json.dumps(result))

    def test_empty_scope_is_rejected(self):
        with self.assertRaises(ServiceError):
            audit_application(self.store.path, replace(self.scope, chat_ids=()))

    def test_full_interval_query_includes_local_id_zero_independent_of_cursor(self):
        with database(self.paths.message_db) as db:
            db.execute(f"UPDATE [{message_table(self.scope.chat_ids[0])}] SET local_id=0")
        inventory, _ = source_inventory(self.reader, self.scope)
        self.assertEqual(len(inventory[self.scope.chat_ids[0]]), len(self.ids))
        result = self.audit()
        self.assertEqual(result["chats"][0]["missing_from_application"], len(self.ids))
        self.assertEqual(result["chats"][0]["not_in_current_source"], len(self.ids))

    def test_audit_leaves_database_and_source_unchanged(self):
        paths = (self.store.path, self.paths.message_db)
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        self.audit()
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])

    def test_no_read_permission_refuses_before_opening_any_database(self):
        result = subprocess.run([sys.executable,"-m","scripts.audit_text_coverage","--database",str(self.root/"nonexistent.db"),"--chat-name","generated","--start",self.scope.time_start,"--end",self.scope.time_end],capture_output=True,text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no databases opened", result.stderr)
        self.assertFalse((self.root/"nonexistent.db").exists())

    def test_same_id_with_changed_text_is_not_complete(self):
        self.successful()
        with self.store.database() as db:
            db.execute("UPDATE messages SET content='PRIVATE-AUDIT-MARKER' WHERE message_id=?", (self.ids[0],))
        result = self.audit()
        self.assertFalse(result["text_input_coverage_complete"])
        self.assertEqual(result["chats"][0]["text_content_mismatches"], 1)
        self.assertNotIn("PRIVATE-AUDIT-MARKER", json.dumps(result))

    def test_empty_task_sources_are_detected(self):
        self.successful()
        with self.store.database() as db:
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("generated-empty",self.scope.account_id,"generated-run","Generated","unknown",None,None,"unknown","needs_confirmation","[]",self.scope.time_end,1))
        self.assertFalse(self.audit()["text_input_coverage_complete"])
        self.assertEqual(self.audit()["invalid_task_source_refs"], 1)

    def test_missing_codec_is_environment_error_not_source_corruption(self):
        with patch.dict(sys.modules, {"zstandard": None}):
            with self.assertRaises(ServiceError) as caught:
                content_hash(b"\x28\xb5\x2f\xfdgenerated")
        self.assertEqual(caught.exception.code, "audit_codec_missing")

    def test_invalid_utf8_is_unreadable_not_fake_empty_text(self):
        self.assertIsNone(content_hash(b"\xff"))
        self.assertNotEqual(content_hash(""), None)


if __name__ == "__main__":
    unittest.main()
