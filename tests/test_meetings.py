"""Generated meeting text, isolated SQLite and intercepted model transport only."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
import io
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.contracts import ServiceError
from backend.gemini import GeminiClient
from backend.meetings import MAX_CONTENT, normalize, segments, supported_deadline
from backend.service import LocalService
from backend.storage import Store

ORIGINAL = "会议确认改为线上评审。\n李明：2026-10-12 前发送评审资料。\n全体成员：配合核对。\n联系电话13800138000\n"

def evidence(segment="P2", quote="李明：2026-10-12 前发送评审资料。"):
    return [{"segment_id": segment, "quote": quote}]

def task(**fields):
    return {"title": "发送评审资料", "responsible": "李明", "due_date": "2026-10-12",
        "due_text": "2026-10-12", "evidence": evidence(), **fields}

def point(**fields):
    return {"title": "评审改为线上", "summary": "会议确认本次评审在线上举行。", "reason": "需要知晓会议方式。",
        "evidence": evidence("P1", "会议确认改为线上评审。"), **fields}

def answer():
    return {"meeting_tasks": [task()], "key_points": [point()]}

class Client:
    def __init__(self):
        self.calls, self.response, self.callback, self.error = [], answer(), None, None
    def generate(self, config, key, prompt, **kwargs):
        self.calls.append(json.loads(prompt.partition("输入：")[2]))
        if self.callback: self.callback()
        if self.error: raise self.error
        return deepcopy(self.response), {"input_tokens": 120, "output_tokens": 60, "total_tokens": 200}

class MeetingTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="meeting-generated-")
        self.addCleanup(self.root.cleanup)
        self.now = datetime.fromisoformat("2026-10-09T02:00:00+00:00").timestamp()
        self.client = Client()
        self.s = LocalService(self.root.name, model_client=self.client, clock=lambda:self.now)
        self.addCleanup(self.s.close)
        self.s.models.save(base_url="https://generated.invalid", model="generated-flash", api_key="generated-local-key")
        self.socket = patch("socket.socket", side_effect=AssertionError("live network forbidden"))
        self.socket.start(); self.addCleanup(self.socket.stop)

    def save(self, **fields):
        return self.s.dispatch("meeting.save", {"title":"项目评审", "content": ORIGINAL, **fields})
    def preview(self, note):
        return self.s.dispatch("meeting.preview", {"meeting_id": note["meeting_id"], "revision": note["revision"]})
    def generate(self, note, p=None):
        p = p or self.preview(note)
        return self.s.dispatch("meeting.generate", {"meeting_id": note["meeting_id"], "revision": note["revision"],
            "preview_token": p["preview_token"], "authorize": True})
    def publish(self, note, ids=None):
        return self.s.dispatch("meeting.publish", {"meeting_id":note["meeting_id"], "revision":note["revision"],
            "candidate_ids":ids or [c["candidate_id"] for c in note["result"]["candidates"]], "confirm":True})
    def detail(self, note):
        return self.s.dispatch("meeting.detail", {"meeting_id":note["meeting_id"]})
    def count(self, table):
        with self.s.store.database() as db: return db.execute('SELECT COUNT(*) FROM "'+table+'"').fetchone()[0]

    def test_save_is_local_original_exact_and_has_no_side_effects(self):
        settings, calls = self.s.store.settings(), self.count("model_calls")
        note = self.save(file_name="会议.md")
        self.assertEqual(note["original"], ORIGINAL)
        self.assertIsNone(note["meeting_date"])
        self.assertEqual(note["status"], "draft")
        self.assertEqual(note["file_name"], "会议.md")
        self.assertEqual(self.s.store.settings(), settings)
        self.assertEqual(self.count("model_calls"), calls)
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("tasks"), 0)

    def test_save_without_model_and_without_wechat_connection(self):
        config = self.s.models.public()
        self.s.models.delete(revision=config["revision"], confirm=True)
        note = self.save()
        self.assertEqual(note["status"], "draft")
        with self.assertRaises(ServiceError): self.preview(note)
        self.assertEqual(self.client.calls, [])

    def test_invalid_input_and_file_metadata_are_rejected(self):
        for fields in ({"content":" "}, {"content":"x\0y"}, {"content":"字"*(MAX_CONTENT+1)},
                       {"content":123}, {"content":"\ud800"}, {"meeting_date":"2026-02-30"}, {"meeting_date":"next day"},
                       {"title":"x"*201}, {"file_name":"report.pdf"}, {"file_name":"../meeting.txt"}):
            with self.subTest(fields=list(fields)), self.assertRaises(ServiceError): self.save(**fields)
        self.assertEqual(self.count("meeting_notes"), 0)

    def test_duplicate_is_account_and_date_scoped_without_resend(self):
        first = self.save(); second = self.save(title="另一个标题")
        self.assertEqual(first["meeting_id"], second["meeting_id"])
        self.assertTrue(second["duplicate"])
        third = self.save(meeting_date="2026-10-09")
        self.assertNotEqual(first["meeting_id"], third["meeting_id"])
        self.assertEqual(self.client.calls, [])

    def test_preview_redacts_and_does_not_grant_automatic_access(self):
        note = self.save(); settings = self.s.store.settings(); p = self.preview(note)
        wire = json.dumps(p, ensure_ascii=False)
        self.assertNotIn("13800138000", wire)
        self.assertNotIn("generated-local-key", wire)
        self.assertEqual(p["base_url"], "https://generated.invalid")
        self.assertEqual(self.detail(note)["original"], ORIGINAL)
        self.assertEqual(self.s.store.settings(), settings)
        self.assertEqual(self.count("text_grants"), 0)
        self.assertEqual(self.count("model_calls"), 0)

    def test_send_requires_explicit_authorization_and_single_use_preview(self):
        note = self.save(); p = self.preview(note)
        with self.assertRaises(ServiceError):
            self.s.dispatch("meeting.generate", {"meeting_id":note["meeting_id"], "revision":note["revision"], "preview_token":p["preview_token"]})
        ready = self.generate(note,p)
        self.assertEqual(ready["status"], "ready")
        with self.assertRaises(ServiceError): self.generate(note,p)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.count("tasks"), 0)
        self.assertEqual(self.count("attention_items"), 0)

    def test_model_change_expired_preview_and_wrong_token_do_not_send(self):
        note = self.save(); p = self.preview(note)
        self.s.models.enable(enabled=True)
        with self.assertRaises(ServiceError): self.generate(note,p)
        p = self.preview(note); self.now += 1800
        with self.assertRaises(ServiceError): self.generate(note,p)
        p = self.preview(note); p["preview_token"] = "wrong"
        with self.assertRaises(ServiceError): self.generate(note,p)
        self.assertEqual(self.client.calls, [])

    def test_generating_another_account_cannot_read_or_send(self):
        note = self.save(); p = self.preview(note)
        self.s.store.configure({"mode":"local","root":self.root.name,"account_id":"other","self_id":None}, "2026-10-01T00:00:00+00:00", [])
        with self.assertRaises(ServiceError): self.detail(note)
        with self.assertRaises(ServiceError): self.generate(note,p)
        self.assertEqual(self.s.dispatch("meeting.list", {})["total"], 0)
        self.assertEqual(self.client.calls, [])

    def test_only_selected_candidates_publish_atomically_with_full_source(self):
        note = self.generate(self.save()); settings = self.s.store.settings(); revision = self.s.store.results_revision()
        value = self.publish(note, ["T1"])
        self.assertEqual(value["status"], "published")
        self.assertEqual(value["result"]["task_count"], 1)
        self.assertEqual(self.count("attention_items"), 0)
        current = self.s.dispatch("tasks.list", {})["items"][0]
        self.assertEqual(current["title"], "李明：发送评审资料")
        self.assertEqual((current["owner_type"],current["status"]), ("unknown","needs_confirmation"))
        self.assertEqual(current["due_at"], "2026-10-12")
        source = self.s.dispatch("message.detail", {"message_id":current["source_message_ids"][0]})
        self.assertEqual(source["content"], ORIGINAL)
        self.assertEqual(source["source_kind"], "meeting")
        self.assertIsNone(source["meeting_date"])
        self.assertFalse(source["analysis_eligible"])
        self.assertIsNone(source["effective_is_self"])
        self.assertEqual(self.s.store.settings(), settings)
        self.assertEqual(self.s.store.results_revision(), revision+1)
        self.assertIsNone(self.s.dispatch("today.list", {})["last_analysis"])
        self.assertEqual(self.s.dispatch("messages.chats", {})["total"], 0)
        self.assertEqual(self.s.dispatch("messages.list", {})["total"], 0)
        self.assertEqual(self.s.store.chats(), [])

    def test_points_join_today_and_claim_uses_existing_local_action(self):
        note = self.publish(self.generate(self.save(meeting_date="2026-10-09")))
        today = self.s.dispatch("today.list", {})
        point = today["attention_items"][0]
        self.assertEqual(point["kind"], "key_point")
        self.assertEqual((point["source_kind"],point["meeting_date"]), ("meeting","2026-10-09"))
        task = today["items"][0]
        self.s.dispatch("task.claim_self", {"task_id":task["task_id"], "revision":task["revision"]})
        claimed = self.s.dispatch("tasks.list", {})["items"][0]
        self.assertEqual(claimed["owner_type"], "self")
        self.assertEqual(self.s.models.usage()["today"]["requests"], 1)
        self.assertEqual(self.s.models.usage()["today"]["known_total_tokens"], 200)
        with self.s.store.database() as db: self.assertEqual(db.execute("SELECT purpose FROM model_calls").fetchone()[0], "meeting_notes")

    def test_publish_requires_confirmation_current_version_and_valid_selection(self):
        note = self.generate(self.save())
        for fields in ({"confirm":False}, {"revision":1}, {"candidate_ids":[]},
                       {"candidate_ids":["unknown"]}, {"candidate_ids":["T1","T1"]}):
            params = {"meeting_id":note["meeting_id"], "revision":note["revision"], "candidate_ids":["T1"], "confirm":True, **fields}
            with self.assertRaises(ServiceError): self.s.dispatch("meeting.publish", params)
        self.publish(note)
        with self.assertRaises(ServiceError): self.publish(note)
        self.assertEqual(self.count("tasks"), 1)
        self.assertEqual(self.count("attention_items"), 1)

    def test_publication_failure_rolls_back_all_business_rows(self):
        note = self.generate(self.save())
        with self.s.store.database() as db:
            db.execute("CREATE TRIGGER deny_point BEFORE INSERT ON attention_items BEGIN SELECT RAISE(ABORT,'blocked'); END")
        with self.assertRaises(ServiceError): self.publish(note)
        for table in ("tasks","attention_items","analysis_runs","messages","chats","task_evidence_links"):
            self.assertEqual(self.count(table), 0, table)
        self.assertEqual(self.detail(note)["status"], "ready")
        self.assertEqual(self.count("model_calls"), 1)

    def test_unknown_network_result_is_not_retried_and_original_survives(self):
        self.client.error = ServiceError("model_timeout_unknown", "unknown", retryable=True)
        note = self.save(); p = self.preview(note)
        with self.assertRaises(ServiceError): self.generate(note,p)
        failed = self.detail(note)
        self.assertEqual((failed["status"],failed["original"]), ("failed",ORIGINAL))
        with self.assertRaises(ServiceError): self.generate(failed,p)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.count("tasks"), 0)
        self.assertEqual(self.s.models.usage()["today"]["unknown_usage_requests"], 1)

    def test_model_or_account_change_during_generation_prevents_candidate_storage(self):
        self.client.callback = lambda:self.s.models.enable(enabled=False)
        note = self.save()
        with self.assertRaises(ServiceError): self.generate(note)
        self.assertEqual(self.detail(note)["status"], "failed")
        self.assertIsNone(self.detail(note)["result"])
        self.assertEqual(self.count("tasks"), 0)

    def test_independent_bad_evidence_does_not_block_valid_candidates(self):
        self.client.response["meeting_tasks"].append(task(evidence=evidence("P999")))
        ready = self.generate(self.save())
        self.assertEqual(ready["result"]["rejected_count"], 1)
        self.assertEqual(len(ready["result"]["candidates"]), 2)
        self.publish(ready)
        self.assertEqual(self.count("tasks"), 1)

    def test_no_valid_candidates_keeps_original_and_never_publishes_fake_results(self):
        self.client.response = {"meeting_tasks":[],"key_points":[]}
        ready = self.generate(self.save())
        self.assertEqual(ready["result"]["candidates"], [])
        with self.assertRaises(ServiceError): self.s.dispatch("meeting.publish", {"meeting_id":ready["meeting_id"], "revision":ready["revision"],"candidate_ids":[],"confirm":True})
        self.assertEqual(self.count("analysis_runs"), 0)

    def test_restart_preserves_ready_preview_without_resend(self):
        ready = self.generate(self.save()); self.s.close()
        reopened = LocalService(self.root.name, model_client=self.client)
        self.addCleanup(reopened.close)
        value = reopened.dispatch("meeting.detail", {"meeting_id":ready["meeting_id"]})
        self.assertEqual(value["result"],ready["result"])
        self.assertEqual(value["status"],"ready")
        self.assertEqual(len(self.client.calls),1)

    def test_interrupted_generation_recovers_without_retry(self):
        note = self.save()
        with self.s.store.database() as db: db.execute("UPDATE meeting_notes SET status='generating' WHERE meeting_id=?", (note["meeting_id"],))
        self.s.meetings.recover()
        self.assertEqual(self.detail(note)["status"],"failed")
        self.assertEqual(self.detail(note)["error_code"],"interrupted")
        self.assertEqual(self.client.calls,[])

    def test_history_is_paged_searched_and_does_not_return_originals(self):
        for i in range(25): self.save(title=f"会议 {i}",content=ORIGINAL+str(i))
        page = self.s.dispatch("meeting.list", {"limit":20})
        self.assertEqual((page["total"],len(page["items"]),page["has_more"]),(25,20,True))
        self.assertTrue(all("original" not in row for row in page["items"]))
        self.assertEqual(len(self.s.dispatch("meeting.list", {"offset":20})["items"]),5)
        self.assertEqual(self.s.dispatch("meeting.list", {"query":"会议 24"})["total"],1)
        for params in ({"limit":True},{"offset":-1},{"query":1}):
            with self.assertRaises(ServiceError): self.s.dispatch("meeting.list",params)

    def test_actual_gemini_adapter_carries_only_redacted_meeting_and_expected_schema(self):
        class Opener:
            def open(inner, request, timeout):
                self.assertTrue(request.full_url.endswith("/v1beta/models/generated-flash:generateContent"))
                wire = json.loads(request.data)
                schema = wire["generationConfig"]["responseSchema"]
                self.assertEqual(set(schema["properties"]),{"meeting_tasks","key_points"})
                prompt = wire["contents"][0]["parts"][0]["text"]
                self.assertNotIn("13800138000",prompt)
                self.assertNotIn("generated-local-key",prompt)
                payload = json.loads(prompt.partition("输入：")[2])
                self.assertEqual(set(payload),{"title","meeting_date","segments"})
                return io.BytesIO(json.dumps({"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":json.dumps(answer())}]}}],
                    "usageMetadata":{"promptTokenCount":120,"candidatesTokenCount":60,"totalTokenCount":200}}).encode())
        self.s.models.client=GeminiClient(opener=Opener())
        ready=self.generate(self.save())
        self.assertEqual(len(ready["result"]["candidates"]),2)


class MeetingEvidenceTests(unittest.TestCase):
    def test_relative_dates_require_actual_meeting_date_not_import_time(self):
        self.assertIsNone(supported_deadline("2026-10-10","明天","明天发送资料",None))
        self.assertEqual(supported_deadline("2026-10-10","明天","明天发送资料","2026-10-09"),"2026-10-10")
        self.assertEqual(supported_deadline("2026-10-12","下周一","下周一发送资料","2026-10-09"),"2026-10-12")
        self.assertIsNone(supported_deadline("2026-10-13","下周一","下周一发送资料","2026-10-09"))

    def test_unknown_relative_deadline_is_kept_as_unconfirmed_wording(self):
        raw=task(due_date="2026-10-10",due_text="明天",evidence=evidence("P1","李明明天发资料"))
        result=normalize({"meeting_tasks":[raw],"key_points":[]},segments("李明明天发资料"),None)
        self.assertEqual(result["candidates"][0]["due_text"],"明天")
        self.assertIsNone(result["candidates"][0]["due_at"])

    def test_responsible_and_quote_must_exist_and_date_not_invented(self):
        for fields in ({"responsible":"虚构人"},{"evidence":evidence(quote="不存在的文字")},{"title":""}):
            result=normalize({"meeting_tasks":[task(**fields)],"key_points":[point()]},segments(ORIGINAL),None)
            self.assertEqual(len(result["candidates"]),1)
            self.assertEqual(result["rejected_count"],1)
        result=normalize({"meeting_tasks":[task(due_date="2026-10-13")],"key_points":[]},segments(ORIGINAL),None)
        self.assertIsNone(result["candidates"][0]["due_at"])

    def test_segments_preserve_all_long_text_before_redaction(self):
        original="评审资料"*2000+"\r\n末段内容"
        parts=segments(original)
        self.assertEqual("".join(part["content"] for part in parts),original)
        self.assertTrue(all(len(part["content"])<=2000 for part in parts))

    def test_sensitive_text_across_segment_boundary_is_redacted(self):
        parts=segments("字"*1995+"13800138000"+"字"*30)
        joined="".join(part["content"] for part in parts)
        self.assertNotIn("13800138000", joined)
        self.assertNotIn("13800", joined)

class MeetingMigrationTests(unittest.TestCase):
    def test_v9_upgrade_preserves_every_existing_row_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            old=Store(root)
            with old.database() as db:
                for table in ("ask_citations","ask_runs","ask_turns","ask_threads","ask_grants","ask_fts","ask_index"): db.execute("DROP TABLE "+table)
                for table in ("news_settings","news_runs","news_editions"): db.execute("DROP TABLE "+table)
                db.execute("DROP TABLE meeting_notes"); db.execute("PRAGMA user_version=9")
                db.execute("INSERT INTO tags VALUES('keep','工作')")
                names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                before={n:list(map(tuple,db.execute('SELECT * FROM "'+n+'" ORDER BY 1'))) for n in names}
            for _ in range(2):
                upgraded=Store(root)
                with upgraded.database() as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],12)
                    self.assertEqual({n:list(map(tuple,db.execute('SELECT * FROM "'+n+'" ORDER BY 1'))) for n in names},before)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM meeting_notes").fetchone()[0],0)

    def test_v9_conflict_fails_without_silently_accepting_wrong_schema(self):
        with tempfile.TemporaryDirectory() as root:
            old=Store(root)
            with old.database() as db: db.execute("PRAGMA user_version=9")
            with self.assertRaises(ServiceError): Store(root)
            with closing(sqlite3.connect(old.path)) as db: self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],9)
