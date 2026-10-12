"""Read-only browsing over owned, isolated SQLite data."""
import tempfile
import unittest
from backend.contracts import ServiceError, parse_time
from backend.service import LocalService
from tests.product_data_check import snapshot


class MessageBrowserTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="today-message-browser-")
        self.addCleanup(folder.cleanup)
        self.service = LocalService(folder.name)
        self.addCleanup(self.service.close)
        self.store = self.service.store
        self.store.configure(dict(account_id="a",root="not-read",mode="local",self_id="self"),
            "2026-09-01T00:00:00Z", [dict(chat_id=id,chat_name=name,chat_type="group") for id,name in
             [("c1","资料协作"),("c2","同名协作"),("c3","空会话"),("literal","100%_事项")]])
        self.store.set_paused(True)
        self.add("before", "2026-10-01T23:59:59+08:00", "self")
        self.add("first", "2026-10-02T00:00:00+08:00", "self")
        self.add("last", "2026-10-02T23:59:59+08:00", "other")
        self.add("end", "2026-10-03T00:00:00+08:00", "other")
        self.add("unknown", "2026-10-02T01:00:00+08:00", None)
        self.add("elsewhere", "2026-10-02T00:00:00+08:00", "self", chat="c2")
        self.add("literal", "2026-10-02T00:00:00+08:00", "self", chat="literal")

    def add(self, id, date, sender, *, account="a", chat="c1", name="同名", body="完整文字"):
        with self.store.database() as db:
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                id,account,chat,"stream",id,sender,name,None,date,parse_time(date),"text",body,"text_available",1))

    def query(self, **params):
        return self.service.dispatch("messages.list", dict(chat_id="c1",**params))

    def test_inclusive_start_exclusive_end_and_unfiltered_facets(self):
        result=self.query(time_start="2026-10-02T00:00:00+08:00",time_end="2026-10-03T00:00:00+08:00",order="asc")
        self.assertEqual([m["message_id"] for m in result["items"]],["first","unknown","last"])
        self.assertEqual(result["total"],3)
        self.assertEqual(result["saved_total"],5)
        self.assertEqual(len(result["senders"]),3)

    def test_stable_sender_id_not_same_nickname(self):
        result=self.query(sender_id="self")
        self.assertEqual({m["message_id"] for m in result["items"]},{"before","first"})
        self.assertTrue(all(m["effective_is_self"] for m in result["items"]))
        self.assertEqual({s["sender_id"] for s in result["senders"]},{"self","other",None})

    def test_unknown_sender_is_a_separate_explicit_filter(self):
        self.assertEqual([m["message_id"] for m in self.query(sender_unknown=True)["items"]],["unknown"])
        with self.assertRaises(ServiceError): self.query(sender_unknown=True,sender_id="self")

    def test_both_filters_apply_to_count_and_every_page(self):
        result=self.query(time_start="2026-10-02T00:00:00+08:00",time_end="2026-10-03T00:00:00+08:00",sender_id="other",limit=1)
        self.assertEqual(result["total"],1)
        self.assertEqual(result["items"][0]["message_id"],"last")
        self.assertFalse(result["has_more"])
        empty=self.query(sender_id="self",offset=2)
        self.assertEqual(empty["total"],2)
        self.assertEqual(empty["items"],[])

    def test_timezones_compared_by_epoch_not_timestamp_strings(self):
        self.add("same-instant","2026-10-01T16:00:00Z","self")
        result=self.query(time_start="2026-10-02T00:00:00+08:00",time_end="2026-10-02T00:00:01+08:00",order="asc")
        self.assertEqual([m["message_id"] for m in result["items"]],["first","same-instant"])

    def test_asc_pagination_has_no_duplicates_and_default_desc_is_preserved(self):
        seen=[]
        for offset in range(0,6,2): seen.extend(m["message_id"] for m in self.query(order="asc",limit=2,offset=offset)["items"])
        self.assertEqual(seen,["before","first","unknown","last","end"])
        self.assertEqual(self.query()["items"][0]["message_id"],"end")

    def test_invalid_filters_fail_without_guessing_or_unbounded_fallback(self):
        for params in [dict(time_start="2026-10-02"),dict(time_end=False),dict(sender_unknown=1),dict(order="bad"),dict(sender_id=""),dict(time_start="2026-10-03T00:00:00Z",time_end="2026-10-02T00:00:00Z")]:
            with self.subTest(params=params), self.assertRaises(ServiceError): self.query(**params)

    def test_saved_chat_directory_excludes_empty_chats_and_has_bounded_pages(self):
        result=self.service.dispatch("messages.chats",dict(limit=1,offset=1))
        self.assertEqual(result["total"],3)
        self.assertEqual(len(result["items"]),1)
        self.assertTrue(result["has_more"])
        self.assertNotIn("c3",{r["chat_id"] for r in self.store.message_chats()["items"]})

    def test_search_covers_saved_directory_and_treats_like_symbols_literally(self):
        self.assertEqual(self.store.message_chats(query="100%_")["total"],1)
        self.assertEqual(self.store.message_chats(query="_")["total"],1)
        self.store.update_chat("c1",display_name="新名称",sync_enabled=False,analysis_enabled=False)
        self.assertEqual(self.store.message_chats(query="新名称")["items"][0]["chat_id"],"c1")
        self.assertEqual(self.store.message_chats(query="资料")["total"],1)
        self.assertEqual(self.store.message_chats(query="不存在")["total"],0)

    def test_list_facets_and_directory_never_leak_another_account(self):
        self.add("alien","2026-10-02T01:00:00Z","alien",account="b")
        self.assertNotIn("alien",{m["message_id"] for m in self.query()["items"]})
        self.assertNotIn("alien",{s["sender_id"] for s in self.query()["senders"]})
        self.assertEqual(self.store.message_chats()["items"][0]["message_count"],5)
        with self.store.database() as db: self.store._set(db,"source",dict(account_id="b",root="not-read",mode="local"))
        self.assertEqual([m["message_id"] for m in self.query()["items"]],["alien"])
        self.assertEqual(self.store.message_chats()["total"],1)

    def test_reading_does_not_write_rows_or_change_historical_self_fields(self):
        with self.store.database() as db: before=snapshot(db)
        self.query(sender_id="self")
        self.store.message_chats(query="资料")
        self.store.message_detail("first")
        with self.store.database() as db: self.assertEqual(before,snapshot(db))
        self.assertIsNone(self.query(sender_id="self")["items"][0]["is_self"])

    def test_complete_text_and_original_context_are_not_filtered_or_truncated(self):
        text="完整原文"*700
        self.add("long","2026-10-02T03:00:00+08:00","self",body=text)
        result=self.query(sender_id="self")
        self.assertEqual(next(m for m in result["items"] if m["message_id"]=="long")["content"],text)
        detail=self.store.message_detail("long")
        self.assertEqual(detail["content"],text)
        self.assertTrue(any(m["sender_id"]=="other" for m in detail["context"]))
