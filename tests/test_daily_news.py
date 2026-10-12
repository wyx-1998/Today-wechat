"""Daily news uses generated chat text, isolated storage and no external model calls."""
from contextlib import closing
from datetime import datetime
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from backend.contracts import ServiceError, iso_time
from backend.daily_news import BEIJING, civil_day, day_bounds, chunks
from backend.gemini import GeminiClient
from backend.service import LocalService
from backend.storage import Store


def stamp(value):
    return datetime.fromisoformat(value+"+08:00").timestamp()


class NewsClient:
    def __init__(self):
        self.calls=[]; self.callback=None; self.error=None; self.invalid=False; self.omit=False
    def generate(self, config, key, prompt, **kwargs):
        payload=json.loads(prompt.split("输入：",1)[1]); self.calls.append(payload)
        if self.callback:
            callback=self.callback; self.callback=None; callback()
        if self.error: raise self.error
        if "messages" in payload:
            items=[]
            for message in payload["messages"]:
                if "发布模型" not in message["content"] and "项目公告" not in message["content"]: continue
                items.append({"title":"模型发布" if "发布模型" in message["content"] else "项目通知",
                    "summary":message["content"],"kind":"ai" if "发布模型" in message["content"] else "work",
                    "evidence":[{"ref":message["ref"],"quote":message["content"]}]})
            if self.invalid: items.append({"title":"不存在","summary":"虚构","kind":"ai","evidence":[{"ref":"M99999","quote":"虚构"}]})
            result={"news_items":items}
        else:
            grouped={}
            for item in payload["candidates"]:
                if self.omit: continue
                old=grouped.setdefault(item["title"], {"title":item["title"],"summary":item["summary"],"kind":item["kind"],"candidate_ids":[]})
                old["candidate_ids"].append(item["candidate_id"])
            result={"topics":list(grouped.values())}
        return result,{"input_tokens":100,"output_tokens":80,"total_tokens":200}


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.root=tempfile.TemporaryDirectory(prefix="news-owned-");self.addCleanup(self.root.cleanup)
        self.now=stamp("2026-10-10T09:59:00");self.client=NewsClient()
        self.s=LocalService(self.root.name,model_client=self.client,clock=lambda:self.now);self.addCleanup(self.s.close)
        self.source={"mode":"local","account_id":"owned-news","root":self.root.name,"self_id":None}
        self.s.store.configure(self.source,iso_time(stamp("2026-10-08T00:00:00")),[
            {"chat_id":cid,"chat_name":"会话"+cid,"chat_type":"group"} for cid in ("a","b","outside")])
        for cid in ("a","b"): self.s.store.update_chat(cid,sync_enabled=True,analysis_enabled=True)
        self.s.models.save(base_url="https://generated.invalid",model="generated-flash",api_key="generated-local-key")
        p=self.s.automatic.preview();self.s.automatic.save(authorize=True,preview_token=p["preview_token"])
        self.s.store.set_paused(False);self.sync()
        self.network=patch("socket.socket",side_effect=AssertionError("external network forbidden"));self.network.start();self.addCleanup(self.network.stop)
    def sync(self, outcome="success"):
        with self.s.store.database() as db:
            self.s.store._set(db,"last_sync",{"account_id":"owned-news","outcome":outcome,"finished_at":iso_time(self.now),"errors":[]})
    def add(self,mid,content="AI 团队发布模型，支持文本。联系电话13800138000",day="2026-10-09T12:00:00",chat="a",kind="text"):
        when=stamp(day)
        with self.s.store.database() as db:
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(mid,"owned-news",chat,"local",mid,None,None,None,
                iso_time(when),when,kind,content,"text_available",int(kind=="text")))
    def enable(self):
        p=self.s.daily_news.preview();return self.s.daily_news.enable(preview_token=p["preview_token"],authorize=True)
    def generate(self,day="2026-10-09",p=None):
        p=p or self.s.daily_news.preview_run(day=day)
        return self.s.daily_news.run(day=day,preview_token=p["preview_token"],authorize=True)
    def tables(self):
        with self.s.store.database() as db:
            return {name:list(map(tuple,db.execute('SELECT * FROM "'+name+'" ORDER BY 1'))) for name in
                ("messages","tasks","attention_items","analysis_runs","text_grants","text_pending","cursors")}

    def test_upgrade_starts_disabled_and_does_not_call_model(self):
        self.add("one")
        self.assertFalse(self.s.daily_news.public()["enabled"])
        self.assertEqual(self.s.daily_news.tick(),{"status":"inactive"})
        self.assertEqual(self.client.calls,[])

    def test_beijing_boundary_independent_of_machine_timezone(self):
        self.assertEqual(civil_day(stamp("2026-10-10T00:00:00")).isoformat(),"2026-10-10")
        start,end=day_bounds("2026-10-09");self.assertEqual(end-start,86400)
        self.assertEqual(datetime.fromtimestamp(start,BEIJING).hour,0)

    def test_enable_requires_single_use_scope_confirmation(self):
        p=self.s.daily_news.preview()
        with self.assertRaises(ServiceError):self.s.daily_news.enable(preview_token=p["preview_token"])
        self.s.daily_news.enable(preview_token=p["preview_token"],authorize=True)
        with self.assertRaises(ServiceError):self.s.daily_news.enable(preview_token=p["preview_token"],authorize=True)
        self.assertEqual(self.client.calls,[])

    def test_expired_enable_preview_rejected(self):
        p=self.s.daily_news.preview();self.now+=901
        with self.assertRaises(ServiceError):self.s.daily_news.enable(preview_token=p["preview_token"],authorize=True)

    def test_first_day_does_not_backfill_ancient_history(self):
        self.add("old",day="2026-10-08T12:00:00");self.enable()
        self.assertEqual(self.s.daily_news.public()["first_day"],"2026-10-09")
        with self.assertRaises(ServiceError):self.generate("2026-10-08")

    def test_0959_waits_and_1000_generates_once(self):
        self.add("one");self.enable()
        self.assertEqual(self.s.daily_news.tick(),{"status":"waiting"})
        self.now=stamp("2026-10-10T10:00:00");self.sync()
        report=self.s.daily_news.tick();self.assertEqual(report["day"],"2026-10-09")
        self.assertEqual(len(report["edition"]["body"]["topics"]),1)
        calls=len(self.client.calls);self.assertEqual(self.s.daily_news.tick(),{"status":"waiting"});self.assertEqual(len(self.client.calls),calls)

    def test_sync_must_cover_end_of_day_before_auto_generation(self):
        self.add("one");self.enable();self.now=stamp("2026-10-10T10:00:00")
        self.sync("partial");self.assertEqual(self.s.daily_news.tick(),{"status":"waiting_sync"})
        self.sync();self.assertIsNotNone(self.s.daily_news.tick()["edition"])

    def test_inclusive_midnight_exclusive_next_midnight_and_scope(self):
        self.add("first",day="2026-10-09T00:00:00");self.add("end",day="2026-10-10T00:00:00")
        self.add("other",chat="outside");self.add("audio",kind="audio");self.enable()
        report=self.generate();self.assertEqual(report["edition"]["body"]["message_count"],1)
        self.assertEqual(report["edition"]["body"]["topics"][0]["source_message_ids"],["first"])

    def test_independent_report_preserves_tasks_sources_and_authorization(self):
        self.add("one");self.enable();before=self.tables();settings=self.s.store.settings()
        self.generate();self.assertEqual(self.tables(),before);self.assertEqual(self.s.store.settings(),settings)
        self.assertIsNone(self.s.analysis.tasks(today=True)["last_analysis"])
        self.assertEqual(self.s.models.usage()["today"]["requests"],2)

    def test_cross_chat_merge_has_all_originals(self):
        self.add("first");self.add("second",chat="b");self.enable()
        topics=self.generate()["edition"]["body"]["topics"]
        self.assertEqual(len(topics),1);self.assertEqual(set(topics[0]["source_message_ids"]),{"first","second"})

    def test_sensitive_content_redacted_before_chunking(self):
        self.add("one",content="发布模型"+"字"*1990+"13800138000");self.enable();self.generate()
        self.assertNotIn("13800138000",json.dumps(self.client.calls,ensure_ascii=False))
        self.assertIn("13800138000",self.s.store.message_detail("one")["content"])

    def test_invalid_evidence_does_not_block_independent_news(self):
        self.add("one");self.enable();self.client.invalid=True
        body=self.generate()["edition"]["body"]
        self.assertEqual(len(body["topics"]),1);self.assertEqual(body["rejected_count"],1)

    def test_composition_omissions_keep_valid_input(self):
        self.add("one");self.enable();self.client.omit=True
        self.assertEqual(len(self.generate()["edition"]["body"]["topics"]),1)

    def test_late_messages_not_in_frozen_request_and_stay_on_original_day(self):
        self.add("one");self.enable();p=self.s.daily_news.preview_run(day="2026-10-09")
        self.add("late",content="项目公告：调整会议安排。")
        report=self.generate(p=p)
        self.assertEqual(report["edition"]["body"]["message_count"],1);self.assertEqual(report["late_messages"],1)
        second=self.generate();self.assertEqual(second["edition"]["revision"],2);self.assertEqual(second["late_messages"],0)

    def test_changed_original_rejects_preview_without_sending(self):
        self.add("one");self.enable();p=self.s.daily_news.preview_run(day="2026-10-09")
        with self.s.store.database() as db:db.execute("UPDATE messages SET content='已变化' WHERE message_id='one'")
        with self.assertRaises(ServiceError):self.generate(p=p)
        self.assertEqual(self.client.calls,[])

    def test_expired_run_preview_rejected(self):
        self.add("one");self.enable();p=self.s.daily_news.preview_run(day="2026-10-09");self.now+=901
        with self.assertRaises(ServiceError):self.generate(p=p)

    def test_scope_change_during_request_does_not_publish(self):
        self.add("one");self.enable()
        self.client.callback=lambda:self.s.daily_news.disable(revision=self.s.daily_news.public()["revision"])
        with self.assertRaises(ServiceError):self.generate()
        self.assertIsNone(self.s.daily_news.detail()["edition"])

    def test_model_change_during_request_does_not_publish(self):
        self.add("one");self.enable();self.client.callback=lambda:self.s.models.save(base_url="https://different.invalid",model="generated-flash")
        with self.assertRaises(ServiceError):self.generate()
        self.assertFalse(self.s.daily_news.public()["valid"])

    def test_failure_preserves_previous_version_and_does_not_auto_retry(self):
        self.add("one");self.enable();self.generate();self.client.error=ServiceError("model_timeout_unknown","unknown")
        with self.assertRaises(ServiceError):self.generate()
        report=self.s.daily_news.detail();self.assertEqual(report["edition"]["revision"],1);self.assertEqual(report["run"]["status"],"unknown")
        self.now=stamp("2026-10-10T10:00:00");self.sync();count=len(self.client.calls)
        self.s.daily_news.tick();self.assertEqual(len(self.client.calls),count)

    def test_empty_news_is_not_fabricated(self):
        self.add("chat",content="早上好");self.enable()
        self.assertEqual(self.generate()["edition"]["body"]["topics"],[])

    def test_no_messages_publish_empty_without_model_call(self):
        self.enable();report=self.generate();self.assertEqual(report["edition"]["body"]["topics"],[]);self.assertEqual(self.client.calls,[])

    def test_pause_and_analysis_lock_keep_requests_waiting(self):
        self.add("one");self.enable();self.s.pause()
        with self.assertRaises(ServiceError):self.generate()
        self.s.resume();self.s.analysis.lock.acquire()
        try:
            with self.assertRaises(ServiceError):self.generate()
        finally:self.s.analysis.lock.release()
        self.assertEqual(self.client.calls,[])

    def test_restart_keeps_edition_and_interrupted_run_never_retries(self):
        self.add("one");self.enable();self.generate()
        with self.s.store.database() as db:db.execute("UPDATE news_runs SET status='running'")
        self.s.daily_news.recover();self.assertEqual(self.s.daily_news.detail()["run"]["status"],"unknown")
        self.assertEqual(self.s.daily_news.detail()["edition"]["revision"],1)

    def test_missed_dates_fill_in_order_but_before_ten_excludes_yesterday(self):
        self.enable();self.now=stamp("2026-10-12T09:00:00");self.sync()
        self.assertEqual(self.s.daily_news.tick()["day"],"2026-10-09")
        self.assertEqual(self.s.daily_news.tick()["day"],"2026-10-10")
        self.assertEqual(self.s.daily_news.tick(),{"status":"waiting"})
        self.now=stamp("2026-10-12T10:00:00");self.sync();self.assertEqual(self.s.daily_news.tick()["day"],"2026-10-11")

    def test_read_operations_do_not_call_model_and_history_is_paged(self):
        self.enable();self.generate();calls=len(self.client.calls)
        self.assertEqual(self.s.daily_news.list(limit=1)["days"],["2026-10-09"])
        self.s.daily_news.detail(day="2026-10-08");self.assertEqual(len(self.client.calls),calls)

    def test_other_account_cannot_read_or_generate_previous_editions(self):
        self.add("one");self.enable();self.generate()
        with self.s.store.database() as db:self.s.store._set(db,"source",self.source|{"account_id":"other-account"})
        self.assertIsNone(self.s.daily_news.detail()["edition"]);self.assertEqual(self.s.daily_news.list()["total"],0)
        with self.assertRaises(ServiceError):self.generate()

    def test_migration_v10_keeps_every_old_row(self):
        with tempfile.TemporaryDirectory() as root:
            old=Store(root)
            with old.database() as db:
                for table in ("ask_citations","ask_runs","ask_turns","ask_threads","ask_grants","ask_fts","ask_index"): db.execute("DROP TABLE "+table)
                for table in ("news_settings","news_runs","news_editions"):db.execute("DROP TABLE "+table)
                db.execute("PRAGMA user_version=10")
                db.execute("INSERT INTO tags VALUES('kept','工作')")
                when=stamp("2026-10-09T12:00:00")
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",("kept-message","kept-account","kept-chat","local","1",None,None,None,
                    iso_time(when),when,"text","保留完整原文","text_available",1))
                names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                before={name:list(map(tuple,db.execute('SELECT * FROM "'+name+'" ORDER BY 1'))) for name in names}
            for _ in range(2):
                new=Store(root)
                with new.database() as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],12)
                    self.assertEqual({name:list(map(tuple,db.execute('SELECT * FROM "'+name+'" ORDER BY 1'))) for name in names},before)

    def test_chunking_preserves_every_item(self):
        items=[{"value":"字"*20,"id":i} for i in range(50)]
        self.assertEqual([item for group in chunks(items,limit=100) for item in group],items)

    def test_actual_gemini_adapter_uses_native_schema_and_redacted_payload(self):
        client=NewsClient()
        class Opener:
            def open(inner,request,timeout):
                self.assertTrue(request.full_url.endswith("/v1beta/models/generated-flash:generateContent"))
                wire=json.loads(request.data)
                prompt=wire["contents"][0]["parts"][0]["text"]
                self.assertNotIn("13800138000",prompt);self.assertNotIn("generated-local-key",prompt)
                payload=json.loads(prompt.split("输入：",1)[1])
                self.assertEqual(set(wire["generationConfig"]["responseSchema"]["properties"]),
                    {"news_items"} if "messages" in payload else {"topics"})
                result,_=client.generate({},"not-used",prompt)
                return io.BytesIO(json.dumps({"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":json.dumps(result)}]}}],
                    "usageMetadata":{"promptTokenCount":100,"candidatesTokenCount":80,"totalTokenCount":200}}).encode())
        self.add("one");self.enable();self.s.models.client=GeminiClient(opener=Opener())
        self.assertEqual(len(self.generate()["edition"]["body"]["topics"]),1)

    def test_large_day_processes_every_message_across_chunks(self):
        for i in range(60):self.add(str(i),content="发布模型"+"字"*1000)
        self.enable();body=self.generate()["edition"]["body"]
        self.assertEqual(body["message_count"],60)
        ids={mid for topic in body["topics"] for mid in topic["source_message_ids"]}
        self.assertEqual(ids,{str(i) for i in range(60)})
        self.assertGreater(len(self.client.calls),2)

    def test_real_service_timer_invokes_daily_scheduler(self):
        self.add("one");self.enable();self.now=stamp("2026-10-10T10:00:00");self.sync()
        self.s.analysis_interval=.02;self.s.start()
        end=time.monotonic()+2
        while not self.s.daily_news.detail()["edition"] and time.monotonic()<end:time.sleep(.02)
        self.assertIsNotNone(self.s.daily_news.detail()["edition"])
        self.assertIsNotNone(self.s.news_timer)

    def test_schema_conflict_keeps_v10_without_silent_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            old=Store(root)
            with old.database() as db:db.execute("PRAGMA user_version=10")
            with self.assertRaises(ServiceError):Store(root)
            with closing(sqlite3.connect(old.path)) as db:self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],10)

    def test_two_service_instances_cannot_send_same_automatic_day_twice(self):
        self.add("one");self.enable();self.now=stamp("2026-10-10T10:00:00");self.sync()
        other_client=NewsClient();other=LocalService(self.root.name,model_client=other_client,clock=lambda:self.now)
        self.addCleanup(other.close)
        entered,release=threading.Event(),threading.Event()
        self.client.callback=lambda:(entered.set(),release.wait(2))
        errors=[]
        def tick():
            try:self.s.daily_news.tick()
            except Exception as error:errors.append(error)
        first=threading.Thread(target=tick);first.start()
        try:
            self.assertTrue(entered.wait(2));self.assertEqual(other.daily_news.tick(),{"status":"waiting"})
        finally:release.set();first.join(2)
        self.assertFalse(first.is_alive());self.assertEqual(errors,[])
        self.assertEqual(other_client.calls,[])
        self.assertEqual(self.s.daily_news.list()["total"],1)

    def test_remote_command_cannot_bypass_consent_with_automatic_flag(self):
        self.enable()
        with self.assertRaises(ServiceError):self.s.dispatch("daily.run",{"day":"2026-10-09","automatic":True})
        self.assertEqual(self.client.calls,[])

    def test_daily_grant_is_independent_of_task_automation_grant(self):
        self.add("one");self.s.automatic.revoke()
        self.enable();self.assertTrue(self.s.daily_news.public()["valid"])
        self.assertEqual(len(self.generate()["edition"]["body"]["topics"]),1)
        self.assertEqual(self.s.automatic.public()["grant"]["status"],"revoked")
