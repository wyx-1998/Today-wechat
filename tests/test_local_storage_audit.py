"""Audit never reads private contents or calls an application service."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.storage import Store
from scripts.audit_local_model_storage import audit


class LocalStorageAuditTests(unittest.TestCase):
    def test_plaintext_audit_does_not_expose_key_body_address_or_change_database(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            with store.database() as db:
                db.execute("INSERT INTO model_configs VALUES('text','gemini','PRIVATE_ADDRESS','PRIVATE_MODEL',1,2,?,?,?,'untested',NULL,NULL)", (b"", b"", b""))
                db.execute("INSERT INTO model_local_keys VALUES('text','PRIVATE_LOCAL_KEY',1)")
            before = store.path.read_bytes()
            with patch("backend.service.LocalService", side_effect=AssertionError("must not initialize service")):
                result = audit(store.path)
            self.assertEqual(result["credential_storage"], "sqlite_plaintext")
            self.assertEqual(result["schema_version"], 12)
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(before, store.path.read_bytes())
            self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_version_seven_is_reported_without_schema_migration(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            with store.database() as db:
                db.execute("DROP TABLE model_local_keys")
                db.execute("PRAGMA user_version=7")
            result = audit(store.path)
            self.assertEqual(result["schema_version"], 7)
            self.assertEqual(result["credential_storage"], "not_configured")
            with closing(sqlite3.connect(store.path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)
                self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='model_local_keys'").fetchone())

    def test_missing_database_not_created(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "missing.sqlite3"
            with self.assertRaises(FileNotFoundError):
                audit(path)
            self.assertFalse(path.exists())
