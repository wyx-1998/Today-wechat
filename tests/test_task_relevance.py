"""Independent personal-task expectations. Generated accounts and fake models only."""
import copy
import json
from types import SimpleNamespace
import unittest

from backend import task_relevance as relevance
from backend.analysis import Analysis
from backend.action_discovery import DiscoveryAudit
from backend.contracts import ServiceError, iso_time
from backend.message_identity import identified
from tests import automatic_smoke
from tests.test_action_discovery import row, task
from scripts.audit_candidate_coverage import audit as inspect_audit


def message(i, content, flag=0, chat="group@chatroom", **extra):
    return row(i, chat=chat, content=content) | {"is_self": flag,
        "conversation_kind": "group", "_self_aliases": {"小伢儿"},
        "addressed_to_self": relevance.mentioned(content, {"小伢儿"}),
        "addressed_to_all": relevance.mentioned(content, {"所有人"}), **extra}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.answer = {"decisions": []}
        self.callback = None
        def request(prompt, **kwargs):
            payload = json.loads(prompt.partition("输入：")[2])
            self.calls.append((payload, kwargs))
            if self.callback:
                return self.callback(payload), None
            return self.answer, None
        self.analysis = SimpleNamespace(models=SimpleNamespace(request=request), build_preview=Analysis.build_preview)

    def run_gate(self, rows, tasks=None, **kwargs):
        tasks = tasks if tasks is not None else [task(rows[0]["message_id"])]
        return relevance.filter_tasks(self.analysis, tasks, rows, purpose="generated", guard=lambda: None, **kwargs)

    def decide(self, payload, kind, proofs, cid="R1"):
        refs = [{"source_message_id": payload["messages"][i]["source_message_id"],
            "quote": quote, "basis": basis} for i, quote, basis in proofs]
        return {"candidate_id": cid, "relationship": kind, "evidence": refs}

    def test_reported_third_party_mention_is_excluded_for_every_model_owner(self):
        rows = [message(1, "@李本源\u2005本源总，企微给您发了消息，抽空查看回复一下哦，感谢！")]
        for owner in ("self", "other", "shared", "unknown"):
            with self.subTest(owner=owner):
                self.assertEqual(self.run_gate(rows, [task(rows[0]["message_id"], "查看并回复企微消息") | {"owner_type": owner}]), [])
        self.assertEqual(self.calls, [])

    def test_general_group_task_without_personal_evidence_is_not_confirmation(self):
        self.assertEqual(self.run_gate([message(1, "明天提交厂级方案")]), [])

    def test_private_chat_keeps_previous_path_and_has_no_extra_call(self):
        rows = [message(1, "明天给你资料", conversation_kind="direct")]
        self.assertEqual(len(self.run_gate(rows)), 1)
        self.assertEqual(self.calls, [])

    def test_self_commitment_passes_with_original_title_and_deadline(self):
        rows = [message(1, "我明天发方案", 1)]
        self.callback = lambda p: {"decisions": [self.decide(p, "self", [(0, "我明天发方案", "self_action")])]}
        tasks = [task(rows[0]["message_id"], "发送方案") | {"due_date": "2026-10-11"}]
        result = self.run_gate(rows, tasks)
        self.assertEqual(result, [tasks[0] | {"owner_type": "self"}])
        self.assertEqual(self.calls[0][1]["purpose"], "generated_relevance")

    def test_incoming_assignment_to_confirmed_self_does_not_need_self_sender(self):
        rows = [message(1, "@小伢儿\u2005请发方案")]
        self.callback = lambda p: {"decisions": [self.decide(p, "self", [(0, "@小伢儿\u2005请发方案", "self_addressee")])]}
        self.assertEqual(self.run_gate(rows)[0]["owner_type"], "self")

    def test_other_sender_cannot_be_claimed_as_self_action_by_model(self):
        rows = [message(1, "@小伢儿 请发方案")]
        self.callback = lambda p: {"decisions": [self.decide(p, "self", [(0, "请发方案", "self_action")])]}
        self.assertEqual(self.run_gate(rows), [])

    def test_quote_must_itself_contain_confirmed_self_mention(self):
        rows = [message(1, "@小伢儿 请了解，@李本源 回复企微")]
        self.callback = lambda p: {"decisions": [self.decide(p, "self", [(0, "@李本源 回复企微", "self_addressee")])]}
        self.assertEqual(self.run_gate(rows), [])

    def test_waiting_requires_both_self_request_and_other_promise(self):
        rows = [message(1, "@同事 请发方案", 1), message(2, "好的，我明天发你")]
        self.callback = lambda p: {"decisions": [self.decide(p, "waiting", [(0, "请发方案", "self_request"), (1, "我明天发你", "other_commitment")])]}
        result = self.run_gate(rows)
        self.assertEqual(result[0]["owner_type"], "other")
        self.assertEqual(result[0]["source_message_ids"], [r["message_id"] for r in rows])
        self.callback = lambda p: {"decisions": [self.decide(p, "waiting", [(1, "我明天发你", "other_commitment")])]}
        self.assertEqual(self.run_gate(rows), [])

    def test_promise_before_request_is_not_waiting_evidence(self):
        rows = [message(1, "明天发方案"), message(2, "请提供方案", 1)]
        self.callback = lambda p: {"decisions": [self.decide(p, "waiting", [(1, "请提供方案", "self_request"), (0, "明天发方案", "other_commitment")])]}
        self.assertEqual(self.run_gate(rows), [])

    def test_same_group_personal_context_can_support_fresh_acceptance(self):
        fresh = [message(2, "好，明天发你")]
        context = [message(1, "请给我新方案", 1)]
        self.callback = lambda p: {"decisions": [self.decide(p, "waiting", [(0, "请给我新方案", "self_request"), (1, "明天发你", "other_commitment")])]}
        result = self.run_gate(fresh, context_rows=context)
        self.assertEqual(set(result[0]["source_message_ids"]), {context[0]["message_id"], fresh[0]["message_id"]})

    def test_another_groups_self_message_does_not_justify_candidate(self):
        rows = [message(1, "请发方案"), message(2, "我发方案", 1, chat="another@chatroom")]
        self.assertEqual(self.run_gate(rows), [])
        self.assertEqual(self.calls, [])

    def test_unknown_sender_does_not_prove_self(self):
        self.assertEqual(self.run_gate([message(1, "我来发方案", None)]), [])

    def test_receipt_of_third_party_task_is_not_self_commitment(self):
        rows = [message(1, "@李本源 请回复企微"), message(2, "收到", 1)]
        for basis in ("self_action", "self_acceptance", "self_request"):
            self.callback = lambda p: {"decisions": [self.decide(p, "self", [(1, "收到", basis)])]}
            with self.subTest(basis=basis):
                self.assertEqual(self.run_gate(rows), [])

    def test_short_acceptance_can_follow_explicit_self_assignment(self):
        rows = [message(1, "@小伢儿 请发方案"), message(2, "好的", 1)]
        self.callback = lambda p: {"decisions": [self.decide(p, "self", [
            (0, "@小伢儿 请发方案", "self_addressee"), (1, "好的", "self_acceptance")])]}
        self.assertEqual(self.run_gate(rows)[0]["owner_type"], "self")

    def test_collective_assignment_is_shared_not_confirmed_self(self):
        rows = [message(1, "@所有人 请分别提交材料")]
        self.callback = lambda p: {"decisions": [self.decide(p, "shared", [(0, "@所有人 请分别提交材料", "collective")])]}
        self.assertEqual(self.run_gate(rows)[0]["owner_type"], "shared")
        self.callback = lambda p: {"decisions": [self.decide(p, "self", [(0, "@所有人 请分别提交材料", "collective")])]}
        self.assertEqual(self.run_gate(rows), [])

    def test_uncertain_ownership_still_requires_personal_proof(self):
        rows = [message(1, "@小伢儿 请协调一下分工")]
        self.callback = lambda p: {"decisions": [self.decide(p, "uncertain", [(0, "@小伢儿 请协调一下分工", "self_addressee")])]}
        self.assertEqual(self.run_gate(rows)[0]["owner_type"], "unknown")
        self.callback = lambda p: {"decisions": [self.decide(p, "uncertain", [])]}
        self.assertEqual(self.run_gate(rows), [])

    def test_missing_duplicate_and_unrelated_decisions_are_not_retained(self):
        rows = [message(1, "我来发方案", 1)]
        for mode in ("missing", "duplicate", "unrelated"):
            def callback(p):
                d = self.decide(p, "self", [(0, "我来发方案", "self_action")])
                return {"decisions": [] if mode == "missing" else [d, d] if mode == "duplicate" else [dict(d, relationship="unrelated")]}
            self.callback = callback
            with self.subTest(mode=mode):
                self.assertEqual(self.run_gate(rows), [])

    def test_invalid_proof_does_not_block_independent_valid_task(self):
        rows = [message(1, "我发方案A", 1), message(2, "我发方案B", 1)]
        tasks = [task(r["message_id"], f"方案{i}") for i, r in enumerate(rows)]
        self.callback = lambda p: {"decisions": [self.decide(p, "self", [(0, "伪造原文", "self_action")]),
            self.decide(p, "self", [(1, "我发方案B", "self_action")], "R2")]}
        self.assertEqual(self.run_gate(rows, tasks), [tasks[1] | {"owner_type": "self"}])

    def test_cross_chat_borrowed_quote_is_rejected_even_in_same_check(self):
        rows = [message(1, "我发A", 1), message(2, "我发B", 1, chat="other@chatroom")]
        self.callback = lambda p: {"decisions": [self.decide(p, "self", [(1, "我发B", "self_action")])]}
        self.assertEqual(self.run_gate(rows, [task(rows[0]["message_id"]), task(rows[1]["message_id"])]), [])

    def test_invalid_envelope_and_unknown_result_are_not_success_or_retry(self):
        rows = [message(1, "我来发方案", 1)]
        self.answer = {"tasks": []}
        with self.assertRaises(ServiceError) as error:
            self.run_gate(rows)
        self.assertEqual(error.exception.code, "task_relation_format")
        self.assertEqual(len(self.calls), 1)

    def test_generation_guard_is_rechecked_after_model(self):
        rows = [message(1, "我来发方案", 1)]
        state = [True]
        def guard():
            if not state[0]:
                raise ServiceError("analysis_scope_changed", "changed")
        def callback(p):
            state[0] = False
            return {"decisions": [self.decide(p, "self", [(0, "我来发方案", "self_action")])]}
        self.callback = callback
        with self.assertRaises(ServiceError):
            self.run_gate(rows, check_guard=guard)

    def test_input_is_immutable_and_request_contains_no_sender_ids_or_alias_set(self):
        rows = [message(1, "我发方案", 1)]
        before = copy.deepcopy(rows)
        self.run_gate(rows)
        self.assertEqual(rows, before)
        wire = json.dumps(self.calls[0][0], ensure_ascii=False)
        self.assertNotIn("generated-sender", wire)
        self.assertNotIn("_self_aliases", wire)
        self.assertNotIn(rows[0]["message_id"], wire)

    def test_large_candidate_set_is_checked_in_bounded_groups(self):
        rows = [message(i, f"我发送方案{i}", 1) for i in range(21)]
        tasks = [task(r["message_id"], r["content"]) for r in rows]
        def answer(p):
            known = {r["source_message_id"]: r for r in p["messages"]}
            return {"decisions": [{"candidate_id": c["candidate_id"], "relationship": "self", "evidence": [
                {"source_message_id": c["source_message_ids"][0], "quote": known[c["source_message_ids"][0]]["content"], "basis": "self_action"}]}
                for c in p["candidates"]]}
        self.callback = answer
        self.assertEqual(len(self.run_gate(rows, tasks)), 21)
        self.assertEqual([len(p["candidates"]) for p, _ in self.calls], [20, 1])

    def test_unknown_model_result_is_not_automatically_retried(self):
        def answer(p):
            raise ServiceError("model_result_unknown", "unknown")
        self.callback = answer
        with self.assertRaises(ServiceError):
            self.run_gate([message(1, "我发送方案", 1)])
        self.assertEqual(len(self.calls), 1)

    def test_no_new_context_is_loaded_and_email_or_prefix_is_not_a_mention(self):
        for wording in ("小伢儿负责", "mail@小伢儿", "@小伢儿其他人 请处理"):
            with self.subTest(wording=wording):
                self.assertFalse(relevance.mentioned(wording, {"小伢儿"}))
        self.assertTrue(relevance.mentioned("@小伢儿\u2005请处理", {"小伢儿"}))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.h = automatic_smoke.AutomaticTests("runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.s = self.h.service
        self.h.source["self_id"] = "self-id"
        self.s.store.configure(self.h.source, iso_time(self.h.now-86400), [{"chat_id": "first", "chat_name": "Generated group", "chat_type": "group"}])
        self.h.select("first")
        with self.s.store.database() as db:
            db.execute("UPDATE messages SET sender_id='other-id',sender_name='Savin',is_self=0,content=? WHERE message_id='initial'",
                ("@李本源\u2005本源总，企微给您发了消息，抽空查看回复一下哦，感谢！",))
        self.bad = {"title": "查看并回复企微消息", "owner_type": "unknown", "due_date": None, "source_message_ids": ["initial"]}

    def test_actual_automatic_path_excludes_reported_case_without_modifying_source(self):
        with self.s.store.database() as db:
            before = tuple(db.execute("SELECT * FROM messages WHERE message_id='initial'").fetchone())
        self.h.answer({"tasks": [self.bad]})
        self.h.ready()
        self.assertEqual(self.s.automatic.tick(), {"status": "success", "task_count": 0})
        self.assertEqual(self.s.analysis.tasks()["items"], [])
        self.assertEqual(len(self.h.client.calls), 1)
        with self.s.store.database() as db:
            self.assertEqual(before, tuple(db.execute("SELECT * FROM messages WHERE message_id='initial'").fetchone()))
        self.assertIn("本人在群里", relevance.INSTRUCTION)

    def test_manual_new_text_uses_same_gate(self):
        self.h.answer({"tasks": [self.bad]})
        preview = self.s.analysis.real_preview(chat_id="first")
        result = self.s.analysis.run_real(chat_id="first", authorize=True, preview_token=preview["preview_token"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.s.analysis.tasks()["items"], [])

    def test_reanalysis_excludes_case_and_preserves_existing_user_modified_task(self):
        with self.s.store.database() as db:
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("old", self.h.source["account_id"], "manual-old",
                "保留已确认任务", "self", "self-id", None, "unknown", "completed", '["initial"]', iso_time(self.h.now), 7))
            before = tuple(db.execute("SELECT * FROM tasks WHERE task_id='old'").fetchone())
        self.h.client.generate = lambda *args, **kwargs: ({"tasks": [self.bad | {"existing_task_id": None}], "changes": []}, {"total_tokens": 12})
        preview = self.s.reanalysis.preview(chat_id="first")
        self.s.reanalysis.run(chat_id="first", authorize=True, preview_token=preview["preview_token"])
        with self.s.store.database() as db:
            self.assertEqual(before, tuple(db.execute("SELECT * FROM tasks WHERE task_id='old'").fetchone()))
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_confirmed_alias_and_duplicate_nickname_fail_closed(self):
        self.h.add("my-name", content="我先看下")
        with self.s.store.database() as db:
            db.execute("UPDATE messages SET sender_id='self-id',sender_name='小伢儿' WHERE message_id='my-name'")
            db.execute("UPDATE messages SET content='@小伢儿 请发方案' WHERE message_id='initial'")
            rows = [dict(r) for r in db.execute("SELECT * FROM messages WHERE message_id='initial'")]
        rows = identified(rows, self.h.source)
        annotated, _ = relevance.annotate(self.s.analysis, rows, [], self.h.source)
        self.assertTrue(annotated[0]["addressed_to_self"])
        self.h.add("same-name", content="其他人")
        with self.s.store.database() as db:
            db.execute("UPDATE messages SET sender_id='different-id',sender_name='小伢儿' WHERE message_id='same-name'")
        self.assertFalse(relevance.annotate(self.s.analysis, rows, [], self.h.source)[0][0]["addressed_to_self"])

    def test_annotation_uses_confirmed_id_not_historical_self_or_nickname(self):
        with self.s.store.database() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM messages WHERE message_id='initial'")]
        before = copy.deepcopy(rows)
        annotated, _ = relevance.annotate(self.s.analysis, identified(rows, self.h.source), [], self.h.source)
        self.assertEqual(annotated[0]["effective_is_self"], 0)
        self.assertFalse(annotated[0]["addressed_to_self"])
        self.assertEqual(rows, before)

    def test_automatic_personal_task_is_checked_and_published_with_counted_call(self):
        with self.s.store.database() as db:
            db.execute("UPDATE messages SET sender_id='self-id',sender_name='小伢儿',content='我来发方案' WHERE message_id='initial'")
        def generate(config, key, prompt, **kwargs):
            p = json.loads(prompt.partition("输入：")[2])
            if "decisions" in kwargs["schema"]["properties"]:
                m = p["messages"][0]
                self.assertEqual(m["speaker"], "本人")
                return {"decisions": [{"candidate_id": "R1", "relationship": "self", "evidence": [
                    {"source_message_id": m["source_message_id"], "quote": "我来发方案", "basis": "self_action"}]}]}, {"total_tokens": 5}
            return {"tasks": [dict(self.bad, title="发送方案")]}, {"total_tokens": 7}
        self.h.client.generate = generate
        self.h.ready()
        self.assertEqual(self.s.automatic.tick(), {"status": "success", "task_count": 1})
        item = self.s.analysis.tasks()["items"][0]
        self.assertEqual((item["owner_type"], item["status"], item["source_message_ids"]), ("self", "pending", ["initial"]))
        self.assertEqual(self.s.models.usage()["today"]["requests"], 2)
        with self.s.store.database() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT purpose FROM model_calls ORDER BY rowid")], ["automatic_text", "automatic_text_relevance"])

    def test_candidate_ledger_records_exclusion_and_remaps_remaining_task(self):
        self.h.add("own", content="我来发方案")
        with self.s.store.database() as db:
            db.execute("UPDATE messages SET sender_id='self-id',sender_name='小伢儿' WHERE message_id='own'")
            db.execute("INSERT INTO analysis_runs VALUES(?,?,?,?,?,?,?,?,?)", ("gate-audit", self.h.source["account_id"],
                '["initial","own"]', 1, 1, "success", iso_time(self.h.now), iso_time(self.h.now), None))
            rows = [dict(r) for r in db.execute("SELECT * FROM messages ORDER BY sent_epoch,message_id")]
        rows, _ = relevance.annotate(self.s.analysis, identified(rows, self.h.source), [], self.h.source)
        candidates = [self.bad, dict(self.bad, title="发送方案", source_message_ids=["own"])]
        audit = DiscoveryAudit(self.s.store, "gate-audit", lambda: self.h.now)
        audit.window(0, rows, [], [dict(t, candidate_id=f"C{i+1}") for i, t in enumerate(candidates)])
        audit.decisions([{ "candidate_id": f"C{i+1}", "outcome": "retained", "task_index": i,
            "source_message_ids": t["source_message_ids"], "reason": "提取候选"} for i, t in enumerate(candidates)])
        def generate(config, key, prompt, **kwargs):
            p = json.loads(prompt.partition("输入：")[2])
            own = next(m for m in p["messages"] if m["speaker"] == "本人")
            return {"decisions": [{"candidate_id": "R1", "relationship": "unrelated", "evidence": []},
                {"candidate_id": "R2", "relationship": "self", "evidence": [
                    {"source_message_id": own["source_message_id"], "quote": "我来发方案", "basis": "self_action"}]}]}, {"total_tokens": 8}
        self.h.client.generate = generate
        accepted = relevance.filter_tasks(self.s.analysis, candidates, rows, purpose="generated", guard=lambda: None, audit=audit)
        self.assertEqual(len(accepted), 1)
        report = inspect_audit(self.s.store.path, "gate-audit", [])
        self.assertEqual(report["decisions"], {"not_actionable": 1, "retained": 1})
        with self.s.store.database() as db:
            self.assertEqual(db.execute("SELECT task_index FROM discovery_candidates WHERE candidate_id='C2'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
