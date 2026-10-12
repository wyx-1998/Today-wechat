"""Application integration tests use only owned synthetic databases."""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from backend.contracts import ReadScope, ServiceError, stable_id
from backend.demo import CHATS
from backend.service import LocalService
from backend.storage import Store


class LocalServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="today-service-synthetic-")
        self.addCleanup(self.temporary.cleanup)
        self.service = LocalService(self.temporary.name)
        self.addCleanup(self.service.close)
        self.service.connect_demo()

    def select(self, index, enabled=True):
        self.service.store.update_chat(CHATS[index][0], sync_enabled=enabled, analysis_enabled=False)

    def batch(self):
        self.select(0)
        settings = self.service.store.settings()
        reader = self.service._reader()
        scope = ReadScope(reader.account_id, (CHATS[0][0],), settings["time_start"], datetime.now().astimezone().isoformat())
        batch = reader.read_batch(scope, scope.chat_ids[0], reader.streams(scope.chat_ids[0])[0])
        key = stable_id(float(datetime.fromisoformat(scope.time_start).timestamp()))
        return settings, scope, batch, key

    def test_default_no_selection_no_network(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            self.assertEqual(len(self.service.state()["chats"]), 4)
            self.assertTrue(all(not c["sync_enabled"] for c in self.service.state()["chats"]))
            self.assertEqual(self.service.sync()["outcome"], "no_selection")
            self.assertEqual(self.service.state()["message_count"], 0)

    def test_full_text_identity_range_and_source_read_only(self):
        source = self.service._reader().root / "message/message_0.db"
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        self.select(0)
        report = self.service.sync()
        self.assertEqual(report["outcome"], "success")
        self.assertEqual(report["inserted"], 4)
        messages = self.service.store.messages()["items"]
        long = next(m for m in messages if m["local_id"] == "3")
        self.assertEqual(len(long["content"]), 2800)
        self.assertEqual(len(self.service.store.message_detail(long["message_id"])["content"]), 2800)
        unknown = next(m for m in messages if m["local_id"] == "4")
        self.assertIsNone(unknown["sender_name"])
        self.assertIsNone(unknown["is_self"])
        self.assertEqual({m["local_id"] for m in messages}, {"2", "3", "4", "5"})
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_idempotence_restart_and_incremental_sync(self):
        self.select(0)
        self.assertEqual(self.service.sync()["inserted"], 4)
        self.assertEqual(self.service.sync()["inserted"], 0)
        restarted = LocalService(self.temporary.name)
        self.addCleanup(restarted.close)
        self.assertTrue(next(c for c in restarted.state()["chats"] if c["chat_id"] == CHATS[0][0])["sync_enabled"])
        self.assertEqual(restarted.sync()["inserted"], 0)
        restarted.dispatch("demo.append", {})
        self.assertEqual(restarted.sync()["inserted"], 1)
        self.assertEqual(restarted.state()["message_count"], 5)

    def test_pause_resume_persists_and_never_imports_while_paused(self):
        self.select(0)
        self.service.sync()
        self.service.pause()
        self.service.dispatch("demo.append", {})
        with self.assertRaises(ServiceError) as caught:
            self.service.sync()
        self.assertEqual(caught.exception.code, "sync_paused")
        self.assertEqual(Store(self.temporary.name).settings()["paused"], True)
        self.assertEqual(self.service.state()["message_count"], 4)
        self.service.resume()
        self.assertEqual(self.service.sync()["inserted"], 1)

    def test_partial_failure_not_no_messages(self):
        for index in range(4):
            self.select(index)
        report = self.service.sync()
        self.assertEqual(report["outcome"], "partial")
        self.assertEqual(report["inserted"], 7)
        self.assertEqual(len(report["completed_chats"]), 3)
        self.assertEqual(report["errors"][0]["code"], "missing_table")
        self.assertEqual(Store(self.temporary.name).settings()["last_sync"]["outcome"], "partial")

    def test_tags_and_custom_name_persist_without_granting_access(self):
        tag = self.service.store.save_tag("工作")
        self.service.store.update_chat(CHATS[0][0], sync_enabled=False, analysis_enabled=False, display_name="演示项目", tag_ids=[tag])
        self.service.store.save_tag("重点", tag)
        state = LocalService(self.temporary.name)
        self.addCleanup(state.close)
        chat = next(c for c in state.state()["chats"] if c["chat_id"] == CHATS[0][0])
        self.assertEqual(chat["display_name"], "演示项目")
        self.assertEqual(chat["tags"][0]["name"], "重点")
        self.assertFalse(chat["sync_enabled"])
        state.store.delete_tag(tag)
        self.assertEqual(next(c for c in state.state()["chats"] if c["chat_id"] == CHATS[0][0])["tags"], [])

    def test_last_sync_is_current_account_only_without_deleting_history(self):
        self.select(0)
        self.service.sync()
        store = self.service.store
        original = store.settings()
        store.configure(original["source"], original["time_start"], [])
        self.assertEqual(store.settings()["last_sync"], original["last_sync"])
        other = dict(original["source"], account_id="another-synthetic-account")
        store.configure(other, original["time_start"], [])
        self.assertIsNone(store.settings()["last_sync"])
        with store.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sync_runs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 4)

    def test_invalid_tags_leave_existing_settings_untouched(self):
        self.select(0)
        before = self.service.store.chats()
        with self.assertRaises(ServiceError):
            self.service.store.update_chat(CHATS[0][0], sync_enabled=False, analysis_enabled=True, tag_ids=["missing"])
        self.assertEqual(before, self.service.store.chats())

    def test_atomic_rollback_on_write_and_commit_fault(self):
        settings, scope, batch, key = self.batch()
        for fault in ("write", "commit"):
            with self.subTest(fault=fault), self.assertRaises(sqlite3.Error):
                self.service.store.commit_batch(scope, batch, key, settings["revision"], fault=fault)
            self.assertEqual(self.service.store.messages()["total"], 0)
            self.assertEqual(self.service.store.cursor(scope.account_id, batch.chat_id, batch.stream_id, key), "0")

    def test_actual_sqlite_commit_denial_rolls_back_message_and_cursor(self):
        settings, scope, batch, key = self.batch()
        store = self.service.store

        @contextmanager
        def denied_database():
            db = sqlite3.connect(store.path)
            db.row_factory = sqlite3.Row
            db.set_authorizer(lambda action, first, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT" else sqlite3.SQLITE_OK)
            try:
                yield db
            finally:
                db.rollback()
                db.close()

        with patch.object(store, "database", denied_database), self.assertRaises(sqlite3.Error):
            store.commit_batch(scope, batch, key, settings["revision"])
        self.assertEqual(store.messages()["total"], 0)
        self.assertEqual(store.cursor(scope.account_id, batch.chat_id, batch.stream_id, key), "0")

    def test_scope_change_or_cancel_prevents_commit(self):
        settings, scope, batch, key = self.batch()
        self.select(0, False)
        with self.assertRaises(ServiceError):
            self.service.store.commit_batch(scope, batch, key, settings["revision"])
        self.assertEqual(self.service.store.messages()["total"], 0)
        self.select(0)
        with self.assertRaises(ServiceError):
            self.service.store.commit_batch(scope, batch, key, self.service.store.settings()["revision"], cancelled=lambda: True)
        self.assertEqual(self.service.store.cursor(scope.account_id, batch.chat_id, batch.stream_id, key), "0")

    def test_range_expansion_rescans_without_duplicates_and_shrink_keeps_history(self):
        self.select(0)
        self.service.sync()
        start = datetime.fromisoformat(self.service.store.settings()["time_start"]).timestamp()
        from backend.contracts import iso_time
        self.service.store.set_range(iso_time(start - 2 * 86400))
        self.assertEqual(self.service.sync()["inserted"], 1)
        self.assertEqual(self.service.state()["message_count"], 5)
        self.service.store.set_range(iso_time(start + 86400))
        self.assertEqual(self.service.sync()["inserted"], 0)
        self.assertEqual(self.service.state()["message_count"], 5)

    def test_local_source_requires_explicit_authorization(self):
        with patch("backend.service.SourceReader") as reader, self.assertRaises(ServiceError) as caught:
            self.service.connect_local("/a/private/path", authorize=False)
        reader.assert_not_called()
        self.assertEqual(caught.exception.code, "authorization_required")

    def test_scheduler_and_close_stop_future_sync(self):
        self.select(0)
        self.service.interval = 0.02
        self.service.start()
        deadline = time.monotonic() + 2
        while self.service.state()["message_count"] != 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.service.state()["message_count"], 4)
        self.service.close()
        # Simulate the independent source changing after the app has quit.
        from backend.demo import append_demo
        append_demo(self.service.store.directory, self.service.store.settings()["source"])
        time.sleep(0.06)
        self.assertEqual(self.service.state()["message_count"], 4)

    def test_schema_version_mismatch_does_not_overwrite(self):
        with self.service.store.database() as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(ServiceError):
            Store(self.temporary.name)
        with self.service.store.database() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_stdio_protocol_and_eof_shutdown(self):
        with tempfile.TemporaryDirectory(prefix="today-rpc-synthetic-") as data:
            worker = subprocess.Popen([sys.executable, "-m", "backend", "--data-dir", data], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=Path(__file__).resolve().parents[1], env={**os.environ, "PYTHONUNBUFFERED": "1"})
            try:
                def request(command, params=None):
                    worker.stdin.write(json.dumps({"version": 1, "id": command, "command": command, "params": params or {}}) + "\n")
                    worker.stdin.flush()
                    return json.loads(worker.stdout.readline())
                self.assertEqual(request("state")["result"]["source"], None)
                self.assertEqual(request("source.demo")["result"]["message_count"], 0)
                self.assertEqual(request("arbitrary.sql", {"sql": "DROP TABLE messages"})["error"]["code"], "invalid_command")
                self.assertEqual(request("chat.save")["error"]["code"], "invalid_parameters")
                worker.stdin.close()
                self.assertEqual(worker.wait(timeout=5), 0)
                self.assertEqual(worker.stdout.read(), "")
                self.assertEqual(worker.stderr.read(), "")
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait()
                for pipe in (worker.stdin, worker.stdout, worker.stderr):
                    pipe.close()


if __name__ == "__main__":
    unittest.main()
