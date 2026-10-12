"""Independent actual AES + LocalService checks, no private WeChat data."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.contracts import ServiceError, iso_time, parse_time
from backend.demo import CHATS, SELF, table_name
from backend.service import LocalService
from tests.encrypted_gui_scenarios import create, append, build_database


class EncryptedServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="encrypted-service-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.fixture = create(temporary.name)
        self.root = Path(temporary.name)
        self.service = LocalService(self.fixture["app_directory"])
        self.addCleanup(self.service.close)
        self.before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root / "encrypted").glob("*/*")}

    def connect(self, wrong=False):
        return self.service.dispatch("source.encrypted", {"root": self.fixture["source"],
            "keys_file": self.fixture["wrong_keys_file" if wrong else "keys_file"], "authorize": True, "self_id": SELF})

    def select(self):
        self.service.store.update_chat(CHATS[0][0], sync_enabled=True, analysis_enabled=False)

    def test_discovery_only_no_messages_and_private_key_not_saved(self):
        state = self.connect()
        self.assertEqual(len(state["chats"]), 4)
        self.assertTrue(all(not chat["sync_enabled"] for chat in state["chats"]))
        self.assertEqual(state["message_count"], 0)
        self.assertEqual(self.service.sync()["outcome"], "no_selection")
        self.assertNotIn("encrypted_sync_checkpoint", self.service.store.settings())
        self.assertFalse(list((self.root / "app").glob(".wechat-source-set-*")))
        with closing(sqlite3.connect(self.service.store.path)) as db:
            persisted = "".join(row[0] for row in db.execute("SELECT value FROM settings"))
        self.assertNotIn(bytes(range(32)).hex(), persisted)
        for path, digest in self.before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_unused_metadata_indexes_are_not_opened_or_required(self):
        from backend.encrypted_source import authenticated_decoder
        extra = self.root / "encrypted/contact/contact_fts.db"
        extra.write_bytes(b"unreadable synthetic vendor index")
        other = self.root / "encrypted/session/session_fts.db"
        other.write_bytes(b"unreadable synthetic session index")
        with patch("backend.encrypted_source.authenticated_decoder", wraps=authenticated_decoder) as decoder:
            state = self.connect()
            self.assertEqual({"/".join(call.args[0].parts[-2:])
                              for call in decoder.call_args_list},
                             {"contact/contact.db", "session/session.db"})
            self.assertTrue(all(call.args[0].is_relative_to((self.root / "app").resolve()) for call in decoder.call_args_list))
        self.assertEqual(len(state["chats"]), 4)
        self.assertEqual(state["message_count"], 0)
        self.assertTrue(all(not chat["sync_enabled"] for chat in state["chats"]))
        self.select()
        self.assertEqual(self.service.sync()["inserted"], 4)
        self.assertEqual(extra.read_bytes(), b"unreadable synthetic vendor index")
        self.assertFalse(list((self.root / "app").glob(".wechat-source-set-*")))

    def test_needed_metadata_database_still_fails_authentication(self):
        keys = Path(self.fixture["keys_file"])
        data = json.loads(keys.read_text())
        data["contact/contact.db"]["enc_key"] = bytes(reversed(range(32))).hex()
        keys.write_text(json.dumps(data))
        with self.assertRaises(ServiceError) as error:
            self.connect()
        self.assertEqual(error.exception.code, "page_authentication_failed")
        self.assertIsNone(self.service.state()["source"])
        self.assertEqual(self.service.state()["message_count"], 0)

    def test_auxiliary_message_databases_do_not_block_supported_shards(self):
        for name in ("weclaw.db", "message_fts.db", "message_resource.db", "media_0.db"):
            (self.root / "encrypted/message" / name).write_bytes(b"synthetic unsupported auxiliary")
        self.connect(); self.select()
        self.assertEqual(self.service.sync()["inserted"], 4)
        self.assertEqual(self.service.sync()["inserted"], 0)
        self.assertFalse(list((self.root / "app").glob(".wechat-source-set-*")))

    def test_missing_business_shard_key_is_not_ignored(self):
        self.connect(); self.select()
        shard = self.root / "encrypted/message/biz_message_12.db"
        shard.write_bytes((self.root / "encrypted/message/message_0.db").read_bytes())
        report = self.service.sync()
        self.assertEqual(report["errors"][0]["code"], "missing_database_key")
        self.assertEqual(report["inserted"], 0)
        with closing(sqlite3.connect(self.service.store.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM cursors").fetchone()[0], 0)

    def test_real_service_wal_range_original_identity_restart_and_repeat(self):
        self.connect(); self.select()
        self.assertEqual(self.service.sync()["inserted"], 4)
        messages = self.service.store.messages()["items"]
        self.assertEqual({m["local_id"] for m in messages}, {"2", "3", "4", "5"})
        long = next(m for m in messages if m["local_id"] == "3")
        self.assertEqual(long["content"], "合成加密。" * 560)
        self.assertEqual(len(long["content"]), 2800)
        self.assertTrue(next(m for m in messages if m["local_id"] == "2")["is_self"])
        self.assertIsNone(next(m for m in messages if m["local_id"] == "4")["sender_id"])
        self.assertEqual(self.service.sync()["inserted"], 0)
        self.service.close()
        reopened = LocalService(self.fixture["app_directory"])
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.sync()["inserted"], 0)
        self.assertEqual(reopened.state()["message_count"], 4)
        self.assertFalse(list((self.root / "app").glob(".wechat-source-set-*")))

    def test_wrong_key_connection_keeps_previous_configuration(self):
        self.service.connect_demo()
        before = self.service.state()
        with self.assertRaises(ServiceError) as error:
            self.connect(wrong=True)
        self.assertEqual(error.exception.code, "page_authentication_failed")
        self.assertEqual(self.service.state(), before)
        self.assertFalse(list((self.root / "app").glob(".wechat-source-set-*")))

    def test_message_failure_does_not_advance_cursor_and_recovers(self):
        self.connect(); self.select()
        path = Path(self.fixture["keys_file"])
        good = path.read_text()
        keys = json.loads(good)
        keys["message/message_0.db"]["enc_key"] = bytes(reversed(range(32))).hex()
        path.write_text(json.dumps(keys))
        report = self.service.sync()
        self.assertEqual(report["outcome"], "partial")
        self.assertEqual(report["errors"][0]["code"], "page_authentication_failed")
        self.assertEqual(report["inserted"], 0)
        with closing(sqlite3.connect(self.service.store.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM cursors").fetchone()[0], 0)
        self.assertEqual(self.service.state()["message_count"], 0)
        path.write_text(good)
        self.assertEqual(self.service.sync()["inserted"], 4)

    def test_missing_shard_key_fails_closed_and_pause_reopen(self):
        self.connect(); self.select()
        path = Path(self.fixture["keys_file"])
        keys = json.loads(path.read_text()); del keys["message/message_0.db"]
        path.write_text(json.dumps(keys))
        report = self.service.sync()
        self.assertEqual(report["errors"][0]["code"], "missing_database_key")
        self.assertEqual(report["inserted"], 0)
        self.service.pause(); self.service.close()
        reopened = LocalService(self.fixture["app_directory"])
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.state()["paused"])
        with self.assertRaises(ServiceError):
            reopened.sync()

    def test_pause_then_new_encrypted_wal_resumes_without_duplicate_ids(self):
        self.connect(); self.select()
        self.assertEqual(self.service.sync()["inserted"], 4)
        self.service.pause()
        self.assertEqual(append(self.root)["local_id"], 6)
        with self.assertRaises(ServiceError):
            self.service.sync()
        self.assertEqual(self.service.state()["message_count"], 4)
        self.service.resume()
        self.assertEqual(self.service.sync()["inserted"], 1)
        self.assertEqual(self.service.sync()["inserted"], 0)
        self.assertEqual(self.service.state()["message_count"], 5)
        self.assertEqual({m["local_id"] for m in self.service.store.messages()["items"]}, {"2", "3", "4", "5", "6"})

    def test_unchanged_success_skips_decoder_across_restart(self):
        self.connect(); self.select()
        report = self.service.sync()
        self.assertFalse(report["preparation"]["skipped"])
        self.assertEqual(report["preparation"]["captured_databases"], 3)
        self.service.close()
        reopened = LocalService(self.fixture["app_directory"])
        self.addCleanup(reopened.close)
        with patch("backend.encrypted_source.authenticated_decoder", side_effect=AssertionError("no decrypt needed")):
            report = reopened.sync()
        self.assertEqual(report["outcome"], "success")
        self.assertTrue(report["preparation"]["skipped"])
        self.assertEqual(report["inserted"], 0)

    def test_reset_cursor_and_deleted_messages_force_fresh_decryption(self):
        self.connect(); self.select(); self.service.sync()
        with self.service.store.database() as db:
            db.execute("DELETE FROM cursors")
        self.assertFalse(self.service.sync()["preparation"]["skipped"])
        with self.service.store.database() as db:
            db.execute("DELETE FROM messages")
            db.execute("DELETE FROM cursors")
        report = self.service.sync()
        self.assertFalse(report["preparation"]["skipped"])
        self.assertEqual(report["inserted"], 4)

    def test_scope_and_identity_and_clock_rollback_force_fresh_read(self):
        self.connect(); self.select(); self.service.sync()
        start = parse_time(self.service.store.settings()["time_start"])
        self.service.store.set_range(iso_time(start - 86400))
        self.assertFalse(self.service.sync()["preparation"]["skipped"])
        with self.service.store.database() as db:
            source = self.service.store._settings(db)["source"]
            source["self_id"] = "another-isolated-identity"
            self.service.store._set(db, "source", source)
        self.assertFalse(self.service.sync()["preparation"]["skipped"])
        end = self.service.store.settings()["encrypted_sync_checkpoint"]["end_epoch"]
        self.service.clock = lambda: end - 10
        self.assertFalse(self.service.sync()["preparation"]["skipped"])

    def test_bad_key_does_not_replace_last_success_or_retry_authentication(self):
        from backend.encrypted_source import authenticated_decoder
        self.connect(); self.select(); self.service.sync()
        before = self.service.store.settings()["encrypted_sync_checkpoint"]
        path = Path(self.fixture["keys_file"])
        good = path.read_text()
        keys = json.loads(good)
        keys["message/message_0.db"]["enc_key"] = bytes(reversed(range(32))).hex()
        path.write_text(json.dumps(keys))
        with patch("backend.encrypted_source.authenticated_decoder", wraps=authenticated_decoder) as decoder:
            report = self.service.sync()
        self.assertEqual(report["outcome"], "partial")
        self.assertEqual(report["errors"][0]["code"], "page_authentication_failed")
        self.assertEqual(sum(call.args[0].name == "message_0.db" for call in decoder.call_args_list), 1)
        self.assertEqual(self.service.store.settings()["encrypted_sync_checkpoint"], before)
        path.write_text(good)
        self.assertFalse(self.service.sync()["preparation"]["skipped"])

    def test_pause_or_scope_change_during_skip_never_reports_success(self):
        from backend.encrypted_source import encrypted_manifest
        self.connect(); self.select(); self.service.sync()
        before = self.service.store.settings()["encrypted_sync_checkpoint"]
        def changed(source):
            result = encrypted_manifest(source)
            self.service.pause()
            self.service.resume()
            return result
        with patch("backend.service.encrypted_manifest", side_effect=changed):
            report = self.service.sync()
        self.assertEqual(report["outcome"], "cancelled")
        self.assertEqual(self.service.store.settings()["encrypted_sync_checkpoint"], before)
        def change_scope(source):
            result = encrypted_manifest(source)
            self.service.store.set_range(iso_time(parse_time(self.service.store.settings()["time_start"]) - 1))
            return result
        with patch("backend.service.encrypted_manifest", side_effect=change_scope):
            self.assertEqual(self.service.sync()["outcome"], "cancelled")

    def test_future_message_revisited_when_clock_advances_without_file_change(self):
        now = int(self.service.clock())
        future = now + 3600
        self.service.clock = lambda: now
        build_database(self.root / "plain/message.db", self.root / "encrypted/message/message_0.db", "",
            f'INSERT INTO "{table_name(CHATS[0][0])}" VALUES(6,2,{future},1,0,\'isolated future message\');')
        self.connect(); self.select()
        self.assertEqual(self.service.sync()["inserted"], 4)
        self.assertEqual(self.service.store.settings()["encrypted_sync_checkpoint"]["deferred_until"], future)
        self.service.clock = lambda: future
        self.assertTrue(self.service.sync()["preparation"]["skipped"])
        self.service.clock = lambda: future + 1
        report = self.service.sync()
        self.assertFalse(report["preparation"]["skipped"])
        self.assertEqual(report["inserted"], 1)
        self.assertTrue(self.service.sync()["preparation"]["skipped"])

    def test_partial_chat_failure_never_establishes_success_checkpoint(self):
        self.connect(); self.select()
        self.service.store.update_chat(CHATS[3][0], sync_enabled=True, analysis_enabled=False)
        report = self.service.sync()
        self.assertEqual(report["outcome"], "partial")
        self.assertNotIn("encrypted_sync_checkpoint", self.service.store.settings())
        self.assertEqual(self.service.state()["message_count"], 4)

    def test_source_write_after_capture_does_not_invalidate_copy_but_forces_next_read(self):
        import os
        from backend.encrypted_source import authenticated_decoder
        self.connect(); self.select()
        original = self.root / "encrypted/contact/contact.db"
        def decode(snapshot, key):
            if snapshot.name == "session.db":
                value = original.stat()
                os.utime(original, ns=(value.st_atime_ns, value.st_mtime_ns + 1000000))
            return authenticated_decoder(snapshot, key)
        with patch("backend.encrypted_source.authenticated_decoder", side_effect=decode):
            self.assertEqual(self.service.sync()["outcome"], "success")
        self.assertFalse(self.service.sync()["preparation"]["skipped"])
        self.assertTrue(self.service.sync()["preparation"]["skipped"])

    def test_scope_change_at_final_record_transaction_cancels_checkpoint(self):
        self.connect(); self.select(); self.service.sync()
        before = self.service.store.settings()["encrypted_sync_checkpoint"]
        record = self.service.store.record_run
        def changed(report, **kwargs):
            self.service.store.set_range(iso_time(parse_time(self.service.store.settings()["time_start"]) - 1))
            return record(report, **kwargs)
        with patch.object(self.service.store, "record_run", side_effect=changed):
            self.assertEqual(self.service.sync()["outcome"], "cancelled")
        self.assertEqual(self.service.store.settings()["encrypted_sync_checkpoint"], before)

    def test_checkpoint_write_failure_rolls_back_report_and_checkpoint_together(self):
        self.connect(); self.select(); self.service.sync()
        before = self.service.store.settings()
        self.service.store.set_range(iso_time(parse_time(before["time_start"]) - 1))
        original_set = self.service.store._set
        def fail(db, key, value):
            if key == "encrypted_sync_checkpoint":
                raise sqlite3.OperationalError("isolated checkpoint failure")
            return original_set(db, key, value)
        with self.service.store.database() as db:
            runs = db.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0]
        with patch.object(self.service.store, "_set", side_effect=fail), self.assertRaises(sqlite3.OperationalError):
            self.service.sync()
        state = self.service.store.settings()
        self.assertEqual(state["encrypted_sync_checkpoint"], before["encrypted_sync_checkpoint"])
        self.assertEqual(state["last_sync"], before["last_sync"])
        with self.service.store.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0], runs)
        self.assertFalse(self.service.sync()["preparation"]["skipped"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
