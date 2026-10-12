"""Generated saved-text reviews. Never reads private files or invokes a provider."""

import json
import io
import unittest

from backend.contracts import ServiceError
from backend.gemini import GeminiClient
from backend.reanalysis import Reanalysis
from backend.storage import Store
from tests import automatic_smoke


class ReanalysisTests(unittest.TestCase):
    def setUp(self):
        self.h = automatic_smoke.AutomaticTests("runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.service = self.h.service
        self.h.ready(); self.service.automatic.tick(); self.service.pause()
        self.old = self.service.analysis.tasks()["items"][0]
        self.target = self.old["task_id"]
        self.h.client.calls.clear()
        self.response = {"tasks": [], "changes": []}
        self.payloads = []
        self.callback = None
        def generate(config, key, prompt, **kwargs):
            self.h.client.calls.append((config["base_url"], bool(kwargs.get("schema"))))
            self.payloads.append(json.loads(prompt.split("输入：", 1)[1]))
            if self.callback:
                self.callback()
            return self.response, {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130}
        self.h.client.generate = generate

    def task(self, title=None, *, target=None, refs=None, due=None):
        return {"title": title or self.old["title"], "owner_type": "unknown", "due_date": due,
            "source_message_ids": refs or ["initial"], "existing_task_id": target}

    def preview(self):
        return self.service.dispatch("analysis.review.preview", {"chat_id": "first"})

    def review(self, preview=None):
        preview = preview or self.preview()
        return self.service.dispatch("analysis.review", {"chat_id": "first", "authorize": True,
            "preview_token": preview["preview_token"]})

    def current(self):
        return next(t for t in self.service.analysis.tasks()["items"] if t["task_id"] == self.target)

    def completion(self, kind="completed"):
        self.h.add("completion", content="资料已经发送")
        self.response = {"tasks": [], "changes": [{"task_id": self.target, "kind": kind,
            "source_message_ids": ["completion"]}]}

    def resolve(self, change, accept=True, revision=None):
        return self.service.dispatch("task.change.resolve", {"change_id": change["change_id"],
            "task_revision": self.current()["revision"] if revision is None else revision, "accept": accept})

    def test_preview_includes_processed_text_and_stays_local(self):
        self.assertEqual(self.service.analysis.real_preview(chat_id="first")["message_count"], 0)
        preview = self.preview()
        self.assertEqual(preview["message_count"], 1)
        self.assertEqual(preview["existing_tasks"][0]["task_id"], self.target)
        self.assertEqual(self.h.client.calls, [])
        self.assertTrue(self.service.store.settings()["paused"])
        self.assertNotIn("13800138000", json.dumps(preview))

    def test_actual_adapter_completion_alias_is_pending_and_links_real_evidence(self):
        self.h.add("completion", content="资料已经发送")
        class Opener:
            def open(inner, request, timeout):
                wire = json.loads(request.data)["contents"][0]["parts"][0]["text"]
                payload = json.loads(wire.partition("输入：")[2])
                self.assertEqual(payload["existing_tasks"][0]["task_id"], "T1")
                self.assertNotIn(self.target, wire)
                alias = next(m["source_message_id"] for m in payload["messages"] if m["content"] == "资料已经发送")
                answer = {"tasks": [], "changes": [{"task_id": "T1", "kind": "completed", "source_message_ids": [alias]}]}
                return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(answer)}]}}],
                    "usageMetadata": {"totalTokenCount": 130}}).encode())
        self.service.models.client = GeminiClient(opener=Opener())
        self.assertEqual(self.review()["change_count"], 1)
        current = self.current()
        self.assertEqual(current["status"], self.old["status"])
        self.assertEqual(current["changes"][0]["source_message_ids"], ["completion"])
        with self.service.store.database() as db:
            self.assertEqual(db.execute("SELECT task_id FROM task_changes WHERE change_id=?",
                (current["changes"][0]["change_id"],)).fetchone()[0], self.target)

    def test_actual_adapter_unknown_existing_alias_cannot_turn_into_new_task(self):
        class Opener:
            def open(inner, request, timeout):
                answer = {"tasks": [{"title": "更新材料", "owner_type": "unknown", "due_date": None,
                    "source_message_ids": ["M1"], "existing_task_id": "T999"}], "changes": []}
                return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(answer)}]}}],
                    "usageMetadata": {"totalTokenCount": 130}}).encode())
        self.service.models.client = GeminiClient(opener=Opener())
        with self.assertRaises(ServiceError) as error:
            self.review()
        self.assertEqual(error.exception.code, "review_target")
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 1)
        self.assertEqual(self.current()["changes"], [])

    def test_confirmation_required_before_any_request(self):
        preview = self.preview()
        for authorize in (False, 1, "true"):
            with self.assertRaises(ServiceError):
                self.service.reanalysis.run(chat_id="first", authorize=authorize, preview_token=preview["preview_token"])
        self.assertEqual(self.h.client.calls, [])
        self.assertEqual(self.review(preview)["status"], "success")

    def test_sync_running_allowed_but_unselected_chat_rejected(self):
        with self.assertRaises(ServiceError):
            self.service.reanalysis.preview(chat_id="second")
        self.service.resume()
        self.assertEqual(self.preview()['message_count'], 1)
        self.assertFalse(self.service.store.settings()['paused'])
        self.assertEqual(self.h.client.calls, [])

    def test_late_arrival_inside_old_window_not_added_to_frozen_review(self):
        self.service.resume()
        preview = self.preview()
        self.h.add('late-arrival', sent=self.h.now-50)
        self.review(preview)
        self.assertEqual([m['source_message_id'] for m in self.payloads[0]['messages']], ['initial'])
        self.assertFalse(self.service.store.settings()['paused'])

    def test_review_with_sync_running_preserves_new_messages_for_later(self):
        self.service.resume()
        preview = self.preview()
        self.h.now += 1
        self.h.add('new-after-preview')
        self.review(preview)
        self.assertEqual([m['source_message_id'] for m in self.payloads[0]['messages']], ['initial'])
        self.assertEqual([r['message_id'] for r in self.service.analysis.real_rows(self.service.store.settings(), 'first')], ['new-after-preview'])

    def test_preview_is_single_use_and_bound_to_chat(self):
        preview = self.preview()
        with self.assertRaises(ServiceError):
            self.service.reanalysis.run(chat_id="second", authorize=True, preview_token=preview["preview_token"])
        self.review(preview)
        with self.assertRaises(ServiceError):
            self.review(preview)
        self.assertEqual(len(self.h.client.calls), 1)

    def test_expired_or_restarted_preview_cannot_send(self):
        preview = self.preview()
        self.h.now += 901
        with self.assertRaises(ServiceError):
            self.review(preview)
        preview = self.preview()
        self.service.reanalysis = Reanalysis(self.service)
        with self.assertRaises(ServiceError):
            self.review(preview)
        self.assertEqual(self.h.client.calls, [])

    def test_changed_text_participant_or_identity_before_send_rejected(self):
        for field, value in (("content", "generated changed"), ("sender_id", "different"), ("is_self", 1)):
            with self.subTest(field=field):
                preview = self.preview()
                with self.service.store.database() as db:
                    db.execute("UPDATE messages SET " + field + "=? WHERE message_id='initial'", (value,))
                with self.assertRaises(ServiceError):
                    self.review(preview)
        self.assertEqual(self.h.client.calls, [])

    def test_changed_service_or_user_state_invalidates_preview(self):
        preview = self.preview()
        self.service.analysis.update_task(task_id=self.target, revision=1, status="completed")
        with self.assertRaises(ServiceError):
            self.review(preview)
        preview = self.preview()
        self.service.models.save(base_url="https://other.invalid", model="generated-flash")
        with self.assertRaises(ServiceError):
            self.review(preview)
        self.assertEqual(self.h.client.calls, [])

    def test_new_out_of_frozen_window_message_not_uploaded(self):
        preview = self.preview()
        self.h.add("later", sent=self.h.now + 10)
        self.h.now += 20
        self.review(preview)
        self.assertEqual([m["source_message_id"] for m in self.payloads[0]["messages"]], ["initial"])

    def test_scope_change_during_response_keeps_results_and_counts_usage(self):
        self.response = {"tasks": [self.task("新增事项")], "changes": []}
        self.callback = lambda: self.service.store.update_chat("first", sync_enabled=False, analysis_enabled=False)
        with self.assertRaises(ServiceError) as error:
            self.review()
        self.assertEqual(error.exception.code, "real_chat_scope")
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 1)
        self.assertEqual(len(self.h.client.calls), 1)
        self.assertEqual(self.service.models.usage()["today"]["requests"], 2)

    def test_resume_then_pause_during_response_does_not_publish(self):
        self.response = {"tasks": [self.task("新增事项")], "changes": []}
        def toggle():
            self.service.resume(); self.service.pause()
        self.callback = toggle
        with self.assertRaises(ServiceError):
            self.review()
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 1)

    def test_unknown_target_invalidates_entire_response(self):
        self.response = {"tasks": [self.task("新增事项"), self.task("越界事项", target="outside")], "changes": []}
        with self.assertRaises(ServiceError):
            self.review()
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 1)

    def test_invalid_or_conflicting_change_sources_rejected_atomically(self):
        for refs in ([], ["outside"], ["initial", "initial"]):
            self.response = {"tasks": [self.task("新增事项")], "changes": [{"task_id": self.target,
                "kind": "completed", "source_message_ids": refs}]}
            with self.assertRaises(ServiceError):
                self.review()
            self.assertEqual(len(self.service.analysis.tasks()["items"]), 1)
        self.response = {"tasks": [self.task(target=self.target)], "changes": [{"task_id": self.target,
            "kind": "completed", "source_message_ids": ["initial"]}]}
        with self.assertRaises(ServiceError):
            self.review()

    def test_details_are_proposed_not_overwritten_until_user_accepts(self):
        self.response = {"tasks": [self.task("发送具体资料", target=self.target, due="2026-10-04")], "changes": []}
        result = self.review()
        self.assertEqual((result["task_count"], result["change_count"]), (0, 1))
        self.assertEqual(self.current()["title"], self.old["title"])
        self.resolve(self.current()["changes"][0])
        current = self.current()
        self.assertEqual((current["title"], current["due_at"], current["status"]), ("发送具体资料", "2026-10-04", "needs_confirmation"))

    def test_user_completion_during_analysis_is_preserved(self):
        self.response = {"tasks": [self.task("补充资料细节", target=self.target)], "changes": []}
        self.callback = lambda: self.service.analysis.update_task(task_id=self.target, revision=1, status="completed")
        self.review()
        current = self.current()
        self.assertEqual((current["status"], current["revision"], current["title"]), ("completed", 2, self.old["title"]))
        self.resolve(current["changes"][0])
        self.assertEqual(self.current()["status"], "completed")

    def test_completion_evidence_requires_confirmation(self):
        self.completion(); self.review()
        current = self.current()
        self.assertEqual(current["status"], "needs_confirmation")
        self.assertEqual(set(current["source_message_ids"]), {"initial", "completion"})
        change = current["changes"][0]
        self.resolve(change)
        self.assertEqual(self.current()["status"], "completed")
        with self.assertRaises(ServiceError):
            self.resolve(change)

    def test_cancellation_evidence_requires_confirmation(self):
        self.completion("cancelled"); self.review()
        self.assertEqual(self.current()["status"], "needs_confirmation")
        self.resolve(self.current()["changes"][0])
        self.assertEqual(self.current()["status"], "cancelled")

    def test_keep_current_and_repeated_same_evidence_not_reproposed(self):
        self.completion(); self.review()
        self.resolve(self.current()["changes"][0], accept=False)
        self.assertEqual(self.current()["changes"], [])
        self.assertEqual(self.current()["status"], "needs_confirmation")
        self.assertEqual(self.review()["change_count"], 0)

    def test_old_revision_cannot_apply_proposal(self):
        self.completion(); self.review()
        change = self.current()["changes"][0]
        self.service.analysis.update_task(task_id=self.target, revision=1, status="ignored")
        with self.assertRaises(ServiceError):
            self.resolve(change, revision=1)
        self.assertEqual(self.current()["status"], "ignored")

    def test_linked_item_grows_evidence_without_duplicate_or_status_change(self):
        self.h.add("followup")
        self.response = {"tasks": [self.task(target=self.target, refs=["initial", "followup"])], "changes": []}
        self.assertEqual(self.review()["task_count"], 0)
        current = self.current()
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 1)
        self.assertEqual(set(current["source_message_ids"]), {"initial", "followup"})
        self.assertEqual(current["status"], self.old["status"])

    def test_exact_repeated_result_never_reopens_completed_task(self):
        self.service.analysis.update_task(task_id=self.target, revision=1, status="completed")
        self.response = {"tasks": [self.task()], "changes": []}
        self.assertEqual(self.review()["task_count"], 0)
        self.assertEqual(self.current()["status"], "completed")

    def test_different_actions_sharing_message_do_not_auto_merge(self):
        self.response = {"tasks": [self.task("整理资料"), self.task("联系确认负责人")], "changes": []}
        self.assertEqual(self.review()["task_count"], 2)
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 3)

    def test_primary_success_records_preserved_and_review_is_separate(self):
        with self.service.store.database() as db:
            original = tuple(db.execute("SELECT * FROM analysis_runs").fetchone())
            batch = tuple(db.execute("SELECT * FROM text_batches").fetchone())
        self.review()
        with self.service.store.database() as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM analysis_runs WHERE batch_id=?", (original[0],)).fetchone()), original)
            self.assertEqual(tuple(db.execute("SELECT * FROM text_batches").fetchone()), batch)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0], 1)

    def test_storage_failure_rolls_back_all_task_and_evidence_changes(self):
        self.h.add("followup")
        self.response = {"tasks": [self.task(target=self.target, refs=["initial", "followup"]), self.task("新增事项")], "changes": []}
        with self.service.store.database() as db:
            db.execute("CREATE TRIGGER fail_new_task BEFORE INSERT ON tasks BEGIN SELECT RAISE(ABORT,'generated failure'); END")
        with self.assertRaises(ServiceError) as error:
            self.review()
        self.assertEqual(error.exception.code, "review_store_failed")
        self.assertEqual(self.current()["source_message_ids"], ["initial"])
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 1)

    def test_v4_migration_seeds_evidence_without_modifying_task_or_run(self):
        self.service.analysis.update_task(task_id=self.target, revision=1, status="completed")
        with self.service.store.database() as db:
            task = tuple(db.execute("SELECT * FROM tasks").fetchone())
            run = tuple(db.execute("SELECT * FROM analysis_runs").fetchone())
            for table in ("ask_citations", "ask_runs", "ask_turns", "ask_threads", "ask_grants", "ask_fts", "ask_index", "news_settings", "news_runs", "news_editions", "meeting_notes", "attention_rejections", "attention_items", "model_local_keys", "discovery_rejections", "discovery_candidates", "discovery_windows", "discovery_attempts", "task_changes", "task_evidence_links", "review_runs"):
                db.execute("DROP TABLE " + table)
            db.execute("PRAGMA user_version=4")
        migrated = Store(self.h.temp.name)
        with migrated.database() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 12)
            self.assertEqual(tuple(db.execute("SELECT * FROM tasks").fetchone()), task)
            self.assertEqual(tuple(db.execute("SELECT * FROM analysis_runs").fetchone()), run)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_evidence_links").fetchone()[0], 1)

    def test_out_of_window_existing_result_not_sent_as_context(self):
        with self.service.store.database() as db:
            db.execute("UPDATE tasks SET source_message_ids=?", (json.dumps(["initial", "outside-window"]),))
        self.assertEqual(self.preview()["existing_tasks"], [])
        self.review()
        self.assertEqual(self.payloads[0]["existing_tasks"], [])


if __name__ == "__main__":
    unittest.main()
