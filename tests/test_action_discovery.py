"""Generated focal windows and decision contracts, no private/network I/O."""

import json
import unittest

from backend.analysis import Analysis, SCHEMA
from backend import action_discovery as discovery
from backend.contracts import ServiceError, iso_time
from backend.model_references import compact_prompt
from backend.reanalysis import REVIEW_SCHEMA


def row(number, *, chat="one", account="generated", content="请提供资料"):
    return {"message_id": f"generated-{number:04d}", "account_id": account, "chat_id": chat,
            "sender_id": "generated-sender", "sent_epoch": 1700000000+number,
            "sent_at": iso_time(1700000000+number), "content": content, "is_self": None}


def task(ref, title="提供资料"):
    return {"title": title, "owner_type": "unknown", "due_date": None, "source_message_ids": [ref]}


def decision(cid="C1", *, outcome="retained", index=0, refs=None):
    return {"candidate_id": cid, "outcome": outcome, "task_index": index,
            "source_message_ids": refs or [row(0)["message_id"]], "reason": "原文明确要求提供资料"}


class DiscoveryUnitTests(unittest.TestCase):
    def setUp(self):
        self.rows = [row(0), row(1, content="资料已发")]
        self.tasks = [task(self.rows[0]["message_id"])]
        self.candidates = [self.tasks[0] | {"candidate_id": "C1"}]

    def check(self, decisions, *, tasks=None):
        return discovery.validate_decisions({"candidate_decisions": decisions}, self.candidates,
            self.tasks if tasks is None else tasks, self.rows)

    def test_focal_messages_covered_exactly_once_and_in_time_order(self):
        rows = [row(i) for i in range(45)]
        windows = discovery.windows(list(reversed(rows)))
        self.assertEqual([len(f) for f, _ in windows], [20, 20, 5])
        self.assertEqual([r["message_id"] for f, _ in windows for r in f], [r["message_id"] for r in rows])
        for focal, neighbors in windows:
            self.assertFalse({r["message_id"] for r in focal} & {r["message_id"] for r in neighbors})

    def test_neighboring_acceptance_crosses_segment_boundary(self):
        rows = [row(i) for i in range(21)]
        windows = discovery.windows(rows)
        self.assertEqual(windows[0][1][0]["message_id"], rows[20]["message_id"])
        self.assertEqual([r["message_id"] for r in windows[1][1]], [r["message_id"] for r in rows[17:20]])

    def test_old_context_is_neighbor_only_not_focal(self):
        rows = [row(5), row(7)]
        contexts = [row(i) for i in (0, 4, 6, 8, 9, 10, 11, 100)]
        windows = discovery.windows(rows, contexts)
        self.assertEqual([r["message_id"] for r in windows[0][0]], [r["message_id"] for r in rows])
        self.assertNotIn(row(100)["message_id"], [r["message_id"] for r in windows[0][1]])

    def test_sparse_focal_records_do_not_upload_entire_context_gap(self):
        windows = discovery.windows([row(0), row(100)], [row(i) for i in range(1, 100)])
        self.assertEqual(len(windows[0][1]), 6)
        self.assertNotIn(row(50)["message_id"], [r["message_id"] for r in windows[0][1]])

    def test_accounts_and_chats_never_share_neighbors(self):
        rows = [row(1), row(2, chat="other"), row(3, account="other-account")]
        windows = discovery.windows(rows)
        self.assertEqual(len(windows), 3)
        for focal, neighbors in windows:
            self.assertEqual(len(focal), 1)
            self.assertEqual(neighbors, [])

    def test_character_boundary_preserves_entire_oversized_message(self):
        rows = [row(0, content="字"*7000), row(1, content="字"*7000), row(2, content="字"*15000)]
        windows = discovery.windows(rows)
        self.assertEqual([len(f) for f, _ in windows], [1, 1, 1])
        self.assertEqual(windows[2][0][0]["content"], "字"*15000)

    def test_duplicate_input_is_rejected(self):
        with self.assertRaises(ServiceError):
            discovery.windows([row(0)], [row(0)])

    def test_short_inputs_keep_one_pass_large_inputs_use_discovery(self):
        self.assertFalse(discovery.needed([row(i) for i in range(20)]))
        self.assertTrue(discovery.needed([row(i) for i in range(21)]))
        self.assertTrue(discovery.needed([row(0, content="字"*12001)]))

    def test_valid_retained_candidate_links_to_published_task(self):
        self.check([decision()])

    def test_completion_guidance_requires_object_and_partial_action_matching(self):
        instruction = discovery.OBJECT_COMPLETION_INSTRUCTION
        self.assertIn(instruction, discovery.REQUEST_EVIDENCE_INSTRUCTION)
        for requirement in ("同一交付对象和动作", "紧邻的请求", "完成其中一项不能排除剩余动作",
                            "不编造确定完成或取消"):
            self.assertIn(requirement, instruction)
        self.assertIn("培训手册", instruction)
        self.assertIn("检修方案", instruction)

    def test_independent_actions_sharing_request_can_have_different_decisions(self):
        rows = [row(0, content="请更新培训手册，并给采购清单添加链接"),
                row(1, content="采购清单链接已添加，培训手册仍要整理")]
        original = rows[0]["message_id"]
        tasks = [task(original, "更新培训手册")]
        candidates = [tasks[0] | {"candidate_id": "C1"},
                      task(original, "给采购清单添加链接") | {"candidate_id": "C2"}]
        decisions = [decision(), decision("C2", outcome="fulfilled", index=None,
                         refs=[r["message_id"] for r in rows])]
        discovery.validate_decisions({"candidate_decisions": decisions}, candidates, tasks, rows)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "更新培训手册")
        with self.assertRaises(ServiceError):
            discovery.validate_decisions({"candidate_decisions": decisions[1:]}, candidates, tasks, rows)

    def test_missing_duplicate_or_unknown_decision_rejected(self):
        for decisions in ([], [decision(), decision()], [decision("C999")]):
            with self.assertRaises(ServiceError):
                self.check(decisions)

    def test_unrelated_source_or_task_index_rejected(self):
        for item in (decision(refs=[row(1)["message_id"]]), decision(index=99), decision(index=True)):
            with self.assertRaises(ServiceError):
                self.check([item])
        with self.assertRaises(ServiceError):
            self.check([decision()], tasks=[task(row(1)["message_id"])])

    def test_completed_candidate_requires_distinct_later_evidence(self):
        for kind in ("fulfilled", "cancelled"):
            self.check([decision(outcome=kind, index=None, refs=[r["message_id"] for r in self.rows])])
            with self.assertRaises(ServiceError):
                self.check([decision(outcome=kind, index=None)])
            with self.assertRaises(ServiceError):
                self.check([decision(outcome=kind, index=0, refs=[r["message_id"] for r in self.rows])])

    def test_earlier_evidence_does_not_count_as_later_completion(self):
        candidate = [task(row(1)["message_id"]) | {"candidate_id": "C1"}]
        with self.assertRaises(ServiceError):
            discovery.validate_decisions({"candidate_decisions": [decision(outcome="fulfilled", index=None,
                refs=[r["message_id"] for r in self.rows])]}, candidate, [], self.rows)

    def test_non_action_decision_requires_original_source_and_reason(self):
        self.check([decision(outcome="not_actionable", index=None)])
        item = decision(outcome="not_actionable", index=None) | {"reason": " "}
        with self.assertRaises(ServiceError):
            self.check([item])

    def test_result_cannot_silently_drop_decision_field_or_add_fields(self):
        for answer in ({"tasks": []}, {"tasks": [], "candidate_decisions": [], "extra": True}):
            with self.assertRaises(ServiceError):
                discovery.strip_decisions(answer, expected_fields=("tasks",))
        self.assertEqual(discovery.strip_decisions({"tasks": [], "candidate_decisions": []}, expected_fields=("tasks",)), {"tasks": []})

    def test_short_aliases_include_candidates_and_restore_decision_sources(self):
        schema = discovery.schema_with_decisions(REVIEW_SCHEMA)
        payload = {"messages": Analysis.build_preview(self.rows)["messages"], "existing_tasks": [],
                   "candidate_tasks": self.candidates}
        wire, references = compact_prompt("输入：" + json.dumps(payload), schema)
        data = json.loads(wire.partition("输入：")[2])
        self.assertEqual(data["candidate_tasks"][0]["source_message_ids"], ["M1"])
        self.assertEqual(data["candidate_tasks"][0]["candidate_id"], "C1")
        answer = {"tasks": [], "changes": [], "candidate_decisions": [decision(outcome="fulfilled", index=None, refs=["M1", "M2"])]}
        self.assertEqual(references.restore(answer)["candidate_decisions"][0]["source_message_ids"], [r["message_id"] for r in self.rows])
        self.assertNotIn("candidate_decisions", REVIEW_SCHEMA["properties"])

    def test_invalid_candidate_input_refs_rejected_before_transport(self):
        payload = {"new_messages": Analysis.build_preview(self.rows)["messages"], "context_messages": [],
                   "candidate_tasks": [task("outside") | {"candidate_id": "C1"}]}
        with self.assertRaises(ServiceError):
            compact_prompt("输入：" + json.dumps(payload), discovery.schema_with_decisions(SCHEMA))

    def test_boundary_uses_last_acceptance_without_mutating_input(self):
        candidate = task(row(0)["message_id"]) | {"candidate_id": "C1",
            "source_message_ids": [row(2)["message_id"], row(0)["message_id"]]}
        before = json.dumps(candidate, sort_keys=True)
        bounded = discovery.reconciliation_candidates([candidate], [row(3), row(0), row(2), row(1)])
        self.assertEqual(bounded[0]["request_last_message_id"], row(2)["message_id"])
        self.assertIs(bounded[0]["later_evidence_possible"], True)
        self.assertEqual(json.dumps(candidate, sort_keys=True), before)

    def test_boundary_does_not_infer_completion_from_later_message(self):
        rows = [row(0), row(1, content="明天整理后发你")]
        bounded = discovery.reconciliation_candidates(self.candidates, rows)[0]
        self.assertIs(bounded["later_evidence_possible"], True)
        self.assertNotIn("outcome", bounded)
        self.assertNotIn("status", bounded)
        self.assertEqual(bounded["source_message_ids"], self.candidates[0]["source_message_ids"])

    def test_only_original_same_second_or_outside_chat_has_no_later_evidence(self):
        for extra in ([], [row(0) | {"message_id": "same-second"}],
                      [row(1, chat="outside")], [row(1, account="outside")]):
            bounded = discovery.reconciliation_candidates(self.candidates, [row(0), *extra])[0]
            self.assertIs(bounded["later_evidence_possible"], False)

    def test_intervening_delivery_cannot_complete_later_renewed_commitment(self):
        rows = [row(0), row(1, content="第一版已发"), row(2, content="我再补一版")]
        candidates = [self.candidates[0] | {"source_message_ids": [row(0)["message_id"], row(2)["message_id"]]}]
        for outcome in ("fulfilled", "cancelled"):
            with self.assertRaises(ServiceError) as caught:
                discovery.validate_decisions({"candidate_decisions": [decision(outcome=outcome,
                    index=None, refs=[r["message_id"] for r in rows])]}, candidates, [], rows)
            self.assertEqual(caught.exception.code, "discovery_reconciliation")
            later = row(3, content="补充版已发")
            discovery.validate_decisions({"candidate_decisions": [decision(outcome=outcome,
                index=None, refs=[row(2)["message_id"], later["message_id"]])]}, candidates, [], [*rows, later])

    def test_other_chat_or_account_cannot_supply_completion_evidence(self):
        for other in (row(1, chat="outside"), row(1, account="outside")):
            with self.assertRaises(ServiceError):
                discovery.validate_decisions({"candidate_decisions": [decision(outcome="fulfilled",
                    index=None, refs=[row(0)["message_id"], other["message_id"]])]}, self.candidates, [], [row(0), other])

    def test_bad_candidate_boundary_rejected_without_guessing(self):
        for candidates in ([self.candidates[0] | {"source_message_ids": ["missing"]}],
                [self.candidates[0] | {"source_message_ids": [row(0)["message_id"], row(1)["message_id"]]}]):
            with self.assertRaises(ServiceError):
                discovery.reconciliation_candidates(candidates, [row(0), row(1, chat="outside")])

    def test_request_boundary_uses_request_local_alias_not_raw_source_id(self):
        schema = discovery.schema_with_decisions(REVIEW_SCHEMA)
        candidates = discovery.reconciliation_candidates(self.candidates, self.rows)
        payload = {"messages": Analysis.build_preview(self.rows)["messages"], "existing_tasks": [],
            "candidate_tasks": candidates}
        wire, refs = compact_prompt("输入：" + json.dumps(payload), schema)
        value = json.loads(wire.partition("输入：")[2])["candidate_tasks"][0]
        self.assertEqual(value["request_last_message_id"], "M1")
        self.assertIs(value["later_evidence_possible"], True)
        self.assertEqual(refs.restore({"candidate_decisions": [decision(refs=["M1"])]})
            ["candidate_decisions"][0]["source_message_ids"], [row(0)["message_id"]])
        for invalid in ("missing", row(1)["message_id"]):
            payload["candidate_tasks"] = [candidates[0] | {"request_last_message_id": invalid}]
            with self.assertRaises(ServiceError):
                compact_prompt("输入：" + json.dumps(payload), schema)

    def test_current_unknown_flag_cannot_be_string_in_boundary(self):
        payload = {"messages": Analysis.build_preview(self.rows)["messages"], "existing_tasks": [],
            "candidate_tasks": [self.candidates[0] | {"request_last_message_id": row(0)["message_id"],
                "later_evidence_possible": "false"}]}
        with self.assertRaises(ServiceError):
            compact_prompt("输入：" + json.dumps(payload), discovery.schema_with_decisions(REVIEW_SCHEMA))


if __name__ == "__main__":
    unittest.main()
