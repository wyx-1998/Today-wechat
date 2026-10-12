"""Independent local-model/legacy-migration checks; no private/network I/O."""

from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.error

from backend.contracts import ServiceError
from backend.demo import CHATS
from backend.gemini import GeminiClient, normalize_endpoint
from backend.service import LocalService
from backend.storage import Store, SCHEMA
from backend.redaction import redact

KEY = "synthetic-public-key-not-for-real-use"


def install_legacy_config(service, key=KEY):
    """Build an old encrypted fixture without a product-only fixture command."""
    from Crypto.Cipher import AES
    row = service.models.row()
    cipher = AES.new(service.models.master(create=True), AES.MODE_GCM)
    cipher.update(service.models.aad(row))
    ciphertext, tag = cipher.encrypt_and_digest(key.encode())
    with service.store.database() as db:
        db.execute("DELETE FROM model_local_keys WHERE capability='text'")
        db.execute("UPDATE model_configs SET nonce=?,ciphertext=?,auth_tag=? WHERE capability='text'", (cipher.nonce, ciphertext, tag))


class MemoryKeychain:
    def __init__(self):
        self.value = None

    def master(self, account, *, create=False):
        if self.value is None and create:
            self.value = bytes(range(32))
        if self.value is None:
            raise ServiceError("keychain_missing", "Synthetic missing key")
        return self.value


