"""Isolated configuration and safe usage pagination; never use real sources."""
import tempfile
import unittest
from backend.service import LocalService
from backend.contracts import ServiceError, iso_time


class ConfigFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1791010000
        self.service = LocalService(self.temp.name, clock=lambda: self.now)
        self.service.store.configure(dict(account_id="isolated", root="not-read", mode="local"),
            "2026-09-18T00:00:00Z", [dict(chat_id=f"c{i}", chat_name=f"会话{i}", chat_type="group") for i in range(3)])
        self.a = self.service.store.save_tag("工作")
        self.b = self.service.store.save_tag("重点")

    def preview(self, operation, ids=None, tags=None):
        return self.service.dispatch("chat.bulk.preview", dict(chat_ids=ids or ["c0","c1","c2"], operation=operation, tag_ids=tags or []))

    def apply(self, preview):
        return self.service.dispatch("chat.bulk.apply", dict(preview_token=preview["preview_token"], confirm=True))

    def switches(self):
        return [(c["sync_enabled"],c["analysis_enabled"]) for c in self.service.store.chats()]


class ChatBulkTests(ConfigFixture):
    def test_preview_is_readonly_and_cancel_needs_no_action(self):
        before = self.service.store.settings()
        preview = self.preview("sync_on")
        self.assertEqual(preview["change"],3)
        self.assertEqual(self.switches(), [(False,False)]*3)
        self.assertEqual(self.service.store.settings(), before)
        self.assertEqual(self.service.models.usage()["today"]["requests"],0)

    def test_sync_analysis_and_scope_are_separate(self):
        preview = self.preview("analysis_on")
        self.assertEqual((preview["change"],preview["unavailable"]), (0,3))
        self.assertEqual(self.apply(preview)["unavailable"],3)
        self.apply(self.preview("sync_on",["c0"]))
        preview = self.preview("analysis_on")
        self.assertEqual((preview["change"],preview["unavailable"]), (1,2))
        self.apply(preview)
        self.assertEqual(self.switches(), [(True,True),(False,False),(False,False)])
        self.assertIsNone(self.service.automatic.public()["grant"])

    def test_single_save_also_rejects_analysis_without_sync(self):
        with self.assertRaises(ServiceError):
            self.service.store.update_chat("c0",sync_enabled=False,analysis_enabled=True)

    def test_sync_off_disables_analysis_but_keeps_other_data(self):
        self.apply(self.preview("sync_on")); self.apply(self.preview("analysis_on"))
        self.apply(self.preview("tags_add",tags=[self.a]))
        self.apply(self.preview("sync_off"))
        self.assertEqual(self.switches(), [(False,False)]*3)
        self.assertEqual(len(self.service.store.chats()[0]["tags"]),1)

    def test_append_remove_keep_unselected_tags_and_names(self):
        self.service.store.update_chat("c0",sync_enabled=True,analysis_enabled=False,display_name="个人名称",tag_ids=[self.a])
        self.apply(self.preview("tags_add",["c0"], [self.b]))
        chat = next(c for c in self.service.store.chats() if c["chat_id"]=="c0")
        self.assertEqual(chat["display_name"],"个人名称")
        self.assertEqual({t["tag_id"] for t in chat["tags"]},{self.a,self.b})
        self.apply(self.preview("tags_remove",["c0"], [self.a]))
        self.assertEqual(next(c for c in self.service.store.chats() if c["chat_id"]=="c0")["tags"][0]["tag_id"], self.b)

    def test_nochange_missing_and_unavailable_counts(self):
        self.apply(self.preview("sync_on",["c0"]))
        preview=self.preview("sync_on",["c0","c1","missing"])
        self.assertEqual((preview["change"],preview["unchanged"],preview["unavailable"]),(1,1,1))
        result=self.apply(preview)
        self.assertEqual((result["changed"],result["unchanged"],result["unavailable"]),(1,1,1))
        self.assertEqual(result["unavailable_ids"],["missing"])

    def test_confirmation_expiry_and_reuse_are_rejected(self):
        preview=self.preview("sync_on")
        with self.assertRaises(ServiceError): self.service.dispatch("chat.bulk.apply",dict(preview_token=preview["preview_token"]))
        self.apply(preview)
        with self.assertRaises(ServiceError): self.apply(preview)
        preview=self.preview("sync_off"); self.now+=301
        with self.assertRaises(ServiceError): self.apply(preview)
        self.assertEqual(self.switches(),[(True,False)]*3)

    def test_changed_config_rejects_entire_write(self):
        mutations = [lambda: self.service.store.save_tag("新增"),
            lambda: self.service.store.save_tag("改名",self.a),
            lambda: self.service.store.delete_tag(self.b),
            lambda: self.service.store.set_range("2026-09-19T00:00:00Z"),
            lambda: self.service.store.set_paused(True)]
        for mutate in mutations:
            preview=self.preview("sync_on"); mutate()
            with self.assertRaises(ServiceError) as error: self.apply(preview)
            self.assertEqual(error.exception.code,"bulk_preview_changed")
        self.assertEqual(self.switches(),[(False,False)]*3)

    def test_changed_back_is_still_stale_via_revision(self):
        preview=self.preview("sync_on")
        self.service.store.update_chat("c0",sync_enabled=True,analysis_enabled=False)
        self.service.store.update_chat("c0",sync_enabled=False,analysis_enabled=False)
        with self.assertRaises(ServiceError): self.apply(preview)

    def test_account_switch_and_discovery_are_stale(self):
        preview=self.preview("sync_on")
        self.service.store.refresh_chats([dict(chat_id="new", chat_name="新会话",chat_type="group")])
        with self.assertRaises(ServiceError): self.apply(preview)
        preview=self.preview("sync_on")
        self.service.store.configure(dict(account_id="other",root="other",mode="local"),"2026-09-18T00:00:00Z",[])
        with self.assertRaises(ServiceError): self.apply(preview)
        self.assertEqual(self.service.store.chats(),[])

    def test_partial_database_failure_rolls_back_only_failed_item(self):
        with self.service.store.database() as db:
            db.execute("CREATE TRIGGER deny_one BEFORE UPDATE ON chats WHEN NEW.chat_id='c1' BEGIN SELECT RAISE(ABORT,'controlled'); END")
        result=self.apply(self.preview("sync_on"))
        self.assertEqual((result["changed"],result["failed"]),(2,1))
        self.assertEqual(result["failed_ids"],["c1"])
        self.assertEqual(self.switches(),[(True,False),(False,False),(True,False)])

    def test_final_transaction_failure_rolls_back_all_savepoints(self):
        with self.service.store.database() as db:
            db.execute("CREATE TRIGGER deny_revision BEFORE INSERT ON settings WHEN NEW.key='revision' BEGIN SELECT RAISE(ABORT,'controlled'); END")
        with self.assertRaises(ServiceError) as error: self.apply(self.preview("sync_on"))
        self.assertEqual(error.exception.code,"storage_failed")
        self.assertEqual(self.switches(),[(False,False)]*3)

    def test_invalid_payloads_make_no_changes(self):
        payloads=[dict(chat_ids=[],operation="sync_on"),dict(chat_ids=["c0","c0"],operation="sync_on"),
            dict(chat_ids=["c0"],operation="delete"),dict(chat_ids=["c0"],operation="tags_add",tag_ids=[]),
            dict(chat_ids=["c0"],operation="sync_on",tag_ids=[self.a]),dict(chat_ids=["c0"],operation="tags_add",tag_ids=["missing"])]
        for payload in payloads:
            with self.assertRaises(ServiceError): self.service.dispatch("chat.bulk.preview",payload)
        self.assertEqual(self.switches(),[(False,False)]*3)

    def test_two_thousand_chats_and_three_hundred_tags(self):
        self.service.store.refresh_chats([dict(chat_id=f"scale{i}",chat_name=f"大量会话{i}",chat_type="group") for i in range(2100)])
        for i in range(300): self.service.store.save_tag(f"大量标签{i}")
        ids=[c["chat_id"] for c in self.service.store.chats()]
        result=self.apply(self.preview("tags_add",ids,[self.a,self.b]))
        self.assertEqual(result["changed"],2103)
        self.assertEqual(len(self.service.store.tags()),302)

    def test_scope_changes_invalidate_grant_but_labels_do_not(self):
        self.apply(self.preview("sync_on")); self.apply(self.preview("analysis_on"))
        with self.service.store.database() as db:
            db.execute("INSERT INTO text_grants VALUES('g','{}','hash','active','2026-10-03',NULL,NULL)")
        self.apply(self.preview("tags_add",tags=[self.a]))
        with self.service.store.database() as db: self.assertEqual(db.execute("SELECT status FROM text_grants").fetchone()[0],"active")
        self.apply(self.preview("analysis_off"))
        with self.service.store.database() as db: self.assertEqual(db.execute("SELECT status FROM text_grants").fetchone()[0],"invalid")

    def test_model_change_invalidates_preview_without_secret_disclosure(self):
        self.service.models.save(base_url="https://generated.invalid", model="generated", api_key="isolated-secret", protocol="gemini")
        preview=self.preview("sync_on")
        self.assertNotIn("isolated-secret",str(preview))
        self.service.models.enable(enabled=False)
        with self.assertRaises(ServiceError): self.apply(preview)


