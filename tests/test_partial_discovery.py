"""Independent synthetic expectations for partial candidate publication."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from backend.action_discovery import DiscoveryAudit, partition_result
from backend.analysis import Analysis
from backend.contracts import ServiceError
from backend.reanalysis import Reanalysis
from backend.storage import Store, SCHEMA, MODEL_MIGRATION, AUTOMATIC_MIGRATION, CONTEXT_MIGRATION, REVIEW_MIGRATION, DISCOVERY_MIGRATION
from scripts.audit_candidate_coverage import audit as inspect
from tests.test_action_discovery import row, task, decision


class PartialDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.rows = [row(i) for i in range(3)]
        self.tasks = [task(r["message_id"], f"交付独立材料{i}") for i, r in enumerate(self.rows)]
        self.candidates = [t | {"candidate_id": f"C{i+1}"} for i, t in enumerate(self.tasks)]
        self.decisions = [decision(f"C{i+1}", index=i, refs=t["source_message_ids"]) for i, t in enumerate(self.tasks)]
        self.answer = {"tasks": self.tasks, "candidate_decisions": self.decisions}

    def run_partition(self, answer=None, candidates=None, **kwargs):
        return partition_result(self.answer if answer is None else answer,
            self.candidates if candidates is None else candidates, self.rows,
            validate_task=lambda raw: Analysis.validate({"tasks": [raw]}, self.rows)[0], **kwargs)

    def test_invalid_completion_does_not_block_other_tasks_or_become_retained(self):
        self.decisions[0] = decision(outcome="fulfilled", index=None)
        tasks, changes, counts = self.run_partition()
        self.assertEqual(tasks, self.tasks[1:])
        self.assertEqual(changes, [])
        self.assertEqual(counts["rejected_candidate_count"], 1)

    def test_invalid_task_source_is_isolated_and_indices_not_shifted_early(self):
        self.tasks[0]["source_message_ids"] = ["outside"]
        result, _, counts = self.run_partition()
        self.assertEqual(result, self.tasks[1:])
        self.assertEqual(counts["rejected_candidate_count"], 1)

    def test_invalid_date_isolated(self):
        self.tasks[1]["due_date"] = "2026-02-30"
        self.assertEqual(self.run_partition()[0], [self.tasks[0], self.tasks[2]])

    def test_missing_known_decision_does_not_drop_other_valid_decisions(self):
        self.answer["candidate_decisions"] = self.decisions[1:]
        self.assertEqual(self.run_partition()[0], self.tasks[1:])

    def test_duplicate_known_decision_quarantines_both_not_first_or_last_wins(self):
        self.decisions.append(dict(self.decisions[0]))
        self.assertEqual(self.run_partition()[0], self.tasks[1:])

    def test_unknown_candidate_is_global_error(self):
        self.decisions.append(decision("C999"))
        with self.assertRaises(ServiceError):
            self.run_partition()

    def test_broken_envelope_is_global_error(self):
        for answer in ({"tasks": self.tasks}, self.answer | {"tasks": None}, self.answer | {"extra": True}):
            with self.subTest(answer=type(answer)), self.assertRaises(ServiceError):
                self.run_partition(answer)

    def test_rejected_shared_output_quarantines_dependents(self):
        self.decisions[0]["source_message_ids"] = []
        self.decisions[1]["task_index"] = 0
        self.tasks[0]["source_message_ids"] = [self.rows[0]["message_id"], self.rows[1]["message_id"]]
        result, _, counts = self.run_partition()
        self.assertEqual(result, [self.tasks[2]])
        self.assertEqual(counts["rejected_candidate_count"], 2)

    def test_same_original_message_does_not_quarantine_independent_actions(self):
        self.candidates[1]["source_message_ids"] = self.candidates[0]["source_message_ids"]
        self.tasks[1]["source_message_ids"] = self.candidates[0]["source_message_ids"]
        self.decisions[1]["source_message_ids"] = self.candidates[0]["source_message_ids"]
        self.decisions[0] = decision(outcome="fulfilled", index=None)
        self.assertEqual(self.run_partition()[0], self.tasks[1:])

    def test_duplicate_tasks_quarantine_entire_duplicate_group(self):
        self.tasks[1] = dict(self.tasks[0])
        self.assertEqual(self.run_partition()[0], [self.tasks[2]])

    def test_orphan_task_without_retained_decision_cannot_publish(self):
        self.decisions[0] = decision(outcome="not_actionable", index=None)
        self.assertEqual(self.run_partition()[0], self.tasks[1:])

    def test_all_candidates_rejected_does_not_mark_batch_success(self):
        self.answer["candidate_decisions"] = []
        with self.assertRaises(ServiceError) as error:
            self.run_partition()
        self.assertEqual(error.exception.code, "discovery_reconciliation")

    def test_valid_completed_candidates_can_publish_zero_new_tasks(self):
        self.answer["candidate_decisions"] = [decision(outcome="fulfilled", index=None,
            refs=[r["message_id"] for r in self.rows[:2]])]
        self.assertEqual(self.run_partition(candidates=self.candidates[:1])[0], [])

    def review_partition(self, *, bad_completion=False, bad_target=False, bad_change=False, conflict=False):
        old = [{"task_id": "T1", "source_message_ids": self.tasks[0]["source_message_ids"]}]
        tasks = [self.tasks[1] | {"existing_task_id": "T1" if conflict else None}]
        refs = [self.rows[0]["message_id"], self.rows[2]["message_id"]]
        decisions = [decision(outcome="fulfilled", index=None, refs=refs[:1] if bad_completion else refs),
                     decision("C2", index=0, refs=self.tasks[1]["source_message_ids"])]
        change = {"task_id": "T1", "kind": "completed", "source_message_ids": refs}
        if bad_target:
            old[0]["source_message_ids"] = self.tasks[1]["source_message_ids"]
        if bad_change:
            change["source_message_ids"] = ["outside"]
        checker = SimpleNamespace(analysis=SimpleNamespace(validate=Analysis.validate))
        answer = {"tasks": tasks, "changes": [change], "candidate_decisions": decisions}
        return partition_result(answer, self.candidates[:2], self.rows, existing=old,
            validate_task=lambda raw: Reanalysis.validate(checker, {"tasks": [raw], "changes": []}, self.rows, old)[0][0],
            validate_change=lambda raw: Reanalysis.validate(checker, {"tasks": [], "changes": [raw]}, self.rows, old)[1][0])

    def test_valid_change_has_matching_candidate_original_target_and_later_evidence(self):
        tasks, changes, counts = self.review_partition()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(changes[0]["task_id"], "T1")
        self.assertEqual(counts["rejected_change_count"], 0)

    def test_rejected_completion_cannot_escape_through_status_changes(self):
        tasks, changes, counts = self.review_partition(bad_completion=True)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(changes, [])
        self.assertEqual(counts, {"rejected_candidate_count": 1, "rejected_change_count": 1})

    def test_valid_completion_of_another_candidate_cannot_change_unrelated_old_task(self):
        self.assertEqual(self.review_partition(bad_target=True)[1], [])

    def test_invalid_status_source_does_not_block_independent_task(self):
        tasks, changes, counts = self.review_partition(bad_change=True)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(changes, [])
        self.assertEqual(counts["rejected_change_count"], 1)

    def test_conflicting_retention_and_completion_for_same_target_quarantine_both(self):
        tasks, changes, counts = self.review_partition(conflict=True)
        self.assertEqual(tasks, [])
        self.assertEqual(changes, [])
        self.assertEqual(counts["rejected_candidate_count"], 1)

    def audit_store(self):
        temp = tempfile.TemporaryDirectory(prefix="partial-discovery-")
        self.addCleanup(temp.cleanup)
        store = Store(temp.name)
        with store.database() as db:
            for r in self.rows:
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (r["message_id"], r["account_id"],
                    r["chat_id"], "stream", r["message_id"], None, None, None, r["sent_at"], r["sent_epoch"],
                    "text", "PRIVATE ORIGINAL BODY", "text_available", 1))
            db.execute("INSERT INTO analysis_runs VALUES(?,?,?,?,?,?,?,?,?)", ("batch", "generated",
                json.dumps([r["message_id"] for r in self.rows]), 1, 1, "running", "now", None, None))
        audit = DiscoveryAudit(store, "batch", lambda: 1000)
        audit.window(0, self.rows, [], self.candidates)
        return store, audit

    def test_partial_audit_keeps_rejection_null_outcome_and_compact_task_indices(self):
        store, audit = self.audit_store()
        self.decisions[0] = decision(outcome="fulfilled", index=None)
        self.run_partition(audit=audit)
        with store.database() as db:
            db.execute("UPDATE analysis_runs SET status='success'")
            records = db.execute("SELECT outcome,task_index FROM discovery_candidates ORDER BY rowid").fetchall()
            self.assertEqual([tuple(r) for r in records], [(None, None), ("retained", 0), ("retained", 1)])
        report = inspect(store.path, "batch")
        self.assertTrue(report["partial_publication"])
        self.assertFalse(report["decision_coverage_complete"])
        self.assertEqual(report["rejected_candidate_count"], 1)
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_rejection_records_multiple_candidates_and_redacts_all_untrusted_metadata(self):
        store, audit = self.audit_store()
        self.decisions[0]["reason"] = ""
        self.decisions[1]["source_message_ids"] = []
        self.run_partition(audit=audit)
        with store.database() as db:
            rejected = [dict(r) for r in db.execute("SELECT * FROM discovery_rejections")]
        self.assertEqual({r["candidate_id"] for r in rejected}, {"C1", "C2"})
        self.assertEqual(set(rejected[0]), {"attempt_id", "candidate_id", "rule", "created_at"})
        self.assertNotIn("PRIVATE", json.dumps(rejected))
        with self.assertRaises(ServiceError):
            audit.rejection("PRIVATE RAW BODY", "C1")
        with self.assertRaises(ServiceError):
            audit.rejection("decision_fields", "C999")

    def test_rejection_write_failure_stops_publication(self):
        store, audit = self.audit_store()
        with store.database() as db:
            db.execute("CREATE TRIGGER deny_rejection BEFORE INSERT ON discovery_rejections BEGIN SELECT RAISE(ABORT,'generated'); END")
        self.decisions[0]["reason"] = ""
        with self.assertRaises(ServiceError) as error:
            self.run_partition(audit=audit)
        self.assertEqual(error.exception.code, "discovery_store_failed")
        with store.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM discovery_candidates WHERE outcome IS NOT NULL").fetchone()[0], 0)

    def test_legacy_v6_migration_preserves_all_tables_and_does_not_backfill_rejections(self):
        temp = tempfile.TemporaryDirectory(prefix="partial-migration-")
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "application.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            for migration in (SCHEMA, MODEL_MIGRATION, AUTOMATIC_MIGRATION, CONTEXT_MIGRATION, REVIEW_MIGRATION, DISCOVERY_MIGRATION):
                db.executescript(migration)
            db.execute("INSERT INTO analysis_runs VALUES('old','a','[]',1,1,'failed','now','then','discovery_reconciliation')")
            db.commit()
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            before = {n: db.execute(f'SELECT * FROM "{n}" ORDER BY rowid').fetchall() for n in names}
        migrated = Store(temp.name)
        with migrated.database() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 12)
            for name, rows in before.items():
                after = [tuple(r) for r in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
                self.assertTrue(set(rows).issubset(after) if name == "settings" else rows == after, name)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM discovery_rejections").fetchone()[0], 0)

    def test_v6_failed_migration_leaves_version_and_tables_unchanged(self):
        temp = tempfile.TemporaryDirectory(prefix="partial-migration-fault-")
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "application.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            for migration in (SCHEMA, MODEL_MIGRATION, AUTOMATIC_MIGRATION, CONTEXT_MIGRATION, REVIEW_MIGRATION, DISCOVERY_MIGRATION):
                db.executescript(migration)
            db.execute("CREATE TABLE discovery_rejections(collision TEXT)")
            db.commit()
        with self.assertRaises(ServiceError):
            Store(temp.name)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM discovery_candidates").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