class FakeClient:
    def __init__(self):
        self.calls = []
        self.callback = None
        self.invalid = False

    def generate(self, config, key, prompt, *, schema=None, json_response=True):
        self.calls.append((config["base_url"], key, bool(schema)))
        if self.callback:
            self.callback()
        if not schema:
            return "Hello from AIGoCode!" if not json_response else {"ok": True}, {"input_tokens": None, "output_tokens": None, "total_tokens": None}
        payload = json.loads(prompt.split("输入：", 1)[1])
        if "decisions" in schema.get("properties", {}):
            own = next(m for m in payload["messages"] if m["speaker"] == "本人")
            return {"decisions": [{"candidate_id": c["candidate_id"], "relationship": "self",
                "evidence": [{"source_message_id": own["source_message_id"], "quote": own["content"], "basis": "self_action"}]}
                for c in payload["candidates"]]}, {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130}
        payload = payload["new_messages"] if isinstance(payload, dict) else payload
        source = "synthetic-unknown-source" if self.invalid else payload[0]["source_message_id"]
        return {"tasks": [{"title": "发送合成方案", "owner_type": "self", "due_date": None,
                           "source_message_ids": [source]}]}, {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130}


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="model-only-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.keychain, self.client = MemoryKeychain(), FakeClient()
        self.service = LocalService(self.temp.name, keychain=self.keychain, model_client=self.client)
        self.addCleanup(self.service.close)

    def save(self):
        return self.service.models.save(base_url="https://synthetic.invalid", model="synthetic-flash", api_key=KEY)

    def prepare(self):
        self.save(); self.service.connect_demo()
        self.service.store.update_chat(CHATS[0][0], sync_enabled=True, analysis_enabled=True)
        self.service.sync()

    def analyze(self):
        return self.service.analysis.run(authorize=True, preview_token=self.service.analysis.preview()["preview_token"])

    def prepare_saved_real_fixture(self):
        # Still entirely generated data; no real WeChat files or network.
        self.prepare()
        source = self.service.store.settings()["source"] | {"mode": "local", "account_id": "fixture-real-policy"}
        with self.service.store.database() as db:
            db.execute("UPDATE chats SET account_id=?,analysis_enabled=0", (source["account_id"],))
            db.execute("UPDATE messages SET account_id=?,is_self=NULL", (source["account_id"],))
            self.service.store._set(db, "source", source)
            self.service.store._set(db, "paused", True)
        return CHATS[0][0]

    def test_real_saved_text_needs_single_use_preview_and_no_persistent_grant(self):
        chat = self.prepare_saved_real_fixture()
        # This scenario intentionally has no configured identity, not just
        # historical NULL flags. Configured-identity recovery is checked separately.
        with self.service.store.database() as db:
            source = self.service.store._settings(db)["source"]
            self.service.store._set(db, "source", source | {"self_id": None})
        with self.assertRaises(ServiceError):
            self.service.analysis.run_real(chat_id=chat, authorize=True)
        preview = self.service.analysis.real_preview(chat_id=chat)
        self.assertEqual(preview["message_count"], 3)
        for value in (False, 1, "true"):
            with self.assertRaises(ServiceError):
                self.service.analysis.run_real(chat_id=chat, authorize=value, preview_token=preview["preview_token"])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.service.analysis.run_real(chat_id=chat, authorize=True, preview_token=preview["preview_token"])["task_count"], 0)
        # Unidentified group participants no longer justify personal ownership
        # confirmation. The frozen preview is still consumed exactly once.
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertTrue(self.service.store.settings()["paused"])
        self.assertFalse(self.service.store.chats()[-1]["analysis_enabled"])
        with self.assertRaises(ServiceError):
            self.service.analysis.run_real(chat_id=chat, authorize=True, preview_token=preview["preview_token"])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.service.analysis.real_preview(chat_id=chat)["message_count"], 0)

    def test_real_preview_allows_sync_but_rejects_other_chat_and_demo(self):
        self.prepare()
        with self.assertRaises(ServiceError):
            self.service.analysis.real_preview(chat_id=CHATS[0][0])
        chat = self.prepare_saved_real_fixture()
        with self.assertRaises(ServiceError):
            self.service.analysis.real_preview(chat_id=CHATS[1][0])
        self.service.resume()
        self.assertEqual(self.service.analysis.real_preview(chat_id=chat)['message_count'], 3)
        self.assertFalse(self.service.store.settings()['paused'])
        self.assertEqual(self.client.calls, [])

    def test_real_preview_changed_content_or_model_stops_before_send(self):
        chat = self.prepare_saved_real_fixture()
        preview = self.service.analysis.real_preview(chat_id=chat)
        with self.service.store.database() as db:
            db.execute("UPDATE messages SET content='synthetic changed text' WHERE message_id=?", (preview["messages"][0]["source_message_id"],))
        with self.assertRaises(ServiceError):
            self.service.analysis.run_real(chat_id=chat, authorize=True, preview_token=preview["preview_token"])
        preview = self.service.analysis.real_preview(chat_id=chat)
        self.service.models.enable(enabled=False)
        with self.assertRaises(ServiceError):
            self.service.analysis.run_real(chat_id=chat, authorize=True, preview_token=preview["preview_token"])
        self.assertEqual(self.client.calls, [])

    def test_real_failed_response_consumes_preview_and_restart_has_no_grant(self):
        chat = self.prepare_saved_real_fixture()
        preview = self.service.analysis.real_preview(chat_id=chat)
        self.client.invalid = True
        with self.assertRaises(ServiceError):
            self.service.analysis.run_real(chat_id=chat, authorize=True, preview_token=preview["preview_token"])
        with self.assertRaises(ServiceError):
            self.service.analysis.run_real(chat_id=chat, authorize=True, preview_token=preview["preview_token"])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        preview = self.service.analysis.real_preview(chat_id=chat)
        reopened = LocalService(self.temp.name, keychain=self.keychain, model_client=self.client)
        self.addCleanup(reopened.close)
        with self.assertRaises(ServiceError):
            reopened.analysis.run_real(chat_id=chat, authorize=True, preview_token=preview["preview_token"])
        self.assertEqual(len(self.client.calls), 1)

    def test_real_resume_during_response_keeps_fixed_input(self):
        chat = self.prepare_saved_real_fixture()
        preview = self.service.analysis.real_preview(chat_id=chat)
        self.client.callback = self.service.resume
        self.assertEqual(self.service.analysis.run_real(chat_id=chat, authorize=True, preview_token=preview['preview_token'])['status'], 'success')
        self.assertEqual(len(self.service.analysis.tasks()['items']), 1)
        self.assertEqual(len(self.client.calls), 2)

    def test_local_plaintext_only_database_public_state_mask_and_restart(self):
        public = self.save()
        self.assertNotIn(KEY, json.dumps(public))
        self.assertIn(KEY.encode(), self.service.store.path.read_bytes())
        self.assertEqual(public["storage_mode"], "sqlite_plaintext")
        self.assertEqual(self.service.models.decrypt(self.service.models.row()), KEY)
        reopened = LocalService(self.temp.name, keychain=self.keychain, model_client=self.client)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.models.decrypt(reopened.models.row()), KEY)
        self.assertEqual(reopened.models.public()["test_status"], "untested")

    def test_bad_ciphertext_or_changed_address_fails_before_send(self):
        self.save(); install_legacy_config(self.service)
        with self.service.store.database() as db:
            db.execute("UPDATE model_configs SET base_url='https://other.invalid'")
        with self.assertRaises(ServiceError) as error:
            self.service.models.migrate(revision=self.service.models.public()["revision"], confirm=True)
        self.assertEqual(error.exception.code, "model_key_invalid")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.service.models.usage()["today"]["requests"], 0)

    def test_missing_keychain_does_not_regenerate_over_ciphertext(self):
        self.save(); install_legacy_config(self.service); self.keychain.value = None
        with self.assertRaises(ServiceError):
            self.service.models.migrate(revision=self.service.models.public()["revision"], confirm=True)
        self.assertIsNone(self.keychain.value)
        self.assertEqual(self.client.calls, [])

    def test_connection_requires_explicit_authorization_and_unknown_usage(self):
        self.save()
        for value in (False, None, 1, "true"):
            with self.assertRaises(ServiceError):
                self.service.models.test(authorize=value)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.service.models.test(authorize=True)["test_status"], "passed")
        usage = self.service.models.usage()["today"]
        self.assertEqual(usage["requests"], 1)
        self.assertIsNone(usage["known_total_tokens"])
        self.assertEqual(usage["unknown_usage_requests"], 1)

    def test_disabled_model_stops_new_requests_even_without_keychain(self):
        self.save(); self.keychain.value = None
        self.service.models.enable(enabled=False)
        with self.assertRaises(ServiceError):
            self.service.models.test(authorize=True)
        self.assertEqual(self.client.calls, [])

    def test_real_source_is_blocked_even_with_authorize_true(self):
        self.save()
        with self.assertRaises(ServiceError) as error:
            self.service.analysis.run(authorize=True)
        self.assertEqual(error.exception.code, "synthetic_only")
        self.assertEqual(self.client.calls, [])

    def test_source_scope_preview_change_prevents_upload(self):
        self.prepare()
        preview = self.service.analysis.preview()
        self.service.store.update_chat(CHATS[0][0], sync_enabled=True, analysis_enabled=False)
        result = self.service.analysis.run(authorize=True, preview_token=preview["preview_token"])
        self.assertEqual(result["status"], "no_new_messages")
        self.assertEqual(self.client.calls, [])

    def test_tasks_sourced_atomic_repeated_batch_and_user_complete(self):
        self.prepare()
        self.assertEqual(self.analyze()["task_count"], 1)
        task = self.service.analysis.tasks()["items"][0]
        source = self.service.store.message_detail(task["source_message_ids"][0])
        self.assertTrue(source["is_self"])
        self.service.analysis.update_task(task_id=task["task_id"], revision=task["revision"], status="completed")
        self.assertEqual(self.analyze()["status"], "no_new_messages")
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.service.analysis.tasks()["items"][0]["status"], "completed")
        with self.assertRaises(ServiceError):
            self.service.analysis.update_task(task_id=task["task_id"], revision=task["revision"], status="pending")

    def test_invalid_source_rejects_entire_result_and_count_is_real(self):
        self.prepare(); self.client.invalid = True
        with self.assertRaises(ServiceError) as error:
            self.analyze()
        self.assertEqual(error.exception.code, "analysis_evidence")
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual(self.service.models.usage()["today"]["requests"], 1)
        self.client.invalid = False
        self.assertEqual(self.analyze()["task_count"], 1)
        self.assertEqual(self.service.models.usage()["today"]["requests"], 3)

    def test_pause_during_response_does_not_publish_but_counts_call(self):
        self.prepare(); self.client.callback = self.service.pause
        with self.assertRaises(ServiceError):
            self.analyze()
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual(self.service.models.usage()["today"]["requests"], 1)

    def test_disable_during_response_does_not_publish_or_deadlock(self):
        self.prepare(); self.client.callback = lambda: self.service.models.enable(enabled=False)
        with self.assertRaises(ServiceError):
            self.analyze()
        self.assertEqual(self.service.analysis.tasks()["items"], [])

    def test_request_response_and_key_not_saved_as_diagnostic_bodies(self):
        self.prepare(); self.analyze()
        with self.service.store.database() as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(model_calls)")}
            self.assertFalse({"request", "response", "body", "prompt", "api_key"} & columns)
            text = json.dumps([tuple(r) for r in db.execute("SELECT * FROM model_calls")])
        self.assertNotIn(KEY, text)
        self.assertNotIn("合成测试原文", text)

    def test_seven_fixed_redaction_categories_and_original_preserved(self):
        text = "13800138000 11010119900101123X 6222021234567890123 fake@example.invalid 密码=syntheticpw sk-syntheticabcdefgh token=synthetictoken"
        safe, categories = redact(text)
        self.assertEqual(set(categories), {"phone", "id_card", "bank_card", "email", "password", "api_key", "token"})
        self.assertNotIn("13800138000", safe)
        self.assertIn("13800138000", text)

    def test_version_one_migrates_without_deleting_existing_tables(self):
        root = Path(self.temp.name) / "migration"
        root.mkdir()
        with closing(sqlite3.connect(root / "application.sqlite3")) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO tags VALUES('synthetic-id','保留合成标签')")
            db.commit()
        store = Store(root)
        with store.database() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 12)
            self.assertEqual(db.execute("SELECT name FROM tags").fetchone()[0], "保留合成标签")
        self.assertIsNone(LocalService(root, keychain=self.keychain, model_client=self.client).models.public())

    def test_native_gemini_endpoint_header_and_no_false_zero_usage(self):
        class Opener:
            def open(inner, request, timeout):
                self.assertEqual(request.full_url, "https://synthetic.invalid/v1beta/models/synthetic-flash:generateContent")
                self.assertEqual(request.get_header("X-goog-api-key"), KEY)
                self.assertNotIn(KEY, request.full_url)
                return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"ok":true}'}]}}]}).encode())
        answer, usage = GeminiClient(opener=Opener()).generate({"base_url": "https://synthetic.invalid", "model": "synthetic-flash"}, KEY, "synthetic prompt")
        self.assertEqual(answer, {"ok": True})
        self.assertIsNone(usage["total_tokens"])

    def test_actual_adapter_aliases_publish_original_sources_not_wire_ids(self):
        chat = self.prepare_saved_real_fixture()
        preview = self.service.analysis.real_preview(chat_id=chat)
        class Opener:
            def open(inner, request, timeout):
                wire = json.loads(request.data)["contents"][0]["parts"][0]["text"]
                data = json.loads(wire.partition("输入：")[2])
                if "candidates" in data:
                    own = data["messages"][0]
                    self.assertEqual(own["speaker"], "本人")
                    answer = {"decisions": [{"candidate_id": "R1", "relationship": "self", "evidence": [
                        {"source_message_id": own["source_message_id"], "quote": own["content"], "basis": "self_action"}]}]}
                else:
                    self.assertEqual(len(data["new_messages"]), 3)
                    self.assertEqual(data["new_messages"][0]["source_message_id"], "M1")
                    answer = {"tasks": [{"title": "发送资料", "owner_type": "unknown", "due_date": None,
                        "source_message_ids": ["M1"]}]}
                return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(answer)}]}}],
                    "usageMetadata": {"totalTokenCount": 130}}).encode())
        self.service.models.client = GeminiClient(opener=Opener())
        self.service.analysis.run_real(chat_id=chat, authorize=True, preview_token=preview["preview_token"])
        task = self.service.analysis.tasks()["items"][0]
        self.assertEqual(task["source_message_ids"], [preview["messages"][0]["source_message_id"]])
        self.assertEqual(task["status"], "pending")
        self.assertEqual(self.service.models.usage()["today"]["known_total_tokens"], 260)

    def test_actual_adapter_unknown_alias_rejects_all_tasks_and_keeps_usage(self):
        chat = self.prepare_saved_real_fixture()
        preview = self.service.analysis.real_preview(chat_id=chat)
        class Opener:
            def open(inner, request, timeout):
                answer = {"tasks": [{"title": "发送资料", "owner_type": "unknown", "due_date": None,
                    "source_message_ids": ["M999"]}]}
                return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(answer)}]}}],
                    "usageMetadata": {"totalTokenCount": 130}}).encode())
        self.service.models.client = GeminiClient(opener=Opener())
        with self.assertRaises(ServiceError):
            self.service.analysis.run_real(chat_id=chat, authorize=True, preview_token=preview["preview_token"])
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual(self.service.models.usage()["today"]["known_total_tokens"], 130)

    def test_actual_adapter_late_result_after_source_change_cannot_publish(self):
        chat = self.prepare_saved_real_fixture()
        preview = self.service.analysis.real_preview(chat_id=chat)
        class Opener:
            def open(inner, request, timeout):
                self.service.store.set_range("2001-01-01T00:00:00+00:00")
                answer = {"tasks": [{"title": "发送资料", "owner_type": "unknown", "due_date": None,
                    "source_message_ids": ["M1"]}]}
                return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(answer)}]}}],
                    "usageMetadata": {"totalTokenCount": 130}}).encode())
        self.service.models.client = GeminiClient(opener=Opener())
        with self.assertRaises(ServiceError):
            self.service.analysis.run_real(chat_id=chat, authorize=True, preview_token=preview["preview_token"])
        self.assertEqual(self.service.analysis.tasks()["items"], [])
        self.assertEqual(self.service.models.usage()["today"]["known_total_tokens"], 130)

    def test_model_error_does_not_expose_provider_error_body_or_redirect_key(self):
        for code, expected in ((302, "model_redirect"), (401, "model_auth"), (404, "model_endpoint"), (429, "model_rate_limit")):
            class Opener:
                def open(inner, request, timeout):
                    raise urllib.error.HTTPError(request.full_url, code, "secret-reflection", {}, io.BytesIO(KEY.encode()))
            with self.subTest(code=code), self.assertRaises(ServiceError) as error:
                GeminiClient(opener=Opener()).generate({"base_url": "https://synthetic.invalid", "model": "synthetic-flash"}, KEY, "synthetic")
            self.assertEqual(error.exception.code, expected)
            self.assertNotIn(KEY, str(error.exception))

    def test_numeric_http_failure_persists_without_key_or_automatic_retry(self):
        self.save()
        class Opener:
            count = 0
            def open(inner, request, timeout):
                inner.count += 1
                raise urllib.error.HTTPError(request.full_url, 503, KEY, {}, io.BytesIO(KEY.encode()))
        opener = Opener()
        self.service.models.client = GeminiClient(opener=opener)
        with self.assertRaises(ServiceError) as caught:
            self.service.models.test(authorize=True)
        self.assertEqual(caught.exception.code, "model_http_503")
        with self.service.store.database() as db:
            row = dict(db.execute("SELECT * FROM model_calls").fetchone())
        self.assertEqual((row["outcome"], row["error_code"], row["total_tokens"]), ("failed", "model_http_503", None))
        self.assertNotIn(KEY, json.dumps(row))
        self.assertEqual(self.service.models.usage()["today"]["unknown_usage_requests"], 1)
        self.service.models.public()
        self.service.models.usage()
        self.assertEqual(opener.count, 1)

    def test_connection_uses_exact_minimal_contents_without_generation_config(self):
        class Opener:
            def open(inner, request, timeout):
                self.assertEqual(json.loads(request.data), {"contents": [{"role": "user", "parts": [{"text": "请只回复：Hello from AIGoCode!"}]}]})
                self.assertEqual(request.get_header("X-goog-api-key"), KEY)
                self.assertEqual(request.get_header("Content-type"), "application/json")
                self.assertEqual(request.get_header("User-agent"), "wechat-today/0.1.0")
                self.assertEqual(request.get_header("Accept"), "application/json")
                return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "Hello from AIGoCode!"}]}}]}).encode())
        self.save()
        self.service.models.client = GeminiClient(opener=Opener())
        self.assertEqual(self.service.models.test(authorize=True)["test_status"], "passed")

    def test_thought_signature_is_not_answer_and_total_usage_includes_thinking(self):
        class Opener:
            def open(inner, request, timeout):
                return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [
                    {"thought": True, "text": "synthetic thinking not an answer"},
                    {"thoughtSignature": "synthetic-signature", "text": "Hello from AIGoCode!"}]}}],
                    "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 7,
                                      "totalTokenCount": 151, "thoughtsTokenCount": 132}}).encode())
        answer, usage = GeminiClient(opener=Opener()).generate(
            {"base_url": "https://synthetic.invalid", "model": "synthetic-flash"}, KEY,
            "请只回复：Hello from AIGoCode!", json_response=False)
        self.assertEqual(answer, "Hello from AIGoCode!")
        self.assertEqual(usage, {"input_tokens": 12, "output_tokens": 7, "total_tokens": 151})

    def test_html_gateway_rejection_is_not_claimed_as_bad_key_and_body_not_read(self):
        class UnreadableBody(io.BytesIO):
            def read(inner, *args):
                self.fail("Provider error bodies must not be read")
        for code, media_type, expected in ((403, "text/html; charset=utf-8", "model_gateway_denied"),
                                           (403, "application/json", "model_auth"),
                                           (401, "application/json", "model_auth")):
            class Opener:
                def open(inner, request, timeout):
                    raise urllib.error.HTTPError(request.full_url, code, KEY,
                        {"Content-Type": media_type, "X-Secret": KEY}, UnreadableBody(KEY.encode()))
            with self.subTest(code=code, media_type=media_type), self.assertRaises(ServiceError) as error:
                GeminiClient(opener=Opener()).generate({"base_url": "https://synthetic.invalid", "model": "synthetic-flash"}, KEY, "synthetic")
            self.assertEqual(error.exception.code, expected)
            self.assertIn(str(code), error.exception.message)
            self.assertNotIn(KEY, json.dumps(error.exception.payload()))

    def test_addresses_cannot_contain_credentials_query_or_unexpected_path(self):
        for value in ("http://synthetic.invalid", "https://[broken", "https://user:secret@synthetic.invalid", "https://synthetic.invalid/?key=x", "https://synthetic.invalid/v1/chat/completions"):
            with self.assertRaises(ServiceError):
                normalize_endpoint(value)
        self.assertEqual(normalize_endpoint("https://synthetic.invalid/v1beta/"), "https://synthetic.invalid/v1beta")

    def test_named_api_key_without_sk_prefix_is_redacted(self):
        safe, categories = redact("API_KEY=syntheticabcdef 访问密钥：synthetic123")
        self.assertEqual(categories, ["api_key"])
        self.assertNotIn("synthetic", safe)

    def test_changed_model_invalidates_preview_before_request(self):
        self.prepare()
        token = self.service.analysis.preview()["preview_token"]
        self.service.models.save(base_url="https://synthetic.invalid", model="another-synthetic", api_key=None)
        with self.assertRaises(ServiceError) as error:
            self.service.analysis.run(authorize=True, preview_token=token)
        self.assertEqual(error.exception.code, "analysis_preview_changed")
        self.assertEqual(self.client.calls, [])

    def test_timeout_is_unknown_not_free_and_never_auto_retried(self):
        class Opener:
            def open(inner, request, timeout):
                raise TimeoutError()
        self.save()
        self.service.models.client = GeminiClient(opener=Opener())
        with self.assertRaises(ServiceError) as error:
            self.service.models.test(authorize=True)
        self.assertEqual(error.exception.code, "model_timeout_unknown")
        usage = self.service.models.usage()["today"]
        self.assertEqual(usage["requests"], 1)
        self.assertEqual(usage["failed_or_unknown"], 1)
        self.assertIsNone(usage["known_total_tokens"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
