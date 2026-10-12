"""Product reader tests against independently authored synthetic expectations."""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import shutil
import sqlite3
import unittest

from backend.contracts import ReadScope, ServiceError, stable_id
from backend.reader import SourceReader
from tests.fixture_support import database, fixture_environment, load_suite, message_table


class SourceReaderTests(unittest.TestCase):
    def setUp(self):
        self.cases, self.expected = load_suite()

    def reader_and_scope(self, case, paths):
        reader = SourceReader(paths.message_db.parent.parent, "synthetic-account", case["self_id"])
        scope = ReadScope("synthetic-account", tuple(case["selected_chats"]), case["window"]["start"], case["window"]["end"])
        return reader, scope

    def test_all_fixture_reads_match_independent_expected(self):
        for case_id, case in self.cases.items():
            with self.subTest(case=case_id), fixture_environment(case) as paths:
                reader, scope = self.reader_and_scope(case, paths)
                before = hashlib.sha256(paths.message_db.read_bytes()).hexdigest()
                label_lookup = {(m["chat_id"], m["local_id"]): m["label"] for m in case["messages"]}
                actual_labels, errors, messages = [], {}, {}
                for chat_id in scope.chat_ids:
                    try:
                        for stream in reader.streams(chat_id):
                            batch = reader.read_batch(scope, chat_id, stream, case["initial"]["cursors"].get(chat_id, "0"))
                            for message in batch.messages:
                                label = label_lookup[(chat_id, message.local_id)]
                                actual_labels.append(label)
                                messages[label] = message
                    except ServiceError as error:
                        errors[chat_id] = error.code
                expectation = self.expected[case_id]
                self.assertEqual(actual_labels, expectation["read_labels"])
                self.assertEqual(errors, {c: kind for c, kind in expectation["failed_chats"].items() if kind in {"missing_table", "missing_content"}})
                for label, length in expectation.get("content_lengths", {}).items():
                    self.assertEqual(len(messages[label].content), length)
                for label, identity in expectation.get("identities", {}).items():
                    self.assertEqual(messages[label].sender_id, identity["sender_id"])
                    self.assertEqual(messages[label].is_self, identity["is_self"])
                self.assertEqual(hashlib.sha256(paths.message_db.read_bytes()).hexdigest(), before)

    def test_discovery_does_not_select_or_write_messages(self):
        case = self.cases["selected_chat"]
        with fixture_environment(case) as paths:
            reader, _ = self.reader_and_scope(case, paths)
            self.assertEqual({c["chat_id"] for c in reader.discover()}, {c["id"] for c in case["chats"]})
            with database(paths.collector_db) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_rejects_unselected_chat_and_other_account(self):
        case = self.cases["selected_chat"]
        with fixture_environment(case) as paths:
            reader, scope = self.reader_and_scope(case, paths)
            for invalid_scope, chat in [(scope, case["chats"][1]["id"]), (replace(scope, account_id="another-account"), scope.chat_ids[0])]:
                with self.assertRaises(ServiceError) as caught:
                    reader.read_batch(invalid_scope, chat, reader.streams(chat)[0])
                self.assertEqual(caught.exception.code, "out_of_scope")

    def test_stable_ids_repeat_but_accounts_and_streams_are_separate(self):
        case = self.cases["selected_chat"]
        with fixture_environment(case) as paths:
            reader, scope = self.reader_and_scope(case, paths)
            stream = reader.streams(scope.chat_ids[0])[0]
            first = reader.read_batch(scope, scope.chat_ids[0], stream).messages[0]
            again = reader.read_batch(scope, scope.chat_ids[0], stream).messages[0]
            self.assertEqual(first.message_id, again.message_id)
            other = SourceReader(reader.root, "second-account", case["self_id"])
            changed = other.read_batch(replace(scope, account_id="second-account"), scope.chat_ids[0], stream).messages[0]
            self.assertNotEqual(first.message_id, changed.message_id)
            shutil.copyfile(paths.message_db, paths.message_db.with_name("message_1.db"))
            streams = reader.streams(scope.chat_ids[0])
            self.assertEqual(len(streams), 2)
            self.assertNotEqual(reader.read_batch(scope, scope.chat_ids[0], streams[0]).messages[0].message_id, reader.read_batch(scope, scope.chat_ids[0], streams[1]).messages[0].message_id)

    def test_future_lower_id_does_not_get_skipped_by_cursor(self):
        case = self.cases["selected_chat"]
        with fixture_environment(case) as paths:
            chat = case["selected_chats"][0]
            with database(paths.message_db) as db:
                db.execute(f"INSERT INTO [{message_table(chat)}] VALUES(2,2,?,1,0,?)", (int(datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp()), "合成未来消息"))
                db.execute(f"INSERT INTO [{message_table(chat)}] VALUES(3,2,?,1,0,?)", (int(datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp()), "合成乱序消息"))
            reader, scope = self.reader_and_scope(case, paths)
            batch = reader.read_batch(scope, chat, reader.streams(chat)[0])
            self.assertEqual([m.local_id for m in batch.messages], ["1", "3"])
            self.assertEqual(batch.next_cursor, "1")

    def test_supported_business_shards_and_explicit_layout_boundary(self):
        case = self.cases["selected_chat"]
        with fixture_environment(case) as paths:
            reader, scope = self.reader_and_scope(case, paths)
            shutil.copyfile(paths.message_db, paths.message_db.with_name("biz_message_12.db"))
            paths.message_db.with_name("message_fts.db").write_bytes(b"synthetic unsupported index")
            # Even a readable DB with a matching table is outside the declared
            # profile when its filename is not a supported text shard.
            shutil.copyfile(paths.message_db, paths.message_db.with_name("future_layout.db"))
            self.assertEqual({stream[1].name for stream in reader.streams(scope.chat_ids[0])},
                             {"message_0.db", "biz_message_12.db"})

    def test_cancellation_has_no_source_writes(self):
        case = self.cases["selected_chat"]
        with fixture_environment(case) as paths:
            reader, scope = self.reader_and_scope(case, paths)
            with self.assertRaises(ServiceError) as caught:
                reader.read_batch(scope, scope.chat_ids[0], reader.streams(scope.chat_ids[0])[0], cancelled=lambda: True)
            self.assertEqual(caught.exception.code, "cancelled")

    def test_media_is_type_only_not_understood(self):
        case = self.cases["selected_chat"]
        with fixture_environment(case) as paths:
            chat = case["selected_chats"][0]
            with database(paths.message_db) as db:
                db.execute(f"UPDATE [{message_table(chat)}] SET local_type=43")
            reader, scope = self.reader_and_scope(case, paths)
            message = reader.read_batch(scope, chat, reader.streams(chat)[0]).messages[0]
            self.assertEqual(message.message_type, "video")
            self.assertEqual(message.content_state, "type_only")
            self.assertIsNone(message.content)
            self.assertFalse(message.analysis_eligible)

    def test_invalid_utf8_not_silently_treated_as_text(self):
        case = self.cases["selected_chat"]
        with fixture_environment(case) as paths:
            chat = case["selected_chats"][0]
            with database(paths.message_db) as db:
                db.execute(f"UPDATE [{message_table(chat)}] SET message_content=?", (b"\xff\xfe",))
            reader, scope = self.reader_and_scope(case, paths)
            message = reader.read_batch(scope, chat, reader.streams(chat)[0]).messages[0]
            self.assertEqual(message.content_state, "read_failed")
            self.assertIsNone(message.content)

    def test_missing_source_does_not_create_a_database(self):
        case = self.cases["selected_chat"]
        with fixture_environment(case) as paths:
            missing = paths.root / "does-not-exist"
            with self.assertRaises(ServiceError):
                SourceReader(missing, "account")
            self.assertFalse(missing.exists())

    def test_scope_rejects_naive_or_reversed_time(self):
        for start, end in [("2026-10-01T12:00:00", "2026-10-02T12:00:00+08:00"), ("2026-10-02T12:00:00+08:00", "2026-10-01T12:00:00+08:00")]:
            with self.assertRaises(ServiceError):
                ReadScope("account", (), start, end)


if __name__ == "__main__":
    unittest.main()
