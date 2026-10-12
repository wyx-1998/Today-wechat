"""Self-ownership confirmation uses isolated local data; no provider requests."""
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import unittest
from unittest.mock import patch

from backend.contracts import ServiceError, iso_time
from backend.demo import CHATS
from backend.service import LocalService


class TaskClaimTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="today-task-claim-")
        self.addCleanup(self.root.cleanup)
        self.service = LocalService(self.root.name)
        self.addCleanup(self.service.close)
        self.service.connect_demo()
        self.service.store.update_chat(CHATS[0][0], sync_enabled=True, analysis_enabled=False)
        self.service.sync()
        self.source = self.service.store.settings()["source"]
        self.refs = [self.service.store.messages()["items"][0]["message_id"]]
        self.seed("claim")

    def seed(self, task_id, owner="unknown", status="needs_confirmation", account=None):
        with self.service.store.database() as db:
            account = account or self.source["account_id"]
            batch = "batch-" + task_id
            stamp = iso_time(self.service.clock())
            db.execute("INSERT INTO analysis_runs VALUES(?,?,?,?,?,?,?,?,?)",
                (batch, account, json.dumps(self.refs), 1, 1, "success", stamp, stamp, None))
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, account, batch, "发送评审资料", owner, None, "2026-10-12", "date",
                 status, json.dumps(self.refs), stamp, 1))
            db.execute("INSERT INTO task_evidence_links VALUES(?,?,?)", (task_id, batch, json.dumps(self.refs)))

    def task(self, task_id="claim"):
        with self.service.store.database() as db:
            return dict(db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())

    def claim(self, task_id="claim", revision=1):
        return self.service.dispatch("task.claim_self", {"task_id": task_id, "revision": revision})

    def snapshot(self):
        with self.service.store.database() as db:
            return {r[0]: [tuple(row) for row in db.execute('SELECT * FROM "' + r[0] + '" ORDER BY 1')]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='tasks'").fetchall()}

    def test_claim_moves_to_self_and_only_changes_ownership_confirmation(self):
        before, other_tables = self.task(), self.snapshot()
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            page = self.claim()
        current = self.task()
        self.assertEqual(current["owner_type"], "self")
        self.assertEqual(current["status"], "pending")
        self.assertEqual(current["revision"], 2)
        for key in before.keys() - {"owner_type", "owner_id", "status", "revision"}:
            self.assertEqual(current[key], before[key], key)
        self.assertEqual(self.snapshot(), other_tables)
        self.assertEqual(page["items"][0]["owner_type"], "self")

    def test_shared_and_existing_active_states_keep_progress(self):
        for index, status in enumerate(("pending", "todo", "in_progress")):
            task_id = "shared-" + str(index)
            self.seed(task_id, owner="shared", status=status)
            self.claim(task_id)
            self.assertEqual(self.task(task_id)["status"], status)
            self.assertEqual(self.task(task_id)["owner_type"], "self")

    def test_current_self_identifier_is_used_without_rewriting_messages(self):
        with self.service.store.database() as db:
            source = dict(self.source, self_id="confirmed-self")
            self.service.store._set(db, "source", source)
        before = self.snapshot()
        self.claim()
        self.assertEqual(self.task()["owner_id"], "confirmed-self")
        self.assertEqual(self.snapshot(), before)

    def test_stale_and_repeated_actions_do_not_overwrite(self):
        self.claim()
        before = self.task()
        for revision in (1, 2, True, "2", 0):
            with self.subTest(revision=revision), self.assertRaises(ServiceError) as error:
                self.claim(revision=revision)
            self.assertEqual(error.exception.code, "task_revision")
            self.assertEqual(self.task(), before)

    def test_closed_owned_unknown_and_foreign_tasks_are_rejected(self):
        cases = [("self", "pending", None), ("other", "pending", None),
                 ("unknown", "completed", None), ("shared", "ignored", None),
                 ("unknown", "cancelled", None), ("unknown", "deleted", None),
                 ("unknown", "unexpected", None), ("unknown", "pending", "foreign")]
        for index, (owner, status, account) in enumerate(cases):
            task_id = "rejected-" + str(index)
            self.seed(task_id, owner, status, account)
            before = self.task(task_id)
            with self.assertRaises(ServiceError):
                self.claim(task_id)
            self.assertEqual(self.task(task_id), before)
        with self.assertRaises(ServiceError):
            self.claim("missing")

    def test_storage_failure_rolls_back(self):
        before = self.task()
        with self.service.store.database() as db:
            db.execute("CREATE TRIGGER deny_claim BEFORE UPDATE ON tasks BEGIN SELECT RAISE(ABORT,'blocked'); END")
        with self.assertRaises(ServiceError) as error:
            self.claim()
        self.assertEqual(error.exception.code, "storage_failed")
        self.assertEqual(self.task(), before)

    def test_restart_preserves_confirmed_owner(self):
        self.claim()
        self.service.close()
        restarted = LocalService(self.root.name)
        self.addCleanup(restarted.close)
        task = next(t for t in restarted.dispatch("tasks.list", {})["items"] if t["task_id"] == "claim")
        self.assertEqual((task["owner_type"], task["status"], task["revision"]), ("self", "pending", 2))

    def test_concurrent_clicks_commit_once(self):
        def run(_):
            try:
                self.claim()
                return "saved"
            except ServiceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(run, range(2))), ["saved", "task_revision"])
        self.assertEqual(self.task()["revision"], 2)

    def test_new_analysis_proposes_but_does_not_overwrite_confirmed_owner(self):
        existing = self.service.analysis.tasks()["items"]
        self.claim()
        before = self.task()
        with self.service.store.database() as db:
            result = self.service.reanalysis.publish(db, before["batch_id"], self.source["account_id"],
                [{"title": before["title"], "owner_type": "other", "due_date": before["due_at"],
                  "source_message_ids": self.refs, "existing_task_id": "claim"}], [], existing)
        self.assertEqual(result["change_count"], 1)
        self.assertEqual(self.task(), before)
        listed = self.service.analysis.tasks()["items"][0]
        self.assertEqual(listed["changes"][0]["proposed"]["owner_type"], "other")

    def test_existing_status_action_still_works_after_claim(self):
        self.claim()
        self.service.dispatch("task.update", {"task_id": "claim", "revision": 2, "status": "completed"})
        self.assertEqual((self.task()["owner_type"], self.task()["status"], self.task()["revision"]),
                         ("self", "completed", 3))
