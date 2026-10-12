"""Validate synthetic fixtures and test fault ingredients, not reader delivery."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import sqlite3
import unittest
from unittest.mock import patch

from tests.fixture_support import (
    content_of, database, derive_expectation, eligible_messages, epoch, fixture_environment,
    install_commit_failure, install_write_failure, load_suite, message_table,
    validate_case,
)


def persist_test_transaction(db, messages):
    """A test-only SQLite transaction; it is NOT wired into collector.py."""
    try:
        db.execute("BEGIN")
        for msg in messages:
            db.execute(
                "INSERT OR IGNORE INTO messages(chatroom_id,content,msg_time,local_id,msg_type) VALUES(?,?,?,?,?)",
                (msg["chat_id"], content_of(msg), epoch(msg["sent_at"]), msg["local_id"], msg["type"]),
            )
        for chat_id in {msg["chat_id"] for msg in messages}:
            cursor = str(max(int(msg["local_id"]) for msg in messages if msg["chat_id"] == chat_id))
            db.execute("INSERT OR REPLACE INTO sync_state(chatroom_id,last_local_id) VALUES(?,?)", (chat_id, cursor))
        db.commit()
    except sqlite3.Error:
        db.rollback()
        raise


class OfflineFixtureTest(unittest.TestCase):
    def setUp(self):
        self.cases, self.expected = load_suite()
        for name in ("socket.socket", "socket.create_connection"):
            guard = patch(name, side_effect=AssertionError("Fixture tests must not use the network"))
            guard.start()
            self.addCleanup(guard.stop)


class ManifestTests(OfflineFixtureTest):
    def test_scenario_coverage(self):
        required = {
            "selected_chat", "empty_selection", "seven_day_boundary", "long_text",
            "sender_identity", "same_display_name", "unknown_sender", "repeated_sync",
            "cross_chat_local_id", "missing_table", "missing_content_column", "empty_table",
            "no_new_messages", "write_failure", "commit_failure", "restart_cursor",
        }
        self.assertTrue(required <= set(self.cases))
        self.assertEqual(set(self.cases), set(self.expected))

    def test_expected_results_are_independently_consistent(self):
        required_keys = {"read_labels", "stored_labels", "cursors", "failed_chats", "outcome"}
        for case_id, case in self.cases.items():
            with self.subTest(case=case_id):
                expected = self.expected[case_id]
                self.assertTrue(required_keys <= set(expected))
                self.assertFalse(set(expected) - required_keys - {"content_lengths", "identities", "recovery"})
                actual = derive_expectation(case)
                for key, value in expected.items():
                    if key == "recovery":
                        recovered = deepcopy(case)
                        recovered["fault"] = {"kind": "none"}
                        recovered_result = derive_expectation(recovered)
                        for field, expectation in value.items():
                            self.assertEqual(recovered_result[field], expectation)
                    elif key in {"identities", "content_lengths"}:
                        for label, expectation in value.items():
                            self.assertEqual(actual[key][label], expectation)
                    else:
                        self.assertEqual(actual[key], value)

    def test_seven_day_window_is_fixed_and_timezone_aware(self):
        case = self.cases["seven_day_boundary"]
        self.assertEqual(epoch(case["window"]["end"]) - epoch(case["window"]["start"]), 7 * 86400)
        self.assertEqual([msg["label"] for msg in eligible_messages(case)], ["start", "inside", "last"])
        instant = datetime.fromisoformat(case["window"]["start"]).astimezone(timezone.utc)
        self.assertEqual(epoch(instant.isoformat()), epoch(case["window"]["start"]))

    def test_empty_selection_never_means_all_chats(self):
        self.assertEqual(eligible_messages(self.cases["empty_selection"]), [])

    def test_long_original_is_not_a_truncated_preview(self):
        text = content_of(self.cases["long_text"]["messages"][0])
        self.assertEqual(len(text), 2800)
        self.assertNotEqual(text, text[:2000])

    def test_equal_display_names_do_not_equal_identity(self):
        case = self.cases["same_display_name"]
        people = {p["sender_ref"]: p for p in case["contacts"]}
        a, b = case["messages"]
        self.assertEqual(people[a["sender_ref"]]["display_name"], people[b["sender_ref"]]["display_name"])
        self.assertNotEqual(people[a["sender_ref"]]["sender_id"], people[b["sender_ref"]]["sender_id"])

    def test_unknown_identity_stays_unknown(self):
        result = derive_expectation(self.cases["unknown_sender"])
        self.assertEqual(result["identities"]["unknown"], {"sender_id": None, "is_self": None})

    def test_invalid_inputs_are_rejected(self):
        changes = {
            "not_synthetic": lambda c: c.update(synthetic=False),
            "unknown_chat": lambda c: c.update(selected_chats=["fixture_not_discovered"]),
            "reversed_time": lambda c: c["window"].update(start=c["window"]["end"]),
            "timezone_missing": lambda c: c["window"].update(start="2026-09-24T12:00:00"),
            "duplicate_message": lambda c: c["messages"].append(deepcopy(c["messages"][0])),
            "invalid_text": lambda c: c["messages"][0].update(payload={"repeat": {"unit": "合成", "count": -1}}),
            "cursor_skip": lambda c: c["initial"]["cursors"].update({c["selected_chats"][0]: "99"}),
            "unknown_fault": lambda c: c.update(fault={"kind": "not_a_fault"}),
            "missing_seed": lambda c: c["initial"].update(stored_labels=["missing_label"]),
        }
        for name, change in changes.items():
            with self.subTest(invalid=name):
                case = deepcopy(self.cases["selected_chat"])
                change(case)
                with self.assertRaises(ValueError):
                    validate_case(case)


class SQLiteFixtureTests(OfflineFixtureTest):
    def test_all_source_databases_match_inputs(self):
        for case_id, case in self.cases.items():
            with self.subTest(case=case_id), fixture_environment(case) as paths:
                with database(paths.message_db) as db:
                    for chat in case["chats"]:
                        table = message_table(chat["id"])
                        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
                        if chat["table_state"] == "missing":
                            self.assertIsNone(exists)
                            continue
                        self.assertIsNotNone(exists)
                        columns = [row[1] for row in db.execute(f"PRAGMA table_info([{table}])")]
                        source_messages = [msg for msg in case["messages"] if msg["chat_id"] == chat["id"]]
                        self.assertEqual(db.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0], len(source_messages))
                        if chat["table_state"] == "missing_content":
                            self.assertNotIn("message_content", columns)
                            continue
                        for msg in source_messages:
                            row = db.execute(f"SELECT real_sender_id,message_content,create_time,local_type FROM [{table}] WHERE local_id=?", (int(msg["local_id"]),)).fetchone()
                            self.assertEqual(row, (msg["sender_ref"], content_of(msg), epoch(msg["sent_at"]), msg["type"]))

    def test_name_mapping_and_session_activity_are_available(self):
        case = self.cases["seven_day_boundary"]
        with fixture_environment(case) as paths:
            with database(paths.message_db) as db:
                self.assertEqual(dict(db.execute("SELECT rowid,user_name FROM Name2Id")), {p["sender_ref"]: p["sender_id"] for p in case["contacts"]})
            with database(paths.session_db) as db:
                self.assertEqual(db.execute("SELECT last_timestamp FROM SessionTable WHERE username=?", (case["selected_chats"][0],)).fetchone()[0], epoch(case["messages"][-1]["sent_at"]))

    def test_empty_table_and_missing_table_are_different(self):
        for case_id, exists in [("empty_table", True), ("missing_table", False)]:
            case = self.cases[case_id]
            chat = case["selected_chats"][0 if exists else 1]
            with fixture_environment(case) as paths, database(paths.message_db) as db:
                row = db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (message_table(chat),)).fetchone()
                self.assertEqual(row is not None, exists)

    def test_missing_content_column_really_fails_query(self):
        case = self.cases["missing_content_column"]
        with fixture_environment(case) as paths, database(paths.message_db) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute(f"SELECT message_content FROM [{message_table(case['selected_chats'][1])}]")

    def test_initial_successful_progress_survives_reopen(self):
        case = self.cases["restart_cursor"]
        with fixture_environment(case) as paths:
            with database(paths.collector_db) as db:
                self.assertEqual(db.execute("SELECT local_id FROM messages").fetchall(), [("1",)])
            with database(paths.collector_db) as reopened:
                self.assertEqual(dict(reopened.execute("SELECT chatroom_id,last_local_id FROM sync_state")), case["initial"]["cursors"])

    def test_two_chats_with_same_local_id_remain_two_sources(self):
        case = self.cases["cross_chat_local_id"]
        with fixture_environment(case) as paths, database(paths.collector_db) as db:
            persist_test_transaction(db, eligible_messages(case))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            self.assertEqual(len(set(db.execute("SELECT chatroom_id,local_id FROM messages"))), 2)

    def test_fixture_replay_does_not_duplicate_test_storage(self):
        case = self.cases["repeated_sync"]
        with fixture_environment(case) as paths, database(paths.collector_db) as db:
            for _ in range(case["repeat_sync"]):
                persist_test_transaction(db, eligible_messages(case))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], len(self.expected[case["id"]]["stored_labels"]))
            self.assertEqual(dict(db.execute("SELECT chatroom_id,last_local_id FROM sync_state")), self.expected[case["id"]]["cursors"])

    def test_real_sqlite_write_and_commit_failures_can_recover(self):
        for case_id, install in [("write_failure", install_write_failure), ("commit_failure", install_commit_failure)]:
            case = self.cases[case_id]
            with self.subTest(fault=case_id), fixture_environment(case) as paths:
                db = sqlite3.connect(paths.collector_db)
                try:
                    install(db)
                    with self.assertRaises(sqlite3.DatabaseError):
                        persist_test_transaction(db, eligible_messages(case))
                    with database(paths.collector_db) as reopened:
                        self.assertEqual(reopened.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
                        self.assertEqual(reopened.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0], 0)
                    db.set_authorizer(None)
                    db.execute("DROP TRIGGER IF EXISTS temp.fixture_write_failure")
                    persist_test_transaction(db, eligible_messages(case))
                    with database(paths.collector_db) as reopened:
                        self.assertEqual(reopened.execute("SELECT COUNT(*) FROM messages").fetchone()[0], len(self.expected[case_id]["recovery"]["stored_labels"]))
                        self.assertEqual(dict(reopened.execute("SELECT chatroom_id,last_local_id FROM sync_state")), self.expected[case_id]["recovery"]["cursors"])
                finally:
                    db.close()

    def test_test_storage_never_modifies_source_database(self):
        case = self.cases["repeated_sync"]
        with fixture_environment(case) as paths:
            before = hashlib.sha256(paths.message_db.read_bytes()).hexdigest()
            with database(paths.collector_db) as db:
                persist_test_transaction(db, eligible_messages(case))
            self.assertEqual(hashlib.sha256(paths.message_db.read_bytes()).hexdigest(), before)

    def test_temp_fixture_cleanup_on_success_and_failure(self):
        case = self.cases["selected_chat"]
        with fixture_environment(case) as paths:
            root = paths.root
            self.assertTrue(root.exists())
            self.assertTrue(all(p.is_relative_to(root) for p in (paths.message_db, paths.collector_db, paths.session_db, paths.contact_db)))
        self.assertFalse(root.exists())
        with self.assertRaisesRegex(RuntimeError, "synthetic test exit"):
            with fixture_environment(case) as paths:
                failed_root = paths.root
                raise RuntimeError("synthetic test exit")
        self.assertFalse(failed_root.exists())


if __name__ == "__main__":
    unittest.main()
