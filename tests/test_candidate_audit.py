"""External candidate inspection is scoped, read-only and not a quality score."""

from pathlib import Path
import json
import tempfile
import unittest

from backend.action_discovery import DiscoveryAudit
from backend.contracts import ServiceError
from backend.storage import Store
from scripts.audit_candidate_coverage import audit


class CandidateAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="today-candidate-inspection-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        with self.store.database() as db:
            for mid, epoch in (("one", 1), ("two", 2)):
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (mid,"account","chat","stream",mid,
                    "sender", "nickname", None, "2026-10-02T00:00:00+00:00",epoch,"text","PRIVATE BODY","text_available",1))
            db.execute("INSERT INTO analysis_runs VALUES('batch','account','[\"one\",\"two\"]',1,1,'running','now',NULL,NULL)")
        self.record = DiscoveryAudit(self.store, "batch", lambda: 1)

    def inspect(self, watched=()):
        return audit(self.store.path, "batch", watched)

    def window(self, index=0, mid="one", candidates=None):
        candidates = candidates if candidates is not None else [{"candidate_id": "C1", "title": "PRIVATE TITLE",
            "owner_type": "unknown", "due_date": None, "source_message_ids": [mid]}]
        self.record.window(index, [{"message_id": mid}], [], candidates)

    def test_before_first_response_is_not_empty_success(self):
        report = self.inspect(["one"])
        self.assertEqual(report["diagnostic_status"], "awaiting_valid_window")
        self.assertFalse(report["focal_coverage_complete"])
        self.assertFalse(report["decision_coverage_complete"])
        self.assertEqual(report["watched"][0]["candidate_count"], 0)

    def test_partial_window_records_candidate_without_publication(self):
        self.window()
        report = self.inspect(["one", "two"])
        self.assertEqual(report["focal_processed"], 1)
        self.assertEqual(report["decisions"], {"undecided": 1})
        self.assertFalse(report["publication_success"])
        self.assertFalse(report["watched"][1]["focal_processed"])

    def test_complete_empty_discovery_is_not_a_quality_claim(self):
        self.window(candidates=[]); self.window(1, "two", [])
        report = self.inspect()
        self.assertTrue(report["focal_coverage_complete"])
        self.assertTrue(report["decision_coverage_complete"])
        self.assertFalse(report["publication_success"])
        self.assertEqual(report["semantic_completeness"], "not_evaluated")

    def test_finished_decision_counts_and_no_private_payload_output(self):
        self.window(); self.window(1, "two", [])
        self.record.decisions([{"candidate_id":"C1","outcome":"not_actionable","task_index":None,
            "source_message_ids":["one"],"reason":"PRIVATE REASON"}])
        with self.store.database() as db:
            db.execute("UPDATE analysis_runs SET status='success'")
        report = self.inspect(["one"])
        self.assertEqual(report["watched"][0]["outcomes"], {"not_actionable":1})
        self.assertTrue(report["publication_success"])
        self.assertFalse(any(word in json.dumps(report) for word in ("PRIVATE", "nickname", "sender")))

    def test_watched_source_outside_batch_is_rejected(self):
        with self.assertRaises(ServiceError) as error:
            self.inspect(["outside"])
        self.assertEqual(error.exception.code, "candidate_audit_scope")

    def test_duplicate_focal_across_windows_rejected(self):
        self.window(candidates=[]); self.window(1, "one", [])
        with self.assertRaises(ServiceError):
            self.inspect()

    def test_cross_account_source_rejected(self):
        with self.store.database() as db:
            db.execute("UPDATE messages SET account_id='elsewhere' WHERE message_id='one'")
        with self.assertRaises(ServiceError):
            self.inspect()

    def test_non_text_or_unavailable_input_is_rejected(self):
        for column, value in (("message_type", "image"), ("content_state", "read_failed"),
                              ("analysis_eligible", 0), ("content", None)):
            with self.store.database() as db:
                db.execute("UPDATE messages SET " + column + "=? WHERE message_id='one'", (value,))
            with self.assertRaises(ServiceError):
                self.inspect()
            with self.store.database() as db:
                db.execute("UPDATE messages SET message_type='text',content_state='text_available',analysis_eligible=1,content='PRIVATE BODY'")

    def test_unknown_record_policy_is_not_assumed_supported(self):
        with self.store.database() as db:
            db.execute("UPDATE discovery_attempts SET policy_version=99")
        with self.assertRaises(ServiceError):
            self.inspect()

    def test_unavailable_history_does_not_invent_candidates(self):
        with self.store.database() as db:
            db.execute("DELETE FROM discovery_attempts")
        self.assertEqual(self.inspect()["diagnostic_status"], "no_candidate_records")

    def test_multiple_attempts_use_latest_without_erasing_previous(self):
        self.window()
        second = DiscoveryAudit(self.store, "batch", lambda: 2)
        report = self.inspect()
        self.assertEqual(report["attempt_count"], 2)
        self.assertEqual(report["attempt_id"], second.attempt_id)
        self.assertEqual(report["window_count"], 0)

    def test_bad_candidate_count_rejected(self):
        self.window()
        with self.store.database() as db:
            db.execute("UPDATE discovery_windows SET candidate_count=99")
        with self.assertRaises(ServiceError):
            self.inspect()

    def test_database_unchanged_and_closed_after_inspection(self):
        self.window()
        before = Path(self.store.path).read_bytes()
        self.inspect()
        self.assertEqual(Path(self.store.path).read_bytes(), before)
        with self.store.database() as db:
            db.execute("BEGIN EXCLUSIVE")

    def test_later_completion_evidence_is_structural_not_semantic(self):
        self.window(); self.window(1, "two", [])
        self.record.decisions([{"candidate_id":"C1","outcome":"fulfilled","task_index":None,
            "source_message_ids":["one","two"],"reason":"same request finished"}])
        self.assertEqual(self.inspect()["decisions"], {"fulfilled":1})
        with self.store.database() as db:
            db.execute("UPDATE discovery_candidates SET decision_source_message_ids='[\"one\"]'")
        with self.assertRaises(ServiceError):
            self.inspect()

    def renewed_commitment(self):
        with self.store.database() as db:
            db.execute("INSERT INTO messages SELECT 'three',account_id,chat_id,stream_id,'three',sender_id,sender_name,is_self,sent_at,3,message_type,content,content_state,analysis_eligible FROM messages WHERE message_id='one'")
            db.execute("UPDATE analysis_runs SET message_ids='[\"one\",\"two\",\"three\"]'")
        self.record.window(0, [{"message_id": mid} for mid in ("one", "two", "three")], [],
            [{"candidate_id":"C1","title":"renewed request","owner_type":"unknown","due_date":None,
              "source_message_ids":["one","three"]}])
        self.record.decisions([{"candidate_id":"C1","outcome":"fulfilled","task_index":None,
            "source_message_ids":["one","two","three"],"reason":"old intervening delivery"}])

    def test_new_policy_rejects_delivery_before_last_renewed_commitment(self):
        self.renewed_commitment()
        with self.assertRaises(ServiceError):
            self.inspect()
        with self.store.database() as db:
            self.assertEqual(db.execute("SELECT policy_version FROM discovery_attempts").fetchone()[0], 3)

    def test_old_policy_remains_readable_without_rewriting_history_or_rejudging_semantics(self):
        self.renewed_commitment()
        with self.store.database() as db:
            db.execute("UPDATE discovery_attempts SET policy_version=1")
        before = Path(self.store.path).read_bytes()
        report = self.inspect()
        self.assertEqual(report["policy_version"], 1)
        self.assertEqual(report["decisions"], {"fulfilled":1})
        self.assertEqual(report["semantic_completeness"], "not_evaluated")
        self.assertEqual(Path(self.store.path).read_bytes(), before)
