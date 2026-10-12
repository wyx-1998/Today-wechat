"""Details and neighbors retain identity and source, using synthetic SQLite only."""

import tempfile
import unittest

from backend.contracts import ServiceError
from backend.demo import CHATS
from backend.analysis import Analysis
from backend.message_identity import identified
from backend.service import LocalService


class MessageDetailTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="today-detail-synthetic-")
        self.addCleanup(folder.cleanup)
        self.service = LocalService(folder.name)
        self.addCleanup(self.service.close)
        self.service.connect_demo()
        for chat in CHATS[:3]:
            self.service.store.update_chat(chat[0], sync_enabled=True, analysis_enabled=False)
        self.service.sync()
        self.items = self.service.store.messages()["items"]

    def detail(self, local_id):
        message = next(item for item in self.items if item["chat_id"] == CHATS[0][0] and item["local_id"] == local_id)
        return self.service.store.message_detail(message["message_id"])

    def test_detail_has_display_name_but_does_not_change_message_identity(self):
        before = self.detail("3")
        self.service.store.update_chat(CHATS[0][0], display_name="合成显示名称", sync_enabled=True, analysis_enabled=False)
        after = self.detail("3")
        self.assertEqual(after["chat_name"], "合成显示名称")
        for key in ("message_id", "account_id", "chat_id", "stream_id", "local_id", "sender_id", "sender_name", "content"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(len(after["content"]), 2800)

    def test_context_retains_source_and_nullable_self_relation_without_cross_chat(self):
        detail = self.detail("3")
        self.assertEqual(len(detail["context"]), 3)
        for neighbor in detail["context"]:
            self.assertEqual(neighbor["account_id"], detail["account_id"])
            self.assertEqual(neighbor["chat_id"], detail["chat_id"])
            self.assertTrue(neighbor["stream_id"])
            self.assertEqual(neighbor["chat_name"], detail["chat_name"])
            self.assertIs(type(neighbor["analysis_eligible"]), bool)
        by_local = {item["local_id"]: item for item in detail["context"]}
        self.assertIs(by_local["2"]["is_self"], True)
        self.assertIsNone(by_local["4"]["is_self"])
        self.assertIsNone(by_local["4"]["sender_name"])
        self.assertIsNone(by_local["5"]["content"])

    def test_detail_does_not_guess_unknown_identity_or_media_body(self):
        unknown = self.detail("4")
        self.assertIsNone(unknown["sender_id"])
        self.assertIsNone(unknown["sender_name"])
        self.assertIsNone(unknown["is_self"])
        image = self.detail("5")
        self.assertEqual(image["content_state"], "type_only")
        self.assertIsNone(image["content"])
        self.assertEqual(image["content_preview"], "")

    def test_detail_cannot_read_stored_message_from_another_current_source(self):
        detail = self.detail("3")
        source = self.service.store.settings()["source"]
        # The second connection is another identity over this owned synthetic root.
        self.service.connect_local(source["root"], authorize=True)
        with self.assertRaises(ServiceError) as caught:
            self.service.store.message_detail(detail["message_id"])
        self.assertEqual(caught.exception.code, "message_missing")

    def historical_unknown(self):
        with self.service.store.database() as db:
            db.execute("UPDATE messages SET is_self=NULL")

    def test_current_relation_interprets_history_without_rewriting_messages_or_tasks(self):
        self.historical_unknown()
        with self.service.store.database() as db:
            account = self.service.store._settings(db)["source"]["account_id"]
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                "owned-protected-task", account, "owned-batch", "用户修改的事项", "unknown", None,
                None, "unknown", "completed", '[]', "2026-10-01T00:00:00+00:00", 8))
            before_messages = [tuple(r) for r in db.execute("SELECT * FROM messages ORDER BY message_id")]
            before_tasks = [tuple(r) for r in db.execute("SELECT * FROM tasks ORDER BY task_id")]
        myself, other = self.detail("2"), self.detail("3")
        self.assertIs(myself["effective_is_self"], True)
        self.assertIs(other["effective_is_self"], False)
        self.assertIsNone(myself["is_self"])
        self.assertIsNone(other["is_self"])
        self.service.store.messages()
        with self.service.store.database() as db:
            self.assertEqual(before_messages, [tuple(r) for r in db.execute("SELECT * FROM messages ORDER BY message_id")])
            self.assertEqual(before_tasks, [tuple(r) for r in db.execute("SELECT * FROM tasks ORDER BY task_id")])

    def test_list_detail_context_and_analysis_use_same_current_identity(self):
        self.historical_unknown()
        source = self.service.store.settings()["source"]
        items = self.service.store.messages(CHATS[0][0])["items"]
        for item in items:
            detail = self.service.store.message_detail(item["message_id"])
            self.assertIs(detail["effective_is_self"], item["effective_is_self"])
            for value in [detail, *detail["context"]]:
                current = identified([value], source)
                self.assertEqual(current[0]["effective_is_self"], value["effective_is_self"])
                if not value["analysis_eligible"]:
                    continue  # Type-only media is not an analysis text input.
                speaker = Analysis.build_preview(current)["messages"][0]["speaker"]
                expected = "本人" if value["effective_is_self"] is True else "他人" if value["effective_is_self"] is False else "身份未知"
                self.assertEqual(speaker, expected)

    def test_missing_sender_still_unknown_despite_old_true(self):
        with self.service.store.database() as db:
            db.execute("UPDATE messages SET is_self=1 WHERE sender_id IS NULL")
        detail = self.detail("4")
        self.assertIs(detail["is_self"], True)
        self.assertIsNone(detail["effective_is_self"])

    def test_changed_config_reinterprets_without_sync_or_nickname_guess(self):
        self.historical_unknown()
        original = self.detail("2")
        with self.service.store.database() as db:
            source = self.service.store._settings(db)["source"]
            self.service.store._set(db, "source", source | {"self_id": "different-generated-id"})
            db.execute("UPDATE messages SET sender_name='matching nickname'")
        detail = self.detail("2")
        self.assertIs(original["effective_is_self"], True)
        self.assertIs(detail["effective_is_self"], False)
        self.assertEqual(original["sender_id"], detail["sender_id"])
        self.assertIsNone(detail["is_self"])

    def test_unconfigured_identity_keeps_original_reader_relation(self):
        with self.service.store.database() as db:
            source = self.service.store._settings(db)["source"]
            self.service.store._set(db, "source", source | {"self_id": None})
        self.assertIs(self.detail("2")["effective_is_self"], True)
        self.assertIs(self.detail("3")["effective_is_self"], False)
        self.assertIsNone(self.detail("4")["effective_is_self"])
