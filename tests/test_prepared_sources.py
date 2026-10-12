"""Batch preparation: real SQLite + reader + store, synthetic sources only."""

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.contracts import ReadScope, stable_id, parse_time
from backend.prepared_sources import prepare_sources
from backend.reader import SourceReader
from backend.refresh import RefreshError
from backend.storage import Store
from tests.fixture_support import content_of, fixture_environment, load_suite


class PreparedSourcesTests(unittest.TestCase):
    def setUp(self):
        self.cases, self.expected = load_suite()
        temporary = tempfile.TemporaryDirectory(prefix="wechat-prepared-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)

    def factories(self):
        return {relative: (lambda _: lambda page, number: page)
                for relative in ("message/message_0.db", "contact/contact.db", "session/session.db")}

    def test_full_set_actual_reader_and_store_then_cleanup(self):
        case = self.cases["long_text"]
        with fixture_environment(case) as paths:
            source = paths.message_db.parent.parent
            before = {relative: (source / relative).read_bytes() for relative in self.factories()}
            store = Store(self.work / "application")
            account = "prepared-synthetic"
            start, end = case["window"]["start"], case["window"]["end"]
            with prepare_sources(source, self.work, self.factories(), authorize=True) as prepared:
                self.assertEqual(prepared.root.stat().st_mode & 0o777, 0o700)
                reader = SourceReader(prepared.root, account, case["self_id"])
                store.configure({"mode": "synthetic-prepared", "account_id": account}, start, reader.discover())
                scope = ReadScope(account, tuple(case["selected_chats"]), start, end)
                for chat in scope.chat_ids:
                    store.update_chat(chat, sync_enabled=True, analysis_enabled=False)
                revision = store.settings()["revision"]
                for chat in scope.chat_ids:
                    for stream in reader.streams(chat):
                        batch = reader.read_batch(scope, chat, stream)
                        store.commit_batch(scope, batch, stable_id(parse_time(start)), revision)
                messages = store.messages()["items"]
                expected = [content_of(m) for m in case["messages"] if m["label"] in self.expected[case["id"]]["read_labels"]]
                self.assertEqual(sorted(m["content"] for m in messages), sorted(expected))
                self.assertEqual(max(len(m["content"]) for m in messages), 2800)
                self.assertEqual(set(prepared.source_versions), set(self.factories()) | {r + "-wal" for r in self.factories()})
                with self.assertRaises(TypeError):
                    prepared.source_versions["message/message_0.db"] = None
                ready_path = prepared.root
            self.assertFalse(ready_path.exists())
            self.assertFalse(list(self.work.glob(".wechat-source-set-*")))
            self.assertEqual({r: (source / r).read_bytes() for r in before}, before)
            self.assertEqual(Store(store.directory).messages()["total"], len(expected))
            # A new temporary root must not create different message identities.
            saved_ids = {m["message_id"] for m in messages}
            with prepare_sources(source, self.work, self.factories(), authorize=True) as prepared:
                reader = SourceReader(prepared.root, account, case["self_id"])
                for chat in scope.chat_ids:
                    for stream in reader.streams(chat):
                        batch = reader.read_batch(scope, chat, stream)
                        self.assertEqual(store.commit_batch(scope, batch, stable_id(parse_time(start)), revision), 0)
            self.assertEqual({m["message_id"] for m in store.messages()["items"]}, saved_ids)

    def test_later_db_failure_exposes_no_set_and_does_not_write_store(self):
        case = self.cases["selected_chat"]
        with fixture_environment(case) as paths:
            store = Store(self.work / "application")
            factories = self.factories()
            def fail(_):
                raise RefreshError("page_authentication_failed")
            factories["session/session.db"] = fail
            reached_reader = False
            with self.assertRaisesRegex(RefreshError, "page_authentication_failed"):
                with prepare_sources(paths.message_db.parent.parent, self.work, factories, authorize=True):
                    reached_reader = True
            self.assertFalse(reached_reader)
            self.assertEqual(store.messages()["total"], 0)
            with store.database() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM cursors").fetchone()[0], 0)
            self.assertFalse(list(self.work.glob(".wechat-source-set-*")))

    def test_live_changes_during_decode_do_not_invalidate_captured_set(self):
        with fixture_environment(self.cases["selected_chat"]) as paths:
            before = paths.contact_db.read_bytes()
            factories = self.factories()
            def later(snapshot):
                self.assertNotEqual(snapshot.parent.parent, paths.message_db.parent.parent)
                value = paths.contact_db.stat()
                os.utime(paths.contact_db, ns=(value.st_atime_ns, value.st_mtime_ns + 1000000))
                return lambda page, number: page
            factories["session/session.db"] = later
            with prepare_sources(paths.message_db.parent.parent, self.work, factories, authorize=True) as prepared:
                self.assertEqual((prepared.root / "contact/contact.db").read_bytes(), before)
                self.assertNotEqual(prepared.source_versions["contact/contact.db"][3], paths.contact_db.stat().st_mtime_ns)
                self.assertEqual(prepared.metrics["captured_databases"], 3)
            self.assertFalse(list(self.work.glob(".wechat-source-set-*")))

    def test_auxiliary_input_change_and_cancel_preserve_other_work_files(self):
        with fixture_environment(self.cases["selected_chat"]) as paths:
            retained = self.work / "previous-valid.db"
            retained.write_bytes(paths.message_db.read_bytes())
            old = retained.read_bytes()
            guarded = False
            factories = self.factories()
            def change_input(_):
                nonlocal guarded
                guarded = True
                return lambda page, number: page
            factories["session/session.db"] = change_input
            def guard():
                if guarded:
                    raise RefreshError("key_file_changed")
            with self.assertRaisesRegex(RefreshError, "key_file_changed"):
                with prepare_sources(paths.message_db.parent.parent, self.work, factories, authorize=True, input_guard=guard):
                    self.fail("changed key input exposed")
            with self.assertRaisesRegex(RefreshError, "cancelled"):
                with prepare_sources(paths.message_db.parent.parent, self.work, self.factories(), authorize=True, cancelled=lambda: True):
                    self.fail("cancelled set exposed")
            cancellation = False
            def cancel_after_earlier_copy(_):
                nonlocal cancellation
                cancellation = True
                return lambda page, number: page
            factories["session/session.db"] = cancel_after_earlier_copy
            with self.assertRaisesRegex(RefreshError, "cancelled"):
                with prepare_sources(paths.message_db.parent.parent, self.work, factories, authorize=True, cancelled=lambda: cancellation):
                    self.fail("partially prepared cancelled set exposed")
            self.assertEqual(retained.read_bytes(), old)
            self.assertFalse(list(self.work.glob(".wechat-source-set-*")))

    def test_unauthorized_and_invalid_entries_do_not_touch_sources(self):
        with patch("backend.prepared_sources.Path.lstat", side_effect=AssertionError("must not inspect private source")):
            for authorize in (False, None, 1, "true"):
                with self.assertRaisesRegex(RefreshError, "authorization_required"):
                    with prepare_sources("/unused-private", self.work, self.factories(), authorize=authorize):
                        self.fail("unauthorized source")
            for relative in ("../private.db", "/private.db", "message/../private.db", "message/./a.db", "message/a.db-wal", "message/sub/a.db", "private/a.db", "message\\a.db"):
                with self.subTest(relative=relative), self.assertRaisesRegex(RefreshError, "invalid_source_set"):
                    with prepare_sources("/unused-private", self.work, {relative: lambda _: None}, authorize=True):
                        self.fail("invalid source set")
            for factories in ({}, None, [], {"message/a.db": None}):
                with self.assertRaisesRegex(RefreshError, "invalid_source_set"):
                    with prepare_sources("/unused-private", self.work, factories, authorize=True):
                        self.fail("invalid decoder factories")

    def test_linked_directory_and_missing_db_do_not_invoke_decoders(self):
        with fixture_environment(self.cases["selected_chat"]) as paths:
            source = paths.message_db.parent.parent
            callback = Mock(return_value=lambda page, _: page)
            linked = self.work / "linked-source"
            linked.symlink_to(source, target_is_directory=True)
            with self.assertRaisesRegex(RefreshError, "invalid_source_directory"):
                with prepare_sources(linked, self.work, {"message/message_0.db": callback}, authorize=True):
                    self.fail("linked source")
            with self.assertRaisesRegex(RefreshError, "source_missing"):
                with prepare_sources(source, self.work, {"message/missing.db": callback}, authorize=True):
                    self.fail("missing source")
            saved = paths.message_db.with_name("synthetic-saved.db")
            paths.message_db.rename(saved)
            paths.message_db.symlink_to(saved)
            with self.assertRaisesRegex(RefreshError, "non_regular_file"):
                with prepare_sources(source, self.work, {"message/message_0.db": callback}, authorize=True):
                    self.fail("linked database")
            paths.message_db.unlink()  # Only the fixture-owned link.
            saved.rename(paths.message_db)
            moved = source / "synthetic-moved-message"
            paths.message_db.parent.rename(moved)
            (source / "message").symlink_to(moved, target_is_directory=True)
            with self.assertRaisesRegex(RefreshError, "non_regular_file"):
                with prepare_sources(source, self.work, {"message/message_0.db": callback}, authorize=True):
                    self.fail("linked child directory")
            (source / "message").unlink()
            moved.rename(source / "message")
            callback.assert_not_called()

    def test_reader_failure_cleans_own_candidate_only(self):
        with fixture_environment(self.cases["selected_chat"]) as paths:
            retained = self.work / "keep.txt"
            retained.write_text("synthetic unrelated data")
            with self.assertRaisesRegex(RuntimeError, "synthetic consumer failure"):
                with prepare_sources(paths.message_db.parent.parent, self.work, self.factories(), authorize=True) as prepared:
                    root = prepared.root
                    raise RuntimeError("synthetic consumer failure")
            self.assertFalse(root.exists())
            self.assertEqual(retained.read_text(), "synthetic unrelated data")

    def test_source_work_overlap_is_rejected(self):
        with fixture_environment(self.cases["selected_chat"]) as paths:
            source = paths.message_db.parent.parent
            for working in (source, source / "message", source.parent):
                with self.assertRaisesRegex(RefreshError, "source_work_overlap"):
                    with prepare_sources(source, working, self.factories(), authorize=True):
                        self.fail("overlapping source/work")
