"""Settings contracts with generated secrets and clients; never real Keychain/network."""

from datetime import datetime, timedelta
from contextlib import redirect_stdout
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.contracts import ServiceError
from backend.credentials import CredentialSession
from backend.demo import CHATS
from backend.service import LocalService
from tests.credentials_smoke import Provider
from tests.model_smoke import FakeClient, KEY, install_legacy_config


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="settings-generated-")
        self.addCleanup(self.temp.cleanup)
        self.provider = Provider()
        self.session = CredentialSession(self.provider)
        self.client = FakeClient()
        self.service = LocalService(self.temp.name, keychain=self.session, model_client=self.client)
        self.addCleanup(self.service.close)

    def save(self):
        return self.service.models.save(base_url="https://generated.invalid", model="generated-flash", api_key=KEY)

    def delete(self, revision):
        return self.service.dispatch("model.delete", {"revision": revision, "confirm": True})

    def snapshots(self, *tables):
        with self.service.store.database() as db:
            return {table: [tuple(r) for r in db.execute("SELECT * FROM " + table + " ORDER BY rowid")] for table in tables}

    def prepare(self):
        self.save(); self.service.connect_demo()
        self.service.store.update_chat(CHATS[0][0], sync_enabled=True, analysis_enabled=True)
        self.service.sync()

    def test_requires_explicit_confirmation_and_non_boolean_current_revision(self):
        config = self.save()
        before = self.snapshots("model_configs", "settings")
        for confirm, revision in ((False, config["revision"]), (1, config["revision"]),
                                  ("true", config["revision"]), (True, True), (True, 0), (True, "1")):
            with self.subTest(confirm=confirm, revision=revision), self.assertRaises(ServiceError) as caught:
                self.service.models.delete(confirm=confirm, revision=revision)
            self.assertEqual(caught.exception.code, "model_delete_confirmation")
        self.assertEqual(before, self.snapshots("model_configs", "settings"))

    def test_stale_or_repeated_delete_does_not_delete_a_different_config(self):
        old = self.save(); newer = self.save()
        with self.assertRaises(ServiceError) as caught:
            self.delete(old["revision"])
        self.assertEqual(caught.exception.code, "model_config_changed")
        self.assertEqual(self.service.models.public()["revision"], newer["revision"])
        self.delete(newer["revision"])
        with self.assertRaises(ServiceError):
            self.delete(newer["revision"])
        self.assertEqual(self.client.calls, [])

    def test_deletion_keeps_messages_tasks_calls_tags_source_and_system_key(self):
        self.prepare()
        self.service.analysis.run(authorize=True, preview_token=self.service.analysis.preview()["preview_token"])
        tables = ("messages", "tasks", "model_calls", "chats", "tags", "chat_tags", "analysis_runs")
        before = self.snapshots(*tables)
        source_before = self.service.store.settings()["source"]
        usage_before = self.service.models.usage()
        key_before, provider_calls = self.provider.value, len(self.provider.calls)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.delete(self.service.models.public()["revision"]), {"deleted": True})
        self.assertIsNone(self.service.models.public())
        self.assertEqual(self.snapshots("model_local_keys")["model_local_keys"], [])
        self.assertEqual(self.snapshots(*tables), before)
        self.assertEqual(self.service.store.settings()["source"], source_before)
        self.assertEqual(self.service.models.usage(), usage_before)
        self.assertEqual((self.provider.value, len(self.provider.calls)), (key_before, provider_calls))
        self.assertEqual(self.session.values, {})
        with self.assertRaises(ServiceError) as caught:
            self.service.models.test(authorize=True)
        self.assertEqual(caught.exception.code, "model_not_configured")
        self.assertEqual(len(self.client.calls), 1)

    def test_deletion_invalidates_continuous_grant_and_cancels_queued_batches(self):
        self.prepare()
        with self.service.store.database() as db:
            source = self.service.store._settings(db)["source"]
            self.service.store._set(db, "source", source | {"mode": "local", "account_id": "generated-account"})
            db.execute("UPDATE chats SET account_id='generated-account'")
        preview = self.service.automatic.preview()
        self.service.automatic.save(authorize=True, preview_token=preview["preview_token"])
        with self.service.store.database() as db:
            grant = db.execute("SELECT grant_id FROM text_grants WHERE status='active'").fetchone()[0]
            db.execute("INSERT INTO text_batches VALUES(?,?,?,?,?,?,?,?,?,?)", (
                "generated-queue", grant, "generated-account", "generated-chat", "[]", "generated-hash",
                "queued", "2026-10-03T00:00:00+00:00", None, None))
        self.delete(self.service.models.public()["revision"])
        with self.service.store.database() as db:
            self.assertEqual(tuple(db.execute("SELECT status,reason FROM text_grants").fetchone()), ("invalid", "model_deleted"))
            self.assertEqual(tuple(db.execute("SELECT status,error_code FROM text_batches").fetchone()), ("cancelled", "model_deleted"))
        self.assertEqual(self.client.calls, [])

    def test_versions_survive_delete_reopen_readd_and_enable(self):
        old = self.save(); self.delete(old["revision"])
        self.service.close()
        reopened = LocalService(self.temp.name, keychain=CredentialSession(self.provider), model_client=self.client)
        self.addCleanup(reopened.close)
        newer = reopened.models.save(base_url=old["base_url"], model=old["model"], api_key=KEY)
        self.assertGreater(newer["revision"], old["revision"])
        disabled = reopened.models.enable(enabled=False)
        self.assertGreater(disabled["revision"], newer["revision"])
        reopened.models.delete(revision=disabled["revision"], confirm=True)
        next_config = reopened.models.save(base_url=old["base_url"], model=old["model"], api_key=KEY)
        self.assertGreater(next_config["revision"], disabled["revision"])

    def test_old_preview_cannot_upload_after_identical_readd(self):
        self.prepare()
        preview = self.service.analysis.preview()
        self.delete(self.service.models.public()["revision"]); self.save()
        with self.assertRaises(ServiceError) as caught:
            self.service.analysis.run(authorize=True, preview_token=preview["preview_token"])
        self.assertEqual(caught.exception.code, "analysis_preview_changed")
        self.assertEqual(self.client.calls, [])

    def test_sent_request_counts_actual_usage_but_cannot_publish_after_readd(self):
        self.prepare()
        preview = self.service.analysis.preview()
        def replace():
            self.delete(self.service.models.public()["revision"]); self.save()
        self.client.callback = replace
        with self.assertRaises(ServiceError) as caught:
            self.service.analysis.run(authorize=True, preview_token=preview["preview_token"])
        self.assertEqual(caught.exception.code, "analysis_scope_changed")
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual(self.service.models.usage()["today"]["known_total_tokens"], 130)
        self.assertEqual(len(self.client.calls), 1)

    def test_delete_while_unlock_waits_does_not_deadlock_or_send_late_key(self):
        config = self.save(); install_legacy_config(self.service); self.session.invalidate()
        entered, release = threading.Event(), threading.Event()
        def wait():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("generated timeout")
        self.provider.callback = wait
        errors = []
        def request():
            try:
                self.service.models.migrate(revision=config["revision"], confirm=True)
            except ServiceError as error:
                errors.append(error.code)
        worker = threading.Thread(target=request)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.delete(config["revision"])
        finally:
            release.set(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ["credential_session_changed"])
        self.assertEqual(self.session.values, {})
        self.assertEqual(self.client.calls, [])

    def test_failed_delete_rolls_back_config_grant_and_version(self):
        self.prepare()
        before = self.snapshots("model_configs", "text_grants", "settings", "messages", "tasks")
        original_set = self.service.store._set
        def fail(db, key, value):
            if key == "cloud_demo_consent":
                raise sqlite3.OperationalError("generated failure")
            original_set(db, key, value)
        with patch.object(self.service.store, "_set", fail), self.assertRaises(sqlite3.OperationalError):
            self.service.models.delete(revision=self.service.models.public()["revision"], confirm=True)
        self.assertEqual(self.snapshots(*before), before)
        self.assertEqual(self.provider.calls, [])

    def insert_call(self, name, when, *, input=None, output=None, total=None, outcome="success"):
        with self.service.store.database() as db:
            db.execute("INSERT INTO model_calls(call_id,capability,config_revision,purpose,started_at,outcome,input_tokens,output_tokens,total_tokens) VALUES(?,?,?,?,?,?,?,?,?)",
                       (name, "text", 1, "generated", when.isoformat(), outcome, input, output, total))

    def test_usage_missing_fields_independent_zero_real_and_thinking_total_not_derived(self):
        now = datetime.now().astimezone()
        self.service.models.clock = lambda: now.timestamp()
        self.insert_call("a", now, input=12, output=7, total=151)
        self.insert_call("b", now, input=0, output=0, total=0)
        self.insert_call("c", now, input=4, total=None, outcome="failed")
        self.insert_call("d", now, output=2, total=6, outcome="unknown")
        usage = self.service.models.usage()["today"]
        self.assertEqual((usage["known_input_tokens"], usage["known_output_tokens"], usage["known_total_tokens"]), (16, 9, 157))
        self.assertEqual((usage["unknown_input_requests"], usage["unknown_output_requests"], usage["unknown_usage_requests"]), (1, 1, 1))
        self.assertEqual((usage["requests"], usage["success"], usage["failed_or_unknown"]), (4, 2, 2))
        self.assertEqual(self.provider.calls, [])

    def test_empty_periods_unknown_not_zero_and_explicit_zero_not_unknown(self):
        usage = self.service.models.usage()["today"]
        for kind in ("input", "output", "total"):
            self.assertIsNone(usage["known_" + kind + "_tokens"])
        now = datetime.now().astimezone()
        self.insert_call("zero", now, input=0, output=0, total=0)
        usage = self.service.models.usage()["today"]
        for kind in ("input", "output", "total"):
            self.assertEqual(usage["known_" + kind + "_tokens"], 0)
            self.assertEqual(usage["unknown_" + kind + "_requests"], 0)

    def test_local_period_boundaries_and_future_entries_excluded(self):
        now = datetime(2026, 10, 3, 12).astimezone()
        self.service.models.clock = lambda: now.timestamp()
        today = now.replace(hour=0)
        month = now.replace(day=1, hour=0)
        self.insert_call("today-boundary", today, input=1, output=2, total=5)
        self.insert_call("month-boundary", month, input=2, output=3, total=6)
        self.insert_call("older", month - timedelta(seconds=1), input=999, output=999, total=999)
        self.insert_call("previous-day", today - timedelta(seconds=1), input=3, output=4, total=7)
        self.insert_call("future", now + timedelta(seconds=1), input=999, output=999, total=999)
        usage = self.service.models.usage()
        self.assertEqual((usage["today"]["requests"], usage["today"]["known_total_tokens"]), (1, 5))
        self.assertEqual((usage["month"]["requests"], usage["month"]["known_input_tokens"], usage["month"]["known_output_tokens"], usage["month"]["known_total_tokens"]), (3, 6, 9, 18))
        self.assertNotIn(KEY, json.dumps(self.service.state()))

    def test_legacy_migration_preserves_business_data_and_no_later_unlock_even_after_restart(self):
        self.prepare(); install_legacy_config(self.service); self.session.invalidate()
        before = self.snapshots("messages", "tasks", "model_calls", "chats", "text_grants")
        settings = self.service.store.settings()
        revision = self.service.models.public()["revision"]
        converted = self.service.models.migrate(revision=revision, confirm=True)
        self.assertEqual(converted["storage_mode"], "sqlite_plaintext")
        self.assertGreater(converted["revision"], revision)
        self.assertEqual(before, self.snapshots(*before))
        self.assertEqual(self.service.store.settings()["source"], settings["source"])
        self.assertEqual(self.service.store.settings()["paused"], settings["paused"])
        self.assertEqual(self.service.models.row()["ciphertext"], b"")
        self.assertEqual(self.service.models.local_key(), KEY)
        self.assertNotIn(KEY, json.dumps(converted))
        calls = len(self.provider.calls)
        self.provider.failure = ServiceError("keychain_denied", "generated denial")
        self.service.models.test(authorize=True); self.service.models.test(authorize=True)
        self.service.models.enable(enabled=False); self.service.models.enable(enabled=True)
        self.service.models.test(authorize=True)
        self.service.close()
        reopened = LocalService(self.temp.name, keychain=CredentialSession(self.provider), model_client=self.client)
        self.addCleanup(reopened.close)
        reopened.models.test(authorize=True)
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual(len(self.client.calls), 4)
        self.assertNotIn(KEY, json.dumps(reopened.state()))

    def test_legacy_requests_and_blank_save_do_not_unlock_without_explicit_migration(self):
        config = self.save(); install_legacy_config(self.service); self.session.invalidate()
        calls = len(self.provider.calls)
        before = self.snapshots("model_configs", "model_local_keys", "settings")
        for _ in range(3):
            with self.assertRaises(ServiceError) as caught:
                self.service.models.test(authorize=True)
            self.assertEqual(caught.exception.code, "model_storage_migration_required")
        with self.assertRaises(ServiceError):
            self.service.models.save(base_url=config["base_url"], model=config["model"], api_key=None)
        for confirm in (False, 1, "true"):
            with self.assertRaises(ServiceError):
                self.service.models.migrate(revision=config["revision"], confirm=confirm)
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual(before, self.snapshots(*before))
        self.assertEqual(self.client.calls, [])

    def test_denied_or_corrupt_legacy_migration_keeps_ciphertext_and_never_uploads(self):
        config = self.save(); install_legacy_config(self.service); self.session.invalidate()
        before = self.snapshots("model_configs", "model_local_keys", "settings")
        self.provider.failure = ServiceError("keychain_denied", "generated private denial")
        with self.assertRaises(ServiceError) as caught:
            self.service.models.migrate(revision=config["revision"], confirm=True)
        self.assertEqual(caught.exception.code, "keychain_denied")
        self.assertNotIn("private denial", json.dumps(caught.exception.payload()))
        self.assertEqual(before, self.snapshots(*before))
        self.provider.failure = None
        with self.service.store.database() as db:
            db.execute("UPDATE model_configs SET auth_tag=?", (b"bad",))
        corrupted = self.snapshots("model_configs", "model_local_keys", "settings")
        with self.assertRaises(ServiceError) as caught:
            self.service.models.migrate(revision=config["revision"], confirm=True)
        self.assertEqual(caught.exception.code, "model_key_invalid")
        self.assertEqual(corrupted, self.snapshots(*corrupted))
        self.assertEqual(self.client.calls, [])

    def test_legacy_can_be_replaced_with_explicit_key_without_unlock(self):
        config = self.save(); install_legacy_config(self.service); self.session.invalidate()
        calls = len(self.provider.calls)
        self.provider.failure = ServiceError("keychain_denied", "generated denial")
        self.save()
        self.assertEqual(self.service.models.public()["storage_mode"], "sqlite_plaintext")
        self.assertGreater(self.service.models.public()["revision"], config["revision"])
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual(self.client.calls, [])

    def test_plaintext_storage_has_no_mac_dependency_but_does_not_claim_windows_app_support(self):
        with patch("backend.credentials.sys.platform", "win32"):
            other = LocalService(self.temp.name, model_client=self.client)
        self.addCleanup(other.close)
        other.models.save(base_url="https://generated.invalid", model="generated-flash", api_key=KEY)
        other.models.test(authorize=True)
        self.assertEqual(other.models.public()["storage_mode"], "sqlite_plaintext")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.service.store.path.stat().st_mode & 0o777, 0o600)

    def test_failed_migration_rolls_back_plain_key_and_original_ciphertext(self):
        config = self.save(); install_legacy_config(self.service); self.session.invalidate()
        before = self.snapshots("model_configs", "model_local_keys", "settings")
        with patch.object(self.service.models, "next_revision", side_effect=sqlite3.OperationalError("generated write failure")), self.assertRaises(sqlite3.OperationalError):
            self.service.models.migrate(revision=config["revision"], confirm=True)
        self.assertEqual(before, self.snapshots(*before))
        self.assertEqual(self.client.calls, [])

    def test_internal_upgrade_has_no_application_service_timers_network_or_secret_output(self):
        from backend.__main__ import main
        self.save(); install_legacy_config(self.service); self.session.invalidate()
        before = self.snapshots("messages", "tasks", "model_calls", "text_grants")
        output = io.StringIO()
        with patch("backend.models.Models", return_value=self.service.models), patch("backend.__main__.LocalService", side_effect=AssertionError("no service")), patch("sys.argv", ["reader", "--data-dir", self.temp.name, "--upgrade-model-storage"]), redirect_stdout(output):
            main()
        self.assertEqual(json.loads(output.getvalue()), {"status": "ready"})
        self.assertEqual(self.service.models.public()["storage_mode"], "sqlite_plaintext")
        self.assertEqual(before, self.snapshots(*before))
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.session.values, {})

    def test_internal_upgrade_denied_reports_fixed_code_and_keeps_ciphertext(self):
        from backend.__main__ import main
        self.save(); install_legacy_config(self.service); self.session.invalidate()
        self.provider.failure = ServiceError("keychain_denied", "generated private body")
        before = self.snapshots("model_configs", "model_local_keys", "settings")
        output = io.StringIO()
        with patch("backend.models.Models", return_value=self.service.models), patch("backend.__main__.LocalService", side_effect=AssertionError("no service")), patch("sys.argv", ["reader", "--data-dir", self.temp.name, "--upgrade-model-storage"]), redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            main()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue()), {"error": "keychain_denied"})
        self.assertEqual(before, self.snapshots(*before))
        self.assertEqual(self.client.calls, [])

    def test_upgrade_not_exposed_as_business_command(self):
        self.save(); before = self.snapshots("model_configs", "model_local_keys", "settings")
        with self.assertRaises(ServiceError) as caught:
            self.service.dispatch("model.migrate", {"revision": 1, "confirm": True})
        self.assertEqual(caught.exception.code, "invalid_command")
        self.assertEqual(before, self.snapshots(*before))


if __name__ == "__main__":
    unittest.main()
