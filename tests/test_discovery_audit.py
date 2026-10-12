"""Local typed diagnostics and lossless migrations, no OS credentials or model."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend.action_discovery import DiscoveryAudit
from backend.contracts import ServiceError
from backend.storage import (Store, SCHEMA, MODEL_MIGRATION, AUTOMATIC_MIGRATION,
                             CONTEXT_MIGRATION, REVIEW_MIGRATION)


class DiscoveryAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="today-discovery-audit-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        with self.store.database() as db:
            db.execute("INSERT INTO analysis_runs VALUES('batch','account','[\"one\"]',1,1,'running','now',NULL,NULL)")

    def candidate(self, cid="C1"):
        return {"candidate_id": cid, "title": "发送资料给 fake@example.invalid", "owner_type": "unknown",
                "due_date": None, "source_message_ids": ["one"]}

    def audit(self):
        return DiscoveryAudit(self.store, "batch", lambda: 1000)

    def test_version_five_migration_preserves_every_existing_table(self):
        root = Path(self.temp.name) / "v5"
        root.mkdir()
        with closing(sqlite3.connect(root / "application.sqlite3")) as db:
            for migration in (SCHEMA, MODEL_MIGRATION, AUTOMATIC_MIGRATION, CONTEXT_MIGRATION, REVIEW_MIGRATION):
                db.executescript(migration)
            db.execute("INSERT INTO settings VALUES('vault_id','\"keep-vault\"')")
            db.execute("INSERT INTO tags VALUES('tag','keep-tag')")
            db.execute("INSERT INTO analysis_runs VALUES('old','account','[]',2,3,'success','now','then',NULL)")
            db.execute("INSERT INTO tasks VALUES('task','account','old','原任务','unknown',NULL,NULL,'unknown','completed','[]','now',9)")
            db.commit()
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            before = {name: db.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall() for name in names}
        migrated = Store(root)
        with migrated.database() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 12)
            for name in names:
                after = [tuple(r) for r in db.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
                if name == "settings":
                    self.assertTrue(set(before[name]).issubset(after))
                else:
                    self.assertEqual(after, before[name], name)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM discovery_attempts").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_window_stores_typed_redacted_candidates_not_message_content(self):
        audit = self.audit()
        audit.window(0, [{"message_id": "one", "content": "PRIVATE RAW BODY"}], [{"message_id": "neighbor"}], [self.candidate()])
        with self.store.database() as db:
            candidate = dict(db.execute("SELECT * FROM discovery_candidates").fetchone())
            self.assertEqual(candidate["title"], "发送资料给 [email]")
            self.assertIsNone(candidate["outcome"])
            all_data = json.dumps([list(r) for r in db.execute("SELECT * FROM discovery_windows")]) + json.dumps(candidate)
            self.assertNotIn("PRIVATE RAW BODY", all_data)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)

    def test_empty_window_distinguishes_no_candidates_from_unprocessed(self):
        audit = self.audit()
        audit.window(0, [{"message_id": "one"}], [], [])
        with self.store.database() as db:
            self.assertEqual(db.execute("SELECT candidate_count FROM discovery_windows").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM discovery_candidates").fetchone()[0], 0)

    def test_decision_redacts_reason_and_preserves_original_source_ids(self):
        audit = self.audit()
        audit.window(0, [{"message_id": "one"}], [], [self.candidate()])
        audit.decisions([{"candidate_id": "C1", "outcome": "fulfilled", "task_index": None,
                         "source_message_ids": ["one", "later"], "reason": "已发送给 fake@example.invalid"}])
        with self.store.database() as db:
            row = dict(db.execute("SELECT * FROM discovery_candidates").fetchone())
            self.assertEqual(row["reason"], "已发送给 [email]")
            self.assertEqual(json.loads(row["decision_source_message_ids"]), ["one", "later"])

    def test_invalid_parent_is_not_recorded(self):
        with self.assertRaises(ServiceError) as error:
            DiscoveryAudit(self.store, "missing", lambda: 1000)
        self.assertEqual(error.exception.code, "discovery_store_failed")

    def test_window_and_candidates_save_atomically(self):
        audit = self.audit()
        with self.assertRaises(ServiceError):
            audit.window(0, [{"message_id": "one"}], [], [self.candidate(), self.candidate()])
        with self.store.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM discovery_windows").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM discovery_candidates").fetchone()[0], 0)

    def test_decision_batch_rolls_back_if_a_candidate_is_missing(self):
        audit = self.audit()
        audit.window(0, [{"message_id": "one"}], [], [self.candidate()])
        decision = {"candidate_id": "C1", "outcome": "retained", "task_index": 0,
                    "source_message_ids": ["one"], "reason": "请求"}
        with self.assertRaises(ServiceError):
            audit.decisions([decision, decision | {"candidate_id": "missing"}])
        with self.store.database() as db:
            self.assertIsNone(db.execute("SELECT outcome FROM discovery_candidates").fetchone()[0])

    def test_multiple_attempts_for_same_parent_have_distinct_ids(self):
        first, second = self.audit(), self.audit()
        self.assertNotEqual(first.attempt_id, second.attempt_id)
        first.window(0, [{"message_id": "one"}], [], [self.candidate()])
        second.window(0, [{"message_id": "one"}], [], [])
        with self.store.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM discovery_attempts").fetchone()[0], 2)

    def test_failed_migration_is_transactional_and_keeps_version_five(self):
        root = Path(self.temp.name) / "collision"
        root.mkdir()
        with closing(sqlite3.connect(root / "application.sqlite3")) as db:
            for migration in (SCHEMA, MODEL_MIGRATION, AUTOMATIC_MIGRATION, CONTEXT_MIGRATION, REVIEW_MIGRATION):
                db.executescript(migration)
            db.execute("CREATE TABLE discovery_windows(collision TEXT)")
            db.commit()
        with self.assertRaises(ServiceError) as error:
            Store(root)
        self.assertEqual(error.exception.code, "migration_failed")
        with closing(sqlite3.connect(root / "application.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='discovery_attempts'").fetchone()[0], 0)
