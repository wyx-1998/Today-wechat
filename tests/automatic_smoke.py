"""Generated local accounts, in-memory keys and mocked requests only."""

import json
import io
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from backend.contracts import ServiceError, iso_time
from backend.gemini import GeminiClient
from backend.service import LocalService
from backend.storage import SCHEMA, MODEL_MIGRATION, AUTOMATIC_MIGRATION, Store
from tests.model_smoke import FakeClient, MemoryKeychain, KEY


class RecordingClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.payloads = []
        self.context_payloads = []

    def generate(self, config, key, prompt, **kwargs):
        payload = json.loads(prompt.split("输入：", 1)[1])
        self.payloads.append(payload["new_messages"] if isinstance(payload, dict) else payload)
        self.context_payloads.append(payload.get("context_messages", []) if isinstance(payload, dict) else [])
        return super().generate(config, key, prompt, **kwargs)


class AutomaticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="automatic-generated-")
        self.addCleanup(self.temp.cleanup)
        self.now = 1700000000.0
        self.keychain, self.client = MemoryKeychain(), RecordingClient()
        self.service = self.open_service()
        self.service.models.save(base_url="https://synthetic.invalid", model="generated-flash", api_key=KEY)
        self.source = {"mode": "local", "account_id": "generated-account", "root": self.temp.name, "self_id": None}
        self.service.store.configure(self.source, iso_time(self.now-7*86400), [
            {"chat_id": cid, "chat_name": "Generated " + cid, "chat_type": "direct"} for cid in ("first", "second")])
        self.service.pause()
        self.select("first")
        self.add("initial")

    def open_service(self, **kwargs):
        service = LocalService(self.temp.name, keychain=self.keychain, model_client=self.client,
            clock=lambda: self.now, **kwargs)
        self.addCleanup(service.close)
        return service

    def select(self, chat, *, sync=True, analyze=True, name=None, tags=()):
        self.service.store.update_chat(chat, sync_enabled=sync, analysis_enabled=analyze, display_name=name, tag_ids=tags)

    def add(self, mid, *, chat="first", kind="text", content="明天发资料，手机号13800138000", sent=None, arrival=None):
        sent = self.now-1 if sent is None else sent
        with self.service.store.database() as db:
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (mid, self.source["account_id"], chat,
                "generated-stream", mid, None, None, None, iso_time(sent), sent, kind, content,
                "text_available" if content is not None else "type_only", int(kind == "text" and content is not None)))
            db.execute("INSERT INTO message_arrivals VALUES(?,?)", (mid, self.now if arrival is None else arrival))

    def grant(self):
        preview = self.service.dispatch("authorization.preview", {})
        self.service.dispatch("authorization.save", {"authorize": True, "preview_token": preview["preview_token"]})
        return preview

    def ready(self):
        self.grant()
        self.service.resume()
        self.now += 600

    def batches(self):
        with self.service.store.database() as db:
            return [dict(r) for r in db.execute("SELECT * FROM text_batches ORDER BY rowid")]

    def processed(self, *ids, name="generated-previous-run"):
        with self.service.store.database() as db:
            db.execute("INSERT INTO analysis_runs VALUES(?,?,?,?,?,?,?,?,?)", (name, self.source["account_id"],
                json.dumps(ids), 1, 1, "success", iso_time(self.now), iso_time(self.now), None))

    def answer(self, value):
        original = self.client.generate
        def generate(*args, **kwargs):
            _, usage = original(*args, **kwargs)
            return value, usage
        self.client.generate = generate

    def test_prior_success_context_contains_later_completion_not_new_submission(self):
        self.add("prior-completion", content="仓库权限已经开通", sent=self.now-0.5)
        self.processed("prior-completion")
        self.answer({"tasks": []})  # Controlled model response, not an LLM quality test.
        self.ready()
        self.assertEqual(self.service.automatic.tick(), {"status": "success", "task_count": 0})
        self.assertEqual([r["source_message_id"] for r in self.client.payloads[0]], ["initial"])
        self.assertEqual([r["source_message_id"] for r in self.client.context_payloads[0]], ["prior-completion"])
        with self.service.store.database() as db:
            new_run = db.execute("SELECT message_ids FROM analysis_runs WHERE batch_id=?", (self.batches()[0]["batch_id"],)).fetchone()
            self.assertEqual(json.loads(new_run[0]), ["initial"])

    def test_context_excludes_other_chat_account_media_old_future_and_unprocessed(self):
        cases = [("allowed", {}), ("outside-chat", {"chat":"second"}),
                 ("outside-time", {"sent":self.now-8*86400}), ("future-context", {"sent":self.now+90000}),
                 ("system-context", {"kind":"system"}), ("outside-account", {})]
        for mid, kwargs in cases:
            self.add(mid, **kwargs)
        self.add("not-processed", chat="second")
        self.processed(*(mid for mid, _ in cases))
        with self.service.store.database() as db:
            db.execute("UPDATE messages SET account_id='another-generated-account' WHERE message_id='outside-account'")
        self.ready(); self.service.automatic.tick()
        self.assertEqual([r["source_message_id"] for r in self.client.context_payloads[0]], ["allowed"])

    def test_frozen_context_survives_restart_and_does_not_include_later_success(self):
        self.add("old-context"); self.processed("old-context")
        self.ready()
        with self.service.analysis.lock:
            self.assertEqual(self.service.automatic.tick()["status"], "busy")
        self.add("later-context")
        self.processed("later-context", name="later-success")
        self.service.pause(); self.service.close(); self.service = self.open_service()
        self.service.resume(); self.service.automatic.tick()
        self.assertEqual([r["source_message_id"] for r in self.client.context_payloads[0]], ["old-context"])

    def test_changed_context_fails_before_key_read_or_network(self):
        self.add("old-context"); self.processed("old-context")
        self.ready()
        with self.service.analysis.lock:
            self.service.automatic.tick()
        with self.service.store.database() as db:
            db.execute("UPDATE messages SET content='changed-context' WHERE message_id='old-context'")
        self.keychain.value = None
        self.assertEqual(self.service.automatic.tick(), {"status":"failed", "error_code":"batch_context_changed"})
        self.assertEqual(self.client.calls, [])

    def test_missing_context_manifest_is_not_silently_treated_as_legacy(self):
        self.ready()
        with self.service.analysis.lock:
            self.service.automatic.tick()
        with self.service.store.database() as db:
            db.execute("DELETE FROM text_batch_contexts")
        self.assertEqual(self.service.automatic.tick()["error_code"], "batch_context_changed")
        self.assertEqual(self.client.calls, [])

    def test_context_only_task_is_rejected_atomically(self):
        self.add("old-context"); self.processed("old-context")
        self.answer({"tasks":[{"title":"Generated old action", "owner_type":"unknown", "due_date":None,
                               "source_message_ids":["old-context"]}]})
        self.ready()
        self.assertEqual(self.service.automatic.tick()["error_code"], "analysis_evidence")
        self.assertEqual(self.service.analysis.tasks()["items"], [])

    def test_task_can_cite_fresh_acceptance_and_prior_request_context(self):
        self.add("prior-request", content="请给我更新材料")
        self.processed("prior-request")
        self.answer({"tasks":[{"title":"Generated accepted action", "owner_type":"unknown", "due_date":None,
                               "source_message_ids":["initial","prior-request"]}]})
        self.ready()
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        self.assertEqual(self.service.analysis.tasks()["items"][0]["source_message_ids"], ["initial","prior-request"])

    def test_actual_adapter_incremental_context_restores_frozen_sources(self):
        self.add("prior-request", content="请给我更新材料")
        self.processed("prior-request")
        class Opener:
            def open(inner, request, timeout):
                wire = json.loads(request.data)["contents"][0]["parts"][0]["text"]
                payload = json.loads(wire.partition("输入：")[2])
                self.assertEqual(payload["new_messages"][0]["source_message_id"], "M1")
                self.assertEqual(payload["context_messages"][0]["source_message_id"], "M2")
                self.assertNotIn("13800138000", wire)
                answer = {"tasks": [{"title": "更新材料", "owner_type": "unknown", "due_date": None,
                    "source_message_ids": ["M1", "M2"]}]}
                return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(answer)}]}}],
                    "usageMetadata": {"totalTokenCount": 130}}).encode())
        self.service.models.client = GeminiClient(opener=Opener())
        self.ready()
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        self.assertEqual(self.service.analysis.tasks()["items"][0]["source_message_ids"], ["initial", "prior-request"])
        self.assertEqual(self.service.models.usage()["today"]["known_total_tokens"], 130)

    def test_actual_adapter_context_only_alias_cannot_create_automatic_task(self):
        self.add("prior-request", content="请给我更新材料")
        self.processed("prior-request")
        class Opener:
            def open(inner, request, timeout):
                answer = {"tasks": [{"title": "更新材料", "owner_type": "unknown", "due_date": None,
                    "source_message_ids": ["M2"]}]}
                return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(answer)}]}}],
                    "usageMetadata": {"totalTokenCount": 130}}).encode())
        self.service.models.client = GeminiClient(opener=Opener())
        self.ready()
        self.assertEqual(self.service.automatic.tick()["error_code"], "analysis_evidence")
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual(self.batches()[0]["status"], "failed")
        self.assertEqual(self.service.models.usage()["today"]["known_total_tokens"], 130)

    def test_context_is_redacted_without_rewriting_original(self):
        self.add("old-context", content="手机号13800138000")
        self.processed("old-context"); self.ready(); self.service.automatic.tick()
        self.assertNotIn("13800138000", json.dumps(self.client.context_payloads))
        self.assertIn("13800138000", self.service.store.message_detail("old-context")["content"])

    def test_context_sender_change_invalidates_frozen_request(self):
        self.add("old-context"); self.processed("old-context"); self.ready()
        with self.service.analysis.lock:
            self.service.automatic.tick()
        with self.service.store.database() as db:
            db.execute("UPDATE messages SET sender_id='changed-generated-person' WHERE message_id='old-context'")
        self.assertEqual(self.service.automatic.tick()["error_code"], "batch_context_changed")
        self.assertEqual(self.client.calls, [])

    def test_fresh_sender_change_invalidates_frozen_request(self):
        self.ready()
        with self.service.analysis.lock:
            self.service.automatic.tick()
        with self.service.store.database() as db:
            db.execute("UPDATE messages SET sender_id='changed-generated-person' WHERE message_id='initial'")
        self.assertEqual(self.service.automatic.tick()["error_code"], "batch_source_changed")
        self.assertEqual(self.client.calls, [])

    def test_unknown_participant_ids_differ_without_becoming_self(self):
        self.add("old-context"); self.processed("old-context")
        with self.service.store.database() as db:
            db.execute("UPDATE messages SET sender_id=message_id")
        self.ready(); self.service.automatic.tick()
        fresh, context = self.client.payloads[0][0], self.client.context_payloads[0][0]
        self.assertNotEqual(fresh["participant"], context["participant"])
        self.assertEqual(fresh["speaker"], "身份未知")
        self.assertNotEqual(fresh["participant"], "initial")
        self.assertEqual(self.service.analysis.tasks()["items"][0]["owner_type"], "unknown")

    def test_context_change_during_response_keeps_usage_but_no_publication(self):
        self.add("old-context"); self.processed("old-context"); self.ready()
        def changed():
            with self.service.store.database() as db:
                db.execute("UPDATE messages SET content='changed during response' WHERE message_id='old-context'")
        self.client.callback = changed
        self.assertEqual(self.service.automatic.tick()["error_code"], "batch_context_changed")
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual(self.service.models.usage()["today"]["success"], 1)

    def test_unknown_retry_uses_same_context_and_rejects_changed_context(self):
        self.add("old-context"); self.processed("old-context"); self.ready()
        def unknown():
            raise ServiceError("model_result_unknown", "Generated unknown")
        self.client.callback = unknown
        self.service.automatic.tick(); batch = self.batches()[0]
        with self.service.store.database() as db:
            db.execute("UPDATE messages SET content='changed after unknown' WHERE message_id='old-context'")
        with self.assertRaises(ServiceError) as caught:
            self.service.automatic.retry(batch_id=batch["batch_id"], authorize=True)
        self.assertEqual(caught.exception.code, "batch_context_changed")
        self.assertEqual(len(self.client.calls), 1)

    def test_version_three_migration_preserves_old_batch_without_new_context_send(self):
        root = Path(self.temp.name) / "v3"
        root.mkdir()
        with closing(sqlite3.connect(root / "application.sqlite3")) as db:
            db.executescript(SCHEMA); db.executescript(MODEL_MIGRATION); db.executescript(AUTOMATIC_MIGRATION)
            db.execute("INSERT INTO text_grants VALUES('old-grant','{}','old-scope','revoked','old',NULL,NULL)")
            db.execute("INSERT INTO text_batches VALUES('old-batch','old-grant','old-account','old-chat','[\"old-message\"]','old-hash','success','old',NULL,NULL)")
            db.execute("INSERT INTO tasks VALUES('old-task','old-account','old-batch','Generated old task','unknown',NULL,NULL,'unknown','completed','[\"old-message\"]','old',2)")
            db.commit()
        store = Store(root)
        with store.database() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 12)
            context = db.execute("SELECT * FROM text_batch_contexts").fetchone()
            self.assertEqual((context["message_ids"],context["policy_version"]), ("[]",0))
            self.assertEqual(db.execute("SELECT input_hash FROM text_batches").fetchone()[0], "old-hash")
            self.assertEqual(tuple(db.execute("SELECT status,revision FROM tasks").fetchone()), ("completed",2))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0], 0)

    def test_configuration_sync_selection_and_preview_do_not_grant_or_send(self):
        self.service.resume()
        self.now += 1800
        self.assertEqual(self.service.automatic.tick()["status"], "inactive")
        self.assertIsNone(self.service.automatic.public()["grant"])
        preview = self.service.automatic.preview()
        self.assertEqual(preview["message_types"], ["text"])
        self.assertEqual(preview["chat_ids"], ["first"])
        self.assertTrue(preview["continuous"])
        self.assertNotIn("13800138000", json.dumps(preview))
        for authorization in (False, 1, "true", None):
            with self.assertRaises(ServiceError):
                self.service.automatic.save(authorize=authorization, preview_token=preview["preview_token"])
        self.assertEqual(self.client.calls, [])

    def test_grant_consumes_preview_and_does_not_resume_or_send(self):
        preview = self.grant()
        self.assertTrue(self.service.store.settings()["paused"])
        with self.assertRaises(ServiceError):
            self.service.automatic.save(authorize=True, preview_token=preview["preview_token"])
        self.assertEqual(self.service.automatic.public()["pending_messages"], 1)
        self.now += 1800
        self.assertEqual(self.service.automatic.tick()["status"], "inactive")
        self.assertEqual(self.client.calls, [])

    def test_quiet_window_exact_boundary_sourced_task_and_no_duplicate(self):
        self.grant(); self.service.resume(); self.now += 599
        self.assertEqual(self.service.automatic.tick()["status"], "waiting")
        self.now += 1
        self.assertEqual(self.service.automatic.tick(), {"status": "success", "task_count": 1})
        task = self.service.analysis.tasks()["items"][0]
        self.assertEqual((task["status"], task["owner_type"]), ("needs_confirmation", "unknown"))
        self.assertEqual(task["source_message_ids"], ["initial"])
        self.assertNotIn("13800138000", json.dumps(self.client.payloads))
        self.assertIn("13800138000", self.service.store.message_detail("initial")["content"])
        self.service.analysis.update_task(task_id=task["task_id"], revision=1, status="completed")
        self.now += 2000
        self.assertEqual(self.service.automatic.tick()["status"], "waiting")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.service.analysis.tasks()["items"][0]["status"], "completed")

    def test_continuous_conversation_submits_at_thirty_minutes_not_early(self):
        self.grant(); self.service.resume()
        for n in range(1, 6):
            self.now += 300; self.add("continuous-"+str(n))
            self.assertEqual(self.service.automatic.tick()["status"], "waiting")
        self.now += 299
        self.assertEqual(self.service.automatic.tick()["status"], "waiting")
        self.now += 1
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        self.assertEqual(len(self.client.payloads[0]), 6)
        self.assertEqual(len(self.batches()), 1)

    def test_chat_windows_are_independent_and_not_joined(self):
        self.select("second"); self.grant(); self.service.resume()
        self.now += 300; self.add("other", chat="second")
        self.service.automatic.tick()
        self.now += 300
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        self.assertEqual([r["source_message_id"] for r in self.client.payloads[0]], ["initial"])
        self.now += 300
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        self.assertEqual([r["source_message_id"] for r in self.client.payloads[1]], ["other"])

    def test_new_arrival_during_request_waits_for_next_batch(self):
        self.ready()
        self.client.callback = lambda: self.add("during-request")
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        self.client.callback = None
        self.assertEqual([r["source_message_id"] for r in self.client.payloads[0]], ["initial"])
        self.assertEqual(self.service.automatic.tick()["status"], "waiting")
        self.now += 600
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        self.assertEqual([r["source_message_id"] for r in self.client.payloads[1]], ["during-request"])

    def test_non_text_activity_delays_window_without_uploading_body(self):
        self.grant(); self.service.resume(); self.now += 590
        self.add("system", kind="system", content="generated-system-body")
        self.add("audio", kind="audio", content=None)
        self.now += 10
        self.assertEqual(self.service.automatic.tick()["status"], "waiting")
        self.now += 590
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        self.assertEqual(len(self.client.payloads[0]), 1)
        self.assertNotIn("generated-system-body", json.dumps(self.client.payloads))

    def test_range_and_unselected_chat_text_are_not_uploaded(self):
        self.add("unselected", chat="second")
        self.add("older", sent=self.now-8*86400)
        self.add("future", sent=self.now+86400)
        self.add("image", kind="image", content=None)
        self.ready()
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        self.assertEqual([r["source_message_id"] for r in self.client.payloads[0]], ["initial"])

    def test_label_and_tag_changes_keep_grant_without_expanding_scope(self):
        self.grant()
        tag = self.service.store.save_tag("Generated label")
        self.select("first", name="Renamed", tags=[tag])
        self.select("second", sync=True, analyze=False, tags=[tag])
        self.assertEqual(self.service.automatic.public()["grant"]["status"], "active")
        self.assertEqual(self.service.automatic.public()["grant"]["scope"]["chat_ids"], ["first"])
        self.add("tagged-but-not-authorized", chat="second")
        self.service.resume(); self.now += 600
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        self.assertEqual(len(self.client.payloads[0]), 1)

    def test_scope_change_back_does_not_reactivate_old_grant(self):
        self.grant()
        self.select("second"); self.select("second", analyze=False)
        self.assertEqual(self.service.automatic.public()["grant"]["status"], "invalid")
        self.service.resume(); self.now += 1800
        self.assertEqual(self.service.automatic.tick()["status"], "inactive")
        self.assertEqual(self.client.calls, [])

    def test_time_range_change_back_requires_new_grant(self):
        self.grant()
        original = self.service.store.settings()["time_start"]
        self.service.store.set_range(iso_time(self.now-6*86400))
        self.service.store.set_range(original)
        self.assertEqual(self.service.automatic.public()["grant"]["status"], "invalid")

    def test_service_change_back_requires_new_grant(self):
        self.grant()
        self.service.models.save(base_url="https://other.invalid", model="generated-flash", api_key=KEY)
        self.service.models.save(base_url="https://synthetic.invalid", model="generated-flash", api_key=KEY)
        self.assertEqual(self.service.automatic.public()["grant"]["status"], "invalid")
        self.service.resume(); self.now += 600
        self.assertEqual(self.service.automatic.tick()["status"], "inactive")
        self.assertEqual(self.client.calls, [])

    def test_changed_or_expired_authorization_preview_cannot_save(self):
        preview = self.service.automatic.preview()
        self.select("second")
        with self.assertRaises(ServiceError):
            self.service.automatic.save(authorize=True, preview_token=preview["preview_token"])
        preview = self.service.automatic.preview(); self.now += 901
        with self.assertRaises(ServiceError):
            self.service.automatic.save(authorize=True, preview_token=preview["preview_token"])
        self.assertIsNone(self.service.automatic.public()["grant"])

    def test_waiting_messages_and_grant_survive_restart_without_auto_resume(self):
        self.grant(); self.service.close()
        self.service = self.open_service()
        self.assertEqual(self.service.automatic.public()["pending_messages"], 1)
        self.assertEqual(self.service.automatic.public()["grant"]["status"], "active")
        self.now += 600
        self.assertEqual(self.service.automatic.tick()["status"], "inactive")
        self.service.resume()
        self.assertEqual(self.service.automatic.tick()["status"], "success")

    def test_disabled_model_and_closed_service_do_not_send(self):
        self.ready(); self.service.models.enable(enabled=False)
        self.assertEqual(self.service.automatic.tick()["status"], "inactive")
        self.assertEqual(self.client.calls, [])
        self.service.models.enable(enabled=True); self.service.close()
        with self.assertRaises(ServiceError):
            self.service.automatic.tick()
        self.assertEqual(self.client.calls, [])

    def test_pause_then_resume_during_response_does_not_publish(self):
        self.ready()
        def change():
            self.service.pause(); self.service.resume()
        self.client.callback = change
        self.assertEqual(self.service.automatic.tick()["status"], "failed")
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual(self.service.models.usage()["today"]["requests"], 1)
        self.client.callback = None; self.now += 1800
        self.assertEqual(self.service.automatic.tick()["status"], "waiting")
        self.assertEqual(len(self.client.calls), 1)

    def test_revocation_during_response_preserves_call_but_not_result(self):
        self.ready(); self.client.callback = self.service.automatic.revoke
        self.assertEqual(self.service.automatic.tick()["status"], "failed")
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual(self.service.automatic.public()["grant"]["status"], "revoked")
        self.assertEqual(self.service.automatic.tick()["status"], "inactive")
        self.assertEqual(len(self.client.calls), 1)

    def test_unknown_response_has_no_auto_retry_and_explicit_retry_is_idempotent(self):
        self.ready()
        def unknown():
            raise ServiceError("model_result_unknown", "generated unknown response")
        self.client.callback = unknown
        self.assertEqual(self.service.automatic.tick()["status"], "unknown")
        for _ in range(3):
            self.now += 1800; self.service.automatic.tick()
        self.assertEqual(len(self.client.calls), 1)
        batch = self.batches()[0]
        with self.assertRaises(ServiceError):
            self.service.automatic.retry(batch_id=batch["batch_id"])
        self.client.callback = None
        self.service.automatic.retry(batch_id=batch["batch_id"], authorize=True)
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 1)
        self.assertEqual(len(self.batches()), 1)

    def test_invalid_sources_fail_atomically_and_do_not_repeat(self):
        self.ready(); self.client.invalid = True
        self.assertEqual(self.service.automatic.tick()["status"], "failed")
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual(self.service.automatic.tick()["status"], "waiting")
        self.assertEqual(len(self.client.calls), 1)

    def test_frozen_source_change_stops_before_send_and_records_failure(self):
        self.ready()
        with self.service.analysis.lock:
            self.assertEqual(self.service.automatic.tick()["status"], "busy")
        with self.service.store.database() as db:
            db.execute("UPDATE messages SET content='changed generated content' WHERE message_id='initial'")
        self.assertEqual(self.service.automatic.tick(), {"status": "failed", "error_code": "batch_source_changed"})
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.service.automatic.tick()["status"], "waiting")

    def test_interrupted_running_batch_is_unknown_after_restart_not_resent(self):
        self.ready()
        with self.service.analysis.lock:
            self.service.automatic.tick()
        with self.service.store.database() as db:
            db.execute("UPDATE text_batches SET status='running'")
        self.service.close(); self.service = self.open_service()
        self.service.automatic.recover()
        self.assertEqual(self.batches()[0]["status"], "unknown")
        self.assertEqual(self.service.automatic.tick()["status"], "waiting")
        self.assertEqual(self.client.calls, [])

    def test_manual_completed_text_is_not_sent_again_by_continuous_flow(self):
        self.grant()
        preview = self.service.analysis.real_preview(chat_id="first")
        self.service.analysis.run_real(chat_id="first", authorize=True, preview_token=preview["preview_token"])
        self.service.resume(); self.now += 600
        self.assertEqual(self.service.automatic.tick()["status"], "waiting")
        self.assertEqual(len(self.client.calls), 1)

    def test_version_two_migration_preserves_data_without_creating_grant(self):
        root = Path(self.temp.name) / "v2"
        root.mkdir()
        db = sqlite3.connect(root / "application.sqlite3")
        try:
            db.executescript(SCHEMA); db.executescript(MODEL_MIGRATION)
            db.execute("INSERT INTO tags VALUES('keep','Generated historical tag')"); db.commit()
        finally:
            db.close()
        store = Store(root)
        with store.database() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 12)
            self.assertEqual(db.execute("SELECT name FROM tags").fetchone()[0], "Generated historical tag")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM text_grants").fetchone()[0], 0)

    def test_background_timer_uses_grant_and_stops_after_close(self):
        self.ready()
        sent = threading.Event()
        self.client.callback = sent.set
        self.service.analysis_interval = 0.01
        self.service.start()
        self.assertTrue(sent.wait(timeout=3))
        self.service.close()
        self.assertFalse(self.service.analysis_timer.is_alive())
        self.assertEqual(len(self.client.calls), 1)

    def test_credential_read_race_does_not_block_pause_or_start_request_after_pause(self):
        self.ready()
        entered, release, paused = threading.Event(), threading.Event(), threading.Event()
        original = self.service.models.decrypt
        def wait_for_key(row):
            entered.set()
            if not release.wait(timeout=3):
                raise AssertionError("generated key wait exceeded")
            return original(row)
        self.service.models.decrypt = wait_for_key
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(self.service.automatic.tick()))
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=3))
            def pause():
                self.service.pause(); paused.set()
            pauser = threading.Thread(target=pause)
            pauser.start()
            try:
                self.assertTrue(paused.wait(timeout=1))
            finally:
                release.set(); pauser.join(timeout=3)
        finally:
            release.set(); thread.join(timeout=3); self.service.models.decrypt = original
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome[0]["status"], "queued")
        self.assertEqual(self.client.calls, [])
        self.service.resume()
        self.assertEqual(self.service.automatic.tick()["status"], "success")

    def test_missing_key_stops_batch_without_repeated_key_prompts(self):
        self.ready()
        with self.service.store.database() as db:
            db.execute("DELETE FROM model_local_keys")
        self.assertEqual(self.service.automatic.tick()["status"], "failed")
        self.assertEqual(self.service.automatic.tick()["status"], "waiting")
        self.assertEqual(self.client.calls, [])

    def test_revoked_unknown_batch_cannot_retry_under_a_new_scope(self):
        self.ready()
        def unknown():
            raise ServiceError("model_result_unknown", "generated unknown response")
        self.client.callback = unknown; self.service.automatic.tick()
        batch = self.batches()[0]
        self.service.automatic.revoke(); self.select("second"); self.grant()
        self.assertFalse(self.service.automatic.public()["batches"][0]["retryable"])
        with self.assertRaises(ServiceError):
            self.service.automatic.retry(batch_id=batch["batch_id"], authorize=True)
        self.assertEqual(len(self.client.calls), 1)

    def test_new_exact_scope_grant_still_needs_explicit_retry_of_unknown_batch(self):
        self.ready()
        def unknown():
            raise ServiceError("model_result_unknown", "generated unknown response")
        self.client.callback = unknown; self.service.automatic.tick()
        batch = self.batches()[0]
        self.service.automatic.revoke(); self.grant()
        self.client.callback = None
        self.assertEqual(self.service.automatic.tick()["status"], "waiting")
        self.assertEqual(len(self.client.calls), 1)
        self.assertTrue(self.service.automatic.public()["batches"][0]["retryable"])
        self.service.automatic.retry(batch_id=batch["batch_id"], authorize=True)
        self.assertEqual(self.service.automatic.public()["pending_messages"], 1)
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        self.assertEqual(len(self.client.calls), 2)

    def test_queued_batch_counts_as_waiting_in_settings(self):
        self.ready()
        with self.service.analysis.lock:
            self.assertEqual(self.service.automatic.tick()["status"], "busy")
        self.assertEqual(self.service.automatic.public()["pending_messages"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
