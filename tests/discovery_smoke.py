"""Multi-request batch transactions with generated accounts and memory keys."""

import json
import io
import unittest
import http.client

from backend.contracts import ServiceError
from backend.gemini import GeminiClient
from tests import automatic_smoke


class StageClient:
    def __init__(self):
        self.payloads = []
        self.prompts = []
        self.callback = None
        self.failure_step = None
        self.invalid_final = False
        self.invalid_discovery = False

    def generate(self, config, key, prompt, *, schema=None, json_response=True):
        self.prompts.append(prompt)
        payload = json.loads(prompt.partition("输入：")[2])
        self.payloads.append(payload)
        count = len(self.payloads)
        if self.callback:
            self.callback(count)
        if self.failure_step == count:
            raise ServiceError("model_result_unknown", "Generated unknown response")
        if "candidate_tasks" not in payload:
            ref = payload["new_messages"][0]["source_message_id"]
            answer = {"tasks": [{"title": "处理 " + ref, "owner_type": "unknown", "due_date": None,
                "source_message_ids": ["outside" if self.invalid_discovery else ref]}]}
        else:
            tasks, decisions = [], []
            for candidate in payload["candidate_tasks"]:
                task = {k: candidate[k] for k in ("title", "owner_type", "due_date", "source_message_ids")}
                if "existing_tasks" in payload:
                    target = next((t["task_id"] for t in payload["existing_tasks"]
                        if set(t["source_message_ids"]) & set(task["source_message_ids"])), None)
                    task["existing_task_id"] = target
                index = len(tasks)
                tasks.append(task)
                decisions.append({"candidate_id": candidate["candidate_id"], "outcome": "retained",
                    "task_index": index, "source_message_ids": candidate["source_message_ids"], "reason": "生成的明确请求"})
            answer = {"tasks": tasks, "candidate_decisions": [] if self.invalid_final else decisions}
            if "existing_tasks" in payload:
                answer["changes"] = []
        return answer, {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130}


class DiscoveryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.h = automatic_smoke.AutomaticTests("runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.service = self.h.service
        self.client = StageClient()

    def long_batch(self):
        for i in range(20):
            self.h.add(f"z-generated-{i:02d}", content="请提供资料" if i % 2 == 0 else "好，稍后给你")
        self.service.models.client = self.client
        self.h.ready()

    def long_review(self):
        self.h.ready()
        self.service.automatic.tick()
        self.service.pause()
        self.old = self.service.analysis.tasks()["items"][0]
        for i in range(20):
            self.h.add(f"z-generated-{i:02d}", content="请提供资料")
        self.service.models.client = self.client
        return self.service.reanalysis.preview(chat_id="first")

    def review(self, preview):
        return self.service.reanalysis.run(chat_id="first", authorize=True, preview_token=preview["preview_token"])

    def test_request_acceptance_rule_reaches_every_automatic_stage(self):
        from backend.action_discovery import REQUEST_EVIDENCE_INSTRUCTION, OBJECT_COMPLETION_INSTRUCTION
        self.long_batch()
        result = self.service.automatic.tick()
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(self.client.prompts), 3)
        for prompt in self.client.prompts:
            self.assertIn(REQUEST_EVIDENCE_INSTRUCTION, prompt)
            self.assertIn(OBJECT_COMPLETION_INSTRUCTION, prompt)
        self.assertIn("分别输出候选，可共享原请求或接受来源", self.client.prompts[0])

    def test_request_acceptance_rule_reaches_every_review_stage(self):
        from backend.action_discovery import REQUEST_EVIDENCE_INSTRUCTION, OBJECT_COMPLETION_INSTRUCTION
        result = self.review(self.long_review())
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(self.client.prompts), 3)
        for prompt in self.client.prompts:
            self.assertIn(REQUEST_EVIDENCE_INSTRUCTION, prompt)
            self.assertIn(OBJECT_COMPLETION_INSTRUCTION, prompt)
        self.assertIn("分别输出候选，可共享原请求或接受来源", self.client.prompts[0])

    def check_final_boundaries(self, payload):
        messages = payload.get("messages", [*payload.get("new_messages", []), *payload.get("context_messages", [])])
        known = {m["source_message_id"]: m for m in messages}
        self.assertTrue(payload["candidate_tasks"])
        for candidate in payload["candidate_tasks"]:
            last = candidate["request_last_message_id"]
            self.assertIn(last, candidate["source_message_ids"])
            self.assertEqual(known[last]["sent_at"], max(known[mid]["sent_at"] for mid in candidate["source_message_ids"]))
            self.assertEqual(candidate["later_evidence_possible"], any(
                m["sent_at"] > known[last]["sent_at"] and m["source_message_id"] not in candidate["source_message_ids"]
                for m in messages))

    def test_automatic_final_prompt_has_frozen_request_boundaries(self):
        self.long_batch()
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        self.check_final_boundaries(self.client.payloads[-1])
        self.assertIn("later_evidence_possible=false", self.client.prompts[-1])
        self.assertEqual(len(self.client.payloads), 3)

    def test_review_final_prompt_has_frozen_request_boundaries_and_keeps_audit_base(self):
        self.assertEqual(self.review(self.long_review())["status"], "success")
        self.check_final_boundaries(self.client.payloads[-1])
        self.assertIn("request_last_message_id", self.client.prompts[-1])
        with self.service.store.database() as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(discovery_candidates)")}
        self.assertNotIn("request_last_message_id", columns)
        self.assertNotIn("later_evidence_possible", columns)

    def completion_response(self, *, valid):
        self.long_batch()
        self.h.add("generated-completion", content="生成的交付完成依据", sent=self.h.now-300,
                   arrival=self.h.now-600)
        original = self.client.generate
        def generate(config, key, prompt, **kwargs):
            answer, usage = original(config, key, prompt, **kwargs)
            payload = self.client.payloads[-1]
            if "candidate_tasks" in payload:
                first = answer["candidate_decisions"][0]
                first.update(outcome="fulfilled", task_index=None, reason="生成的后续交付证据")
                if valid:
                    proof = next(m["source_message_id"] for m in payload["new_messages"]
                                 if m["content"] == "生成的交付完成依据")
                    first["source_message_ids"] = [*first["source_message_ids"], proof]
                answer["tasks"] = answer["tasks"][1:]
                for item in answer["candidate_decisions"][1:]:
                    item["task_index"] -= 1
            return answer, usage
        self.client.generate = generate

    def test_valid_distinct_later_completion_and_remaining_task_publish_atomically(self):
        self.completion_response(valid=True)
        result = self.service.automatic.tick()
        self.assertEqual(result, {"status": "success", "task_count": 1})
        self.assertEqual([c["outcome"] for c in self.audit_rows("discovery_candidates")],
                         ["fulfilled", "retained"])
        self.assertTrue(self.client.payloads[-1]["candidate_tasks"][0]["later_evidence_possible"])
        self.assertEqual(self.service.models.usage()["today"]["known_total_tokens"], 390)

    def test_missing_completion_evidence_isolates_one_candidate_and_publishes_independent_task(self):
        self.completion_response(valid=False)
        result = self.service.automatic.tick()
        self.assertEqual(result, {"status": "success", "task_count": 1})
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 1)
        self.assertEqual([c["outcome"] for c in self.audit_rows("discovery_candidates")], [None, "retained"])
        self.assertEqual(self.service.analysis.tasks()["last_analysis"]["rejected_candidate_count"], 1)
        self.assertEqual(self.audit_rows("discovery_rejections")[0]["rule"], "completion_missing_later_evidence")
        self.assertEqual(self.service.models.usage()["today"]["known_total_tokens"], 390)
        self.service.automatic.tick()
        self.assertEqual(len(self.client.payloads), 3)

    def test_final_timeout_keeps_candidate_history_unknown_usage_and_never_auto_resends(self):
        self.long_batch()
        original = self.client.generate
        def generate(*args, **kwargs):
            answer, usage = original(*args, **kwargs)
            if "candidate_tasks" in self.client.payloads[-1]:
                raise ServiceError("model_timeout_unknown", "生成的超时未知")
            return answer, usage
        self.client.generate = generate
        result = self.service.automatic.tick()
        self.assertEqual(result, {"status": "unknown", "error_code": "model_timeout_unknown"})
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual([c["outcome"] for c in self.audit_rows("discovery_candidates")], [None, None])
        usage = self.service.models.usage()["today"]
        self.assertEqual(usage["known_total_tokens"], 260)
        self.assertEqual(usage["unknown_usage_requests"], 1)
        self.assertEqual(self.h.batches()[0]["status"], "unknown")
        self.service.automatic.tick()
        self.assertEqual(len(self.client.payloads), 3)

    def test_review_final_timeout_preserves_existing_task_and_candidate_records(self):
        preview = self.long_review()
        original = self.client.generate
        def generate(*args, **kwargs):
            answer, usage = original(*args, **kwargs)
            if "candidate_tasks" in self.client.payloads[-1]:
                raise ServiceError("model_timeout_unknown", "生成的超时未知")
            return answer, usage
        self.client.generate = generate
        with self.assertRaises(ServiceError) as caught:
            self.review(preview)
        self.assertEqual(caught.exception.code, "model_timeout_unknown")
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 1)
        current = self.service.analysis.tasks()["items"][0]
        self.assertEqual((current["title"], current["status"], current["revision"]),
                         (self.old["title"], self.old["status"], self.old["revision"]))
        self.assertEqual(current["changes"], [])
        self.assertEqual([c["outcome"] for c in self.audit_rows("discovery_candidates")], [None, None])
        self.assertEqual(self.service.models.usage()["today"]["unknown_usage_requests"], 1)

    def test_automatic_runs_two_focal_windows_then_publishes_one_batch(self):
        self.long_batch()
        self.client.callback = lambda step: self.assertEqual(self.service.analysis.tasks()["items"], [])
        result = self.service.automatic.tick()
        self.assertEqual(result, {"status": "success", "task_count": 2})
        self.assertEqual(len(self.client.payloads), 3)
        self.assertEqual([len(p["new_messages"]) for p in self.client.payloads[:2]], [20, 1])
        self.assertEqual(len(self.client.payloads[2]["new_messages"]), 21)
        self.assertEqual(self.h.batches()[0]["status"], "success")
        self.assertEqual(self.service.models.usage()["today"]["known_total_tokens"], 390)

    def test_pause_after_first_response_stops_subsequent_requests_and_publication(self):
        self.long_batch()
        self.client.callback = lambda step: self.service.pause() if step == 1 else None
        self.assertEqual(self.service.automatic.tick()["status"], "failed")
        self.assertEqual(len(self.client.payloads), 1)
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual(self.service.models.usage()["today"]["known_total_tokens"], 130)
        self.service.resume()
        self.service.automatic.tick()
        self.assertEqual(len(self.client.payloads), 1)

    def test_model_disable_after_first_response_stops_subsequent_requests(self):
        self.long_batch()
        self.client.callback = lambda step: self.service.models.enable(enabled=False) if step == 1 else None
        self.assertEqual(self.service.automatic.tick()["status"], "failed")
        self.assertEqual(len(self.client.payloads), 1)
        self.assertEqual(self.service.analysis.tasks()["items"], [])

    def test_scope_change_after_first_response_does_not_continue_or_publish(self):
        self.long_batch()
        self.client.callback = lambda step: self.service.store.update_chat("first", sync_enabled=False, analysis_enabled=False) if step == 1 else None
        result = self.service.automatic.tick()
        self.assertIn(result["status"], {"failed", "cancelled"})
        self.assertEqual(result["error_code"], "analysis_scope_changed")
        self.assertEqual(len(self.client.payloads), 1)
        self.assertEqual(self.service.analysis.tasks()["items"], [])

    def test_unknown_second_response_keeps_first_usage_and_never_auto_retries(self):
        self.long_batch()
        self.client.failure_step = 2
        self.assertEqual(self.service.automatic.tick()["status"], "unknown")
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual(len(self.client.payloads), 2)
        usage = self.service.models.usage()["today"]
        self.assertEqual(usage["known_total_tokens"], 130)
        self.assertEqual(usage["unknown_usage_requests"], 1)
        self.service.automatic.tick()
        self.assertEqual(len(self.client.payloads), 2)

    def test_bad_discovery_source_stops_before_second_stage(self):
        self.long_batch()
        self.client.invalid_discovery = True
        self.assertEqual(self.service.automatic.tick()["error_code"], "analysis_evidence")
        self.assertEqual(len(self.client.payloads), 1)
        self.assertEqual(self.service.analysis.tasks()["items"], [])

    def test_incomplete_final_ledger_rejects_whole_batch_after_counting_all_requests(self):
        self.long_batch()
        self.client.invalid_final = True
        self.assertEqual(self.service.automatic.tick()["error_code"], "discovery_reconciliation")
        self.assertEqual(len(self.client.payloads), 3)
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual(self.service.models.usage()["today"]["known_total_tokens"], 390)

    def test_restart_after_partial_failure_does_not_resend(self):
        self.long_batch()
        self.client.failure_step = 2
        self.service.automatic.tick()
        self.service.close()
        self.service = self.h.open_service()
        self.service.models.client = self.client
        self.service.automatic.tick()
        self.assertEqual(len(self.client.payloads), 2)
        self.assertEqual(self.service.analysis.tasks()["items"], [])

    def test_long_review_preserves_existing_fields_until_explicit_acceptance(self):
        preview = self.long_review()
        result = self.review(preview)
        self.assertEqual(len(self.client.payloads), 3)
        self.assertEqual(result["status"], "success")
        current = next(t for t in self.service.analysis.tasks()["items"] if t["task_id"] == self.old["task_id"])
        self.assertEqual((current["title"], current["status"], current["revision"]),
                         (self.old["title"], self.old["status"], self.old["revision"]))
        self.assertEqual(len(current["changes"]), 1)

    def test_review_scope_change_during_discovery_preserves_old_tasks(self):
        preview = self.long_review()
        self.client.callback = lambda step: self.service.store.update_chat("first", sync_enabled=False, analysis_enabled=False) if step == 1 else None
        with self.assertRaises(ServiceError) as error:
            self.review(preview)
        self.assertEqual(error.exception.code, "real_chat_scope")
        self.assertEqual(len(self.client.payloads), 1)
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 1)

    def test_failed_review_does_not_publish_partial_new_tasks_or_suggestions(self):
        preview = self.long_review()
        self.client.invalid_final = True
        with self.assertRaises(ServiceError):
            self.review(preview)
        self.assertEqual(len(self.client.payloads), 3)
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 1)
        self.assertEqual(self.service.analysis.tasks()["items"][0]["changes"], [])

    def test_user_completed_state_during_discovery_is_not_overwritten(self):
        preview = self.long_review()
        self.client.callback = lambda step: self.service.analysis.update_task(task_id=self.old["task_id"],
            revision=self.old["revision"], status="completed") if step == 1 else None
        self.review(preview)
        current = next(t for t in self.service.analysis.tasks()["items"] if t["task_id"] == self.old["task_id"])
        self.assertEqual(current["status"], "completed")

    def test_pause_before_first_send_keeps_batch_queued_without_unlock_or_request(self):
        self.long_batch()
        original = self.service.analysis.candidates
        def paused(*args, **kwargs):
            self.service.pause()
            return original(*args, **kwargs)
        self.service.analysis.candidates = paused
        self.h.keychain.value = None
        self.assertEqual(self.service.automatic.tick()["status"], "queued")
        self.assertEqual(self.client.payloads, [])
        self.assertEqual(self.service.models.usage()["today"]["requests"], 0)

    def test_changed_frozen_message_after_discovery_stops_next_send(self):
        self.long_batch()
        def change(step):
            if step == 1:
                with self.service.store.database() as db:
                    db.execute("UPDATE messages SET content='changed' WHERE message_id='initial'")
        self.client.callback = change
        self.assertEqual(self.service.automatic.tick()["error_code"], "batch_source_changed")
        self.assertEqual(len(self.client.payloads), 1)
        self.assertEqual(self.service.analysis.tasks()["items"], [])

    def test_close_after_first_response_stops_further_requests(self):
        self.long_batch()
        self.client.callback = lambda step: self.service.close() if step == 1 else None
        self.assertEqual(self.service.automatic.tick()["error_code"], "service_closed")
        self.assertEqual(len(self.client.payloads), 1)
        self.assertEqual(self.service.analysis.tasks()["items"], [])

    def test_actual_gemini_adapter_round_trip_across_all_three_requests(self):
        self.long_batch()
        class Opener:
            def open(inner, request, timeout):
                payload = json.loads(request.data)
                wire = payload["contents"][0]["parts"][0]["text"]
                answer, _ = self.client.generate({}, "unused", wire,
                    schema=payload["generationConfig"]["responseSchema"])
                return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(answer)}]}}],
                    "usageMetadata": {"totalTokenCount": 130}}).encode())
        self.service.models.client = GeminiClient(opener=Opener())
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        refs = [r for t in self.service.analysis.tasks()["items"] for r in t["source_message_ids"]]
        self.assertCountEqual(refs, ["initial", "z-generated-19"])
        self.assertEqual(len(self.client.payloads), 3)
        self.assertEqual(self.service.models.usage()["today"]["known_total_tokens"], 390)

    def transport_adapter(self, exception):
        class Opener:
            calls = 0
            def open(inner, request, timeout):
                inner.calls += 1
                payload = json.loads(request.data)
                wire = payload["contents"][0]["parts"][0]["text"]
                if "candidate_decisions" in payload["generationConfig"]["responseSchema"]["properties"]:
                    raise exception
                answer, _ = self.client.generate({}, "unused", wire,
                    schema=payload["generationConfig"]["responseSchema"])
                return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP",
                    "content": {"parts": [{"text": json.dumps(answer)}]}}],
                    "usageMetadata": {"totalTokenCount": 130}}).encode())
        opener = Opener()
        self.service.models.client = GeminiClient(opener=opener)
        return opener

    def test_remote_close_is_unknown_and_automatic_batch_never_replays_it(self):
        self.long_batch()
        opener = self.transport_adapter(http.client.RemoteDisconnected("generated-private-error"))
        self.assertEqual(self.service.automatic.tick(), {"status": "unknown", "error_code": "model_connection_closed_unknown"})
        self.assertEqual(self.h.batches()[0]["status"], "unknown")
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual([c["outcome"] for c in self.audit_rows("discovery_candidates")], [None, None])
        self.assertEqual(self.service.models.usage()["today"]["unknown_usage_requests"], 1)
        self.service.automatic.tick()
        self.assertEqual(opener.calls, 3)
        self.assertEqual(self.audit_rows("model_calls")[-1]["outcome"], "unknown")

    def test_incomplete_review_response_keeps_old_user_fields_and_saves_unknown_classification(self):
        preview = self.long_review()
        opener = self.transport_adapter(http.client.IncompleteRead(b"generated-private-partial", 999))
        with self.assertRaises(ServiceError) as error:
            self.review(preview)
        self.assertEqual(error.exception.code, "model_response_incomplete_unknown")
        current = self.service.analysis.tasks()["items"][0]
        self.assertEqual((current["title"], current["status"], current["revision"]),
                         (self.old["title"], self.old["status"], self.old["revision"]))
        self.assertEqual(current["changes"], [])
        self.assertEqual(self.audit_rows("model_calls")[-1]["outcome"], "unknown")
        self.assertEqual(self.audit_rows("model_calls")[-1]["error_code"], "model_response_incomplete_unknown")
        self.assertEqual(opener.calls, 3)

    def audit_rows(self, table):
        with self.service.store.database() as db:
            return [dict(r) for r in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]

    def test_successful_automatic_records_each_window_and_decision(self):
        self.long_batch()
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        windows = self.audit_rows("discovery_windows")
        candidates = self.audit_rows("discovery_candidates")
        self.assertEqual([len(json.loads(w["focal_message_ids"])) for w in windows], [20, 1])
        self.assertEqual([c["candidate_id"] for c in candidates], ["C1", "C2"])
        self.assertEqual([c["outcome"] for c in candidates], ["retained", "retained"])
        self.assertCountEqual([r for c in candidates for r in json.loads(c["source_message_ids"])], ["initial", "z-generated-19"])

    def test_unknown_second_response_keeps_valid_first_stage_not_task(self):
        self.long_batch()
        self.client.failure_step = 2
        self.assertEqual(self.service.automatic.tick()["status"], "unknown")
        self.assertEqual(len(self.audit_rows("discovery_windows")), 1)
        self.assertIsNone(self.audit_rows("discovery_candidates")[0]["outcome"])
        self.assertEqual(self.service.analysis.tasks()["items"], [])

    def test_retry_creates_new_attempt_without_deleting_old_diagnostics(self):
        self.long_batch()
        self.client.failure_step = 2
        self.service.automatic.tick()
        old = self.audit_rows("discovery_candidates")[0]
        batch = self.h.batches()[0]
        self.service.automatic.retry(batch_id=batch["batch_id"], authorize=True)
        self.client.failure_step = None
        self.assertEqual(self.service.automatic.tick()["status"], "success")
        self.assertEqual(len(self.audit_rows("discovery_attempts")), 2)
        candidates = self.audit_rows("discovery_candidates")
        self.assertEqual(candidates[0], old)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 2)

    def test_invalid_final_ledger_is_not_saved_as_a_valid_decision(self):
        self.long_batch()
        self.client.invalid_final = True
        self.service.automatic.tick()
        self.assertEqual(len(self.audit_rows("discovery_windows")), 2)
        self.assertEqual([c["outcome"] for c in self.audit_rows("discovery_candidates")], [None, None])

    def test_audit_save_failure_stops_further_requests_without_partial_task(self):
        self.long_batch()
        with self.service.store.database() as db:
            db.execute("CREATE TRIGGER generated_audit_failure BEFORE INSERT ON discovery_windows BEGIN SELECT RAISE(ABORT,'generated'); END")
        self.assertEqual(self.service.automatic.tick()["error_code"], "discovery_store_failed")
        self.assertEqual(len(self.client.payloads), 1)
        self.assertEqual(self.audit_rows("discovery_windows"), [])
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual(self.service.models.usage()["today"]["known_total_tokens"], 130)

    def test_real_review_records_discovery_without_overwriting_user_fields(self):
        preview = self.long_review()
        self.review(preview)
        self.assertEqual(len(self.audit_rows("discovery_windows")), 2)
        self.assertEqual([c["outcome"] for c in self.audit_rows("discovery_candidates")], ["retained", "retained"])
        current = next(t for t in self.service.analysis.tasks()["items"] if t["task_id"] == self.old["task_id"])
        self.assertEqual((current["title"], current["status"], current["revision"]),
                         (self.old["title"], self.old["status"], self.old["revision"]))

    def test_restart_preserves_diagnostics_without_resending(self):
        self.long_batch()
        self.client.failure_step = 2
        self.service.automatic.tick()
        before = self.audit_rows("discovery_candidates")
        self.service.close()
        self.service = self.h.open_service()
        self.service.models.client = self.client
        self.service.automatic.tick()
        self.assertEqual(self.audit_rows("discovery_candidates"), before)
        self.assertEqual(len(self.client.payloads), 2)

    def test_three_real_adapter_responses_store_original_ids_not_wire_aliases(self):
        self.test_actual_gemini_adapter_round_trip_across_all_three_requests()
        for candidate in self.audit_rows("discovery_candidates"):
            self.assertIn(json.loads(candidate["source_message_ids"])[0], {"initial", "z-generated-19"})
            self.assertIn(json.loads(candidate["decision_source_message_ids"])[0], {"initial", "z-generated-19"})

    def configure_identity(self, self_id="generated-self"):
        with self.service.store.database() as db:
            db.execute("UPDATE messages SET sender_id=?,sender_name='Shared nickname'", (self_id,))
        state = self.service.store.settings()
        self.service.store.configure(state["source"] | {"self_id": self_id}, state["time_start"], [])

    def test_automatic_uses_identity_for_new_and_previous_context_without_rewriting(self):
        self.h.add("previous"); self.h.processed("previous")
        self.configure_identity()
        self.h.ready()
        self.service.automatic.tick()
        self.assertEqual(self.h.client.payloads[0][0]["speaker"], "本人")
        self.assertEqual(self.h.client.context_payloads[0][0]["speaker"], "本人")
        with self.service.store.database() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT is_self FROM messages")], [None, None])

    def test_continuous_preview_uses_identity_for_historical_unknown_senders(self):
        self.configure_identity()
        preview = self.service.automatic.preview()
        self.assertEqual(preview["messages"][0]["speaker"], "本人")

    def test_manual_real_preview_uses_identity_without_changing_raw_hash(self):
        self.configure_identity()
        preview = self.service.analysis.real_preview(chat_id="first")
        self.assertEqual(preview["messages"][0]["speaker"], "本人")
        with self.service.store.database() as db:
            self.assertIsNone(db.execute("SELECT is_self FROM messages").fetchone()[0])

    def test_discovery_and_whole_review_consistently_use_identity(self):
        preview = self.long_review()
        self.configure_identity()
        preview = self.service.reanalysis.preview(chat_id="first")
        self.assertTrue(all(m["speaker"] == "本人" for m in preview["messages"]))
        self.review(preview)
        for payload in self.client.payloads:
            messages = payload.get("new_messages", payload.get("messages", [])) + payload.get("context_messages", [])
            self.assertTrue(all(m["speaker"] == "本人" for m in messages))

    def test_identity_change_invalidates_old_authorization_and_review_preview(self):
        preview = self.long_review()
        self.configure_identity()
        self.assertEqual(self.service.automatic.public()["grant"]["status"], "invalid")
        with self.assertRaises(ServiceError) as error:
            self.review(preview)
        self.assertEqual(error.exception.code, "review_preview_changed")
        self.assertEqual(self.client.payloads, [])

    def test_identity_change_during_first_discovery_stops_later_send(self):
        self.long_batch()
        self.client.callback = lambda step: self.configure_identity() if step == 1 else None
        self.assertIn(self.service.automatic.tick()["status"], {"failed", "cancelled"})
        self.assertEqual(len(self.client.payloads), 1)
        self.assertEqual(self.service.analysis.tasks()["items"], [])

    def test_review_can_propose_identity_correction_without_changing_completed_task(self):
        preview = self.long_review()
        self.configure_identity()
        self.service.analysis.update_task(task_id=self.old["task_id"], revision=1, status="completed")
        original = self.client.generate
        def generate(*args, **kwargs):
            answer, usage = original(*args, **kwargs)
            for task in answer["tasks"]:
                task["owner_type"] = "self"
            return answer, usage
        self.client.generate = generate
        self.review(self.service.reanalysis.preview(chat_id="first"))
        current = next(t for t in self.service.analysis.tasks()["items"] if t["task_id"] == self.old["task_id"])
        self.assertEqual((current["owner_type"], current["status"], current["revision"]), ("unknown", "completed", 2))
        self.assertEqual(current["changes"][0]["proposed"]["owner_type"], "self")

    def partial_review(self):
        preview = self.long_review()
        self.service.analysis.update_task(task_id=self.old["task_id"], revision=1, status="completed")
        original = self.client.generate
        def generate(*args, **kwargs):
            answer, usage = original(*args, **kwargs)
            if "candidate_tasks" in self.client.payloads[-1]:
                answer["candidate_decisions"][0].update(outcome="fulfilled", task_index=None)
                answer["tasks"] = answer["tasks"][1:]
                for d in answer["candidate_decisions"][1:]:
                    d["task_index"] -= 1
                answer["changes"] = [{"task_id": self.old["task_id"], "kind": "completed",
                    "source_message_ids": answer["candidate_decisions"][0]["source_message_ids"]}]
            return answer, usage
        self.client.generate = generate
        return self.service.reanalysis.preview(chat_id="first")

    def test_partial_review_publishes_new_task_but_keeps_user_status_and_rejects_dependent_change(self):
        result = self.review(self.partial_review())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["task_count"], 1)
        self.assertEqual(result["rejected_candidate_count"], 1)
        self.assertEqual(result["rejected_change_count"], 1)
        current = next(t for t in self.service.analysis.tasks()["items"] if t["task_id"] == self.old["task_id"])
        self.assertEqual((current["status"], current["revision"], current["title"]), ("completed", 2, self.old["title"]))
        self.assertEqual(current["changes"], [])
        self.assertEqual([c["outcome"] for c in self.audit_rows("discovery_candidates")], [None, "retained"])

    def test_partial_publication_storage_failure_rolls_back_new_tasks_and_keeps_old_user_state(self):
        preview = self.partial_review()
        with self.service.store.database() as db:
            db.execute("CREATE TRIGGER deny_partial BEFORE INSERT ON tasks BEGIN SELECT RAISE(ABORT,'generated fault'); END")
        with self.assertRaises(ServiceError) as error:
            self.review(preview)
        self.assertEqual(error.exception.code, "review_store_failed")
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 1)
        current = self.service.analysis.tasks()["items"][0]
        self.assertEqual((current["status"], current["revision"]), ("completed", 2))
        self.assertEqual(current["changes"], [])

    def test_scope_change_on_final_response_still_blocks_partial_publication(self):
        preview = self.partial_review()
        self.client.callback = lambda step: self.service.store.update_chat("first", sync_enabled=True,
            analysis_enabled=False, display_name=None, tag_ids=[]) if step == 3 else None
        with self.assertRaises(ServiceError) as error:
            self.review(preview)
        self.assertEqual(error.exception.code, "analysis_scope_changed")
        self.assertEqual(len(self.service.analysis.tasks()["items"]), 1)
        self.assertEqual(len(self.client.payloads), 3)


if __name__ == "__main__":
    unittest.main()