class UsagePageTests(ConfigFixture):
    # Inherit fixture only, not bulk tests.
    def setUp(self):
        super().setUp()
        with self.service.store.database() as db:
            for i in range(55):
                db.execute("INSERT INTO model_calls(call_id,capability,config_revision,purpose,started_at,outcome,input_tokens,output_tokens,total_tokens) VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"call{i:03}","text",1,"automatic_text",iso_time(self.now-100+i), ["success","failed","unknown","sending"][i%4], None if i%3==0 else 10, 2, 20))

    def test_paginated_usage_is_readonly_bounded_and_stable(self):
        first=self.service.dispatch("model.calls",dict(period="today",limit=25,offset=0))
        second=self.service.dispatch("model.calls",dict(period="today",limit=25,offset=25))
        self.assertEqual(first["total"],55); self.assertEqual(len(first["items"]),25)
        self.assertEqual(len(second["items"]),25)
        self.assertFalse({r["call_id"] for r in first["items"]} & {r["call_id"] for r in second["items"]})
        self.assertFalse(self.service.dispatch("model.calls",dict(offset=50))["has_more"])
        self.assertEqual(self.service.dispatch("model.calls",dict(offset=100))["items"],[])
        self.assertEqual(set(first["items"][0]), {"call_id","purpose","started_at","finished_at","outcome","input_tokens","output_tokens","total_tokens"})

    def test_usage_status_and_tokens_never_guessed(self):
        usage=self.service.models.usage()["today"]
        self.assertEqual(usage["requests"],55)
        self.assertEqual(sum(usage[k] for k in ("success","failed","unknown","sending")),55)
        self.assertEqual(usage["known_total_tokens"],1100)
        self.assertGreater(usage["unknown_input_requests"],0)

    def test_invalid_usage_parameters(self):
        for params in (dict(period="all"),dict(limit=101),dict(limit=True),dict(offset=-1),dict(offset=0.5)):
            with self.assertRaises(ServiceError): self.service.dispatch("model.calls",params)
