"""The GUI scenario controller only modifies its own synthetic databases."""

from contextlib import closing
import hashlib
import json
import shutil
import sqlite3
import unittest

from backend.service import LocalService
from backend.demo import CHATS
from tests.desktop_scenarios import MARKER, create, status, validate, set_chat_fault
from backend.contracts import ServiceError


class DesktopScenarioTests(unittest.TestCase):
    def owned(self):
        root = create()
        self.addCleanup(shutil.rmtree, root)
        return root

    def test_fault_does_not_commit_and_same_reader_recovers(self):
        root = self.owned()
        self.assertEqual(validate(root), root)
        service = LocalService(root)
        self.addCleanup(service.close)
        service.resume()
        service.sync()
        failed = status(root)
        self.assertEqual(failed["messages"], 0)
        self.assertEqual(failed["cursors"], [])
        self.assertEqual(failed["last_sync"]["errors"][0]["code"], "storage_failed")
        with closing(sqlite3.connect(root / "application.sqlite3")) as db:
            db.execute("DROP TRIGGER gui_check_deny_message")
            db.commit()
        service.sync()
        restored = status(root)
        self.assertEqual(restored["messages"], 4)
        self.assertEqual(restored["cursors"], [["demo_project@chatroom", "5"]])
        self.assertFalse(restored["fault_active"])

    def test_local_connection_is_independent_and_preserves_unknown_self(self):
        root = self.owned()
        with closing(sqlite3.connect(root / "application.sqlite3")) as db:
            db.execute("DROP TRIGGER gui_check_deny_message")
            db.commit()
        source = root / "demo-source-v1"
        hashes = {path: hashlib.sha256(path.read_bytes()).digest() for path in source.rglob("*.db")}
        service = LocalService(root)
        self.addCleanup(service.close)
        service.resume()
        self.assertEqual(service.sync()["inserted"], 4)
        connected = service.dispatch("source.local", {"root": str(source), "authorize": True})
        self.assertEqual(connected["source"]["mode"], "local")
        self.assertIsNone(connected["source"]["self_id"])
        self.assertTrue(connected["source"]["account_id"].startswith("local-"))
        self.assertFalse(any(chat["sync_enabled"] for chat in connected["chats"]))
        self.assertEqual(status(root)["messages"], 0)
        self.assertEqual(status(root)["stored_messages_all_accounts"], 4)
        self.assertEqual(status(root)["cursors"], [])
        service.store.update_chat(CHATS[0][0], sync_enabled=True, analysis_enabled=False)
        service.resume()
        self.assertEqual(service.sync()["inserted"], 4)
        items = service.store.messages()["items"]
        self.assertEqual(len(items), 4)
        self.assertTrue(all(item["account_id"] == connected["source"]["account_id"] for item in items))
        self.assertTrue(all(item["is_self"] is None for item in items))
        self.assertEqual(max(len(item["content"] or "") for item in items), 2800)
        self.assertEqual(service.sync()["inserted"], 0)
        self.assertEqual(status(root)["selected_chats"], [CHATS[0][0]])
        self.assertEqual(status(root)["stored_messages_all_accounts"], 8)
        self.assertEqual(hashes, {path: hashlib.sha256(path.read_bytes()).digest() for path in hashes})
        service.close()
        restored = LocalService(root)
        self.addCleanup(restored.close)
        self.assertEqual(restored.state()["source"], connected["source"])
        self.assertEqual(restored.state()["message_count"], 4)

    def test_chat_fault_preserves_config_and_recovers_only_in_owned_directory(self):
        root = self.owned()
        service = LocalService(root)
        self.addCleanup(service.close)
        previous, revision = service.store.chats(), service.store.settings()["revision"]
        set_chat_fault(root, True)
        self.assertTrue(status(root)["chat_fault_active"])
        with self.assertRaises(ServiceError) as failure:
            service.dispatch("chat.save", {"chat_id": CHATS[0][0], "sync_enabled": True,
                                           "analysis_enabled": False, "display_name": "合成保存重试"})
        self.assertEqual(failure.exception.code, "storage_failed")
        self.assertEqual(service.store.chats(), previous)
        self.assertEqual(service.store.settings()["revision"], revision)
        set_chat_fault(root, False)
        self.assertFalse(status(root)["chat_fault_active"])
        service.dispatch("chat.save", {"chat_id": CHATS[0][0], "sync_enabled": True,
                                      "analysis_enabled": False, "display_name": "合成保存重试"})
        chat = next(c for c in service.store.chats() if c["chat_id"] == CHATS[0][0])
        self.assertEqual(chat["display_name"], "合成保存重试")
        self.assertTrue(chat["sync_enabled"])
        (root / MARKER).write_text(json.dumps({"synthetic": False}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ownership"):
            set_chat_fault(root, True)

    def test_ownership_requires_exact_marker_and_temporary_parent(self):
        root = self.owned()
        marker = root / MARKER
        marker.write_text(json.dumps({"synthetic": False}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ownership"):
            validate(root)
        child = root / "wechat-today-gui-check-nested"
        child.mkdir()
        with self.assertRaisesRegex(ValueError, "temporary directories"):
            validate(child)

    def test_linked_database_is_rejected_without_touching_target(self):
        root = self.owned()
        app_db = root / "application.sqlite3"
        target = root / "owned-backup.sqlite3"
        app_db.rename(target)
        before = target.read_bytes()
        app_db.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "Linked databases"):
            validate(root)
        self.assertEqual(target.read_bytes(), before)
