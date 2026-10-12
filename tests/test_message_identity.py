"""Identity interpretation does not modify saved messages or infer from nicknames."""

import copy
import unittest

from backend.analysis import Analysis
from backend.automatic import AutomaticText
from backend.message_identity import identified
from backend.reanalysis import Reanalysis


def row(sender="self-id", account="account", flag=None):
    return {"message_id": "message", "account_id": account, "chat_id": "chat", "sender_id": sender,
            "sender_name": "same nickname", "is_self": flag, "content": "稍后发送资料", "sent_at": "2026-10-02T12:00:00+08:00",
            "sent_epoch": 1790913600, "message_type": "text", "content_state": "text_available", "analysis_eligible": 1}


SOURCE = {"account_id": "account", "self_id": "self-id"}


class MessageIdentityTests(unittest.TestCase):
    def test_explicit_stable_sender_becomes_self_without_mutation(self):
        rows = [row()]
        before = copy.deepcopy(rows)
        result = identified(rows, SOURCE)
        self.assertEqual(result[0]["effective_is_self"], 1)
        self.assertEqual(rows, before)
        self.assertIsNone(result[0]["is_self"])

    def test_matching_nickname_is_not_self_if_sender_differs(self):
        result = identified([row("other-id")], SOURCE)
        self.assertEqual(result[0]["effective_is_self"], 0)

    def test_missing_sender_stays_unknown_even_with_old_flag(self):
        self.assertIsNone(identified([row(None, flag=1)], SOURCE)[0]["effective_is_self"])

    def test_other_account_is_not_reinterpreted(self):
        self.assertIsNone(identified([row(account="other-account")], SOURCE)[0]["effective_is_self"])

    def test_unconfigured_identity_preserves_reader_relation(self):
        self.assertEqual(identified([row(flag=0)], {"account_id": "account"})[0]["effective_is_self"], 0)
        self.assertIsNone(identified([row()], None)[0]["effective_is_self"])

    def test_configured_identity_supersedes_stale_reader_classification(self):
        self.assertEqual(identified([row(flag=0)], SOURCE)[0]["effective_is_self"], 1)

    def test_preview_uses_effective_identity_but_no_sender_secret(self):
        preview = Analysis.build_preview(identified([row()], SOURCE))
        self.assertEqual(preview["messages"][0]["speaker"], "本人")
        self.assertNotIn("self-id", str(preview))

    def test_validation_uses_same_identity_as_preview(self):
        task = {"title": "发资料", "owner_type": "self", "due_date": None, "source_message_ids": ["message"]}
        self.assertEqual(Analysis.validate({"tasks": [task]}, identified([row()], SOURCE))[0]["owner_type"], "self")
        self.assertEqual(Analysis.validate({"tasks": [task]}, identified([row("other-id")], SOURCE))[0]["owner_type"], "unknown")

    def test_effective_identity_does_not_change_frozen_input_hashes(self):
        rows = [row()]
        for hasher in (AutomaticText.input_hash, Reanalysis.input_hash):
            self.assertEqual(hasher(rows), hasher(identified(rows, SOURCE)))

    def test_unknown_identity_cannot_support_other_owner(self):
        task = {"title": "发资料", "owner_type": "other", "due_date": None, "source_message_ids": ["message"]}
        self.assertEqual(Analysis.validate({"tasks": [task]}, identified([row(None)], SOURCE))[0]["owner_type"], "unknown")
