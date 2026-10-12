"""No Crypto required: authorization, selected key parsing, and state guards."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.contracts import ServiceError
from backend.demo import ensure_demo
from backend.encrypted_source import read_keys, selected_paths
from backend.reader import SourceReader
from backend.refresh import RefreshError, fingerprint
from backend.service import LocalService


class EncryptedSourceContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="encrypted-contract-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, _ = ensure_demo(self.root)
        self.key = self.root / "keys.json"
        self.key.write_text(json.dumps({"message/message_0.db": {"enc_key": "00" * 32}}))
        self.service = LocalService(self.root / "app")
        self.addCleanup(self.service.close)

    def test_authorization_precedes_path_or_key_reads(self):
        with patch("backend.service.selected_paths", side_effect=AssertionError("must not inspect")):
            for authorization in (False, None, 1, "true"):
                with self.assertRaisesRegex(ServiceError, "请先确认"):
                    self.service.connect_encrypted("private-not-read", "private-not-read", authorize=authorization)
        self.assertIsNone(self.service.state()["source"])

    def test_bounded_keys_schema_without_exposing_values(self):
        keys, version = read_keys(self.key)
        self.assertEqual(keys, {"message/message_0.db": bytes(32)})
        self.assertEqual(version, fingerprint(self.key))
        self.key.write_text('{"message/message_0.db":{"enc_key":"' + "SECRET" + '"}}')
        with self.assertRaises(ServiceError) as error:
            read_keys(self.key)
        self.assertEqual(error.exception.code, "invalid_keys_file")
        self.assertNotIn("SECRET", str(error.exception))

    def test_duplicates_paths_and_non_object_inputs_are_rejected(self):
        for payload in ('[]', '{"message/a.db":{},"message/a.db":{}}',
                        json.dumps({"message/../a.db": {"enc_key": "00" * 32}}),
                        json.dumps({"message/a.db": "00" * 32})):
            self.key.write_text(payload)
            with self.assertRaises(ServiceError):
                read_keys(self.key)

    def test_links_and_non_regular_keys_are_rejected(self):
        link = self.root / "linked.json"
        link.symlink_to(self.key)
        with self.assertRaises(ServiceError):
            selected_paths(str(self.source), str(link))
        with self.assertRaises(RefreshError):
            read_keys(self.root)

    def test_key_version_changed_while_reading_is_rejected(self):
        version = fingerprint(self.key)
        with patch("backend.encrypted_source.fingerprint", side_effect=[version, None]):
            with self.assertRaisesRegex(RefreshError, "key_file_changed"):
                read_keys(self.key)

    def test_no_selected_chats_does_not_prepare_or_load_keys(self):
        source = {"mode": "encrypted", "root": str(self.source), "keys_file": str(self.key), "account_id": "synthetic-contract"}
        self.service.store.configure(source, "2026-09-24T00:00:00+00:00", [])
        with patch("backend.service.encrypted_reader", side_effect=AssertionError("no decryption")):
            self.assertEqual(self.service.sync()["outcome"], "no_selection")
        self.assertEqual(self.service.state()["message_count"], 0)

    def test_connection_failure_preserves_source_and_missing_params_reject(self):
        self.service.connect_demo()
        before = self.service.state()

        @contextmanager
        def fail(*args, **kwargs):
            raise ServiceError("page_authentication_failed", "Synthetic failure")
            yield

        with patch.object(self.service, "reader_context", fail):
            with self.assertRaises(ServiceError):
                self.service.dispatch("source.encrypted", {"root": str(self.source), "keys_file": str(self.key), "authorize": True})
        self.assertEqual(self.service.state(), before)
        with self.assertRaises(ServiceError) as missing:
            self.service.dispatch("source.encrypted", {"authorize": True})
        self.assertEqual(missing.exception.code, "invalid_parameters")
