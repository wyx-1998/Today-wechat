"""Local text credentials and counted requests; legacy migration is explicit."""

import json
import threading
import time
import uuid

from backend.contracts import ServiceError, iso_time, unknown_model_result
from backend.gemini import GeminiClient, normalize_endpoint, model_name
from backend.credentials import system_credentials


class Models:
    def __init__(self, store, *, keychain=None, client=None, clock=time.time, entry_lock=None, capability="text"):
        if capability not in {"text", "ask"}:
            raise ValueError("Unsupported capability")
        self.capability = capability
        self.store = store
        self.keychain = keychain if keychain is not None else system_credentials()
        self.client = client or GeminiClient()
        self.clock = clock
        self.lock = threading.RLock()
        self.entry_lock = entry_lock or threading.RLock()
        self.closed = False

    def invalidate_grant(self, db, reason):
        if self.capability == "text":
            self.store.invalidate_text_grant(db, reason)
        else:
            db.execute("UPDATE ask_grants SET status='revoked' WHERE status='active'")

    def row(self):
        with self.store.database() as db:
            row = db.execute("SELECT * FROM model_configs WHERE capability=?", (self.capability,)).fetchone()
            return dict(row) if row else None

    def public(self):
        row = self.row()
        if not row:
            return None
        return {key: row[key] for key in ("capability", "protocol", "base_url", "model", "revision", "test_status", "tested_at", "test_error")} | {"enabled": bool(row["enabled"]), "key_saved": True,
            "storage_mode": "sqlite_plaintext" if self.local_key() is not None else "keychain_encrypted"}

    def local_key(self):
        with self.store.database() as db:
            row = db.execute("SELECT api_key FROM model_local_keys WHERE capability=?", (self.capability,)).fetchone()
            return row[0] if row else None

    @staticmethod
    def validate_key(key):
        if not isinstance(key, str) or not 1 <= len(key) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ServiceError("invalid_model_key", "请输入有效访问密钥，不包含空格或换行；完整值不会回显。")
        return key

    def aad(self, row):
        return json.dumps([self.store.settings()["vault_id"], row["capability"], row["protocol"],
            row["base_url"], row["model"]], separators=(",", ":")).encode()

    def master(self, *, create=False):
        # Never create a replacement key over existing ciphertext.
        return self.keychain.master("model-master-" + self.store.settings()["vault_id"], create=create)

    def close(self):
        with self.entry_lock:
            self.closed = True
        if hasattr(self.keychain, "close"):
            self.keychain.close()

    def retry_credential_access(self):
        if hasattr(self.keychain, "retry_access"):
            self.keychain.retry_access()

    def decrypt(self, row):
        # Retain the method name for business callers, not the old storage
        # policy. Never unlock legacy credentials from a scheduled request.
        key = self.local_key()
        if key is None:
            raise ServiceError("model_storage_migration_required", "模型配置暂不可用，请重新保存访问密钥；聊天与待办保留，未发送请求。")
        return self.validate_key(key)

    def legacy_decrypt(self, row):
        from Crypto.Cipher import AES
        try:
            cipher = AES.new(self.master(), AES.MODE_GCM, nonce=row["nonce"])
            cipher.update(self.aad(row))
            return cipher.decrypt_and_verify(row["ciphertext"], row["auth_tag"]).decode("utf-8")
        except (ValueError, KeyError, UnicodeError):
            raise ServiceError("model_key_invalid", "已保存密钥无法认证解密，请重新输入；未发送请求。") from None

    def save(self, *, base_url, model, api_key=None, enabled=True, protocol="gemini"):
        if protocol not in ({"gemini", "openai"} if self.capability == "ask" else {"gemini"}) or type(enabled) is not bool:
            raise ServiceError("invalid_model_protocol", "当前文本入口只接 Gemini 原生协议；启用状态需要明确设置。")
        if protocol == "openai":
            from backend.ask_provider import openai_endpoint, openai_model
            base_url, model = openai_endpoint(base_url), openai_model(model)
        else:
            base_url, model = normalize_endpoint(base_url), model_name(model)
        if api_key is not None:
            self.validate_key(api_key)
        with self.entry_lock, self.lock, self.store.database() as db:
            old = self.row()
            if not api_key and not old:
                raise ServiceError("model_key_required", "首次配置需要访问密钥。")
            key = api_key or self.decrypt(old)
            row = {"capability": self.capability, "protocol": protocol, "base_url": base_url, "model": model,
                   "revision": self.next_revision(db, old)}
            if old and any(old[field] != row[field] for field in ("protocol", "base_url", "model")):
                self.invalidate_grant(db, "model_service_changed")
            if self.capability == "ask": self.invalidate_grant(db, "model_changed")
            # Replacement and its dependent key write commit together.
            db.execute("INSERT OR REPLACE INTO model_configs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.capability, protocol, base_url, model, int(enabled), row["revision"], b"", b"", b"",
                 "untested", None, None))
            db.execute("INSERT OR REPLACE INTO model_local_keys VALUES(?,?,1)", (self.capability, key))
            if self.capability == "text": self.store._set(db, "cloud_demo_consent", None)
        return self.public()

    def migrate(self, *, revision, confirm=False):
        if confirm is not True or type(revision) is not int or revision < 1:
            raise ServiceError("model_migration_confirmation", "请确认把旧模型密钥迁移为 SQLite 明文保存；未修改配置。")
        with self.entry_lock, self.lock:
            if self.closed:
                raise ServiceError("service_closed", "应用正在退出，未迁移密钥。")
            old = self.row()
            if not old or old["revision"] != revision:
                raise ServiceError("model_config_changed", "文本配置已变化，请刷新后重新确认。")
            if self.local_key() is not None:
                return self.public()
            self.retry_credential_access()
        # OS prompts must not hold the pause/quit/config gate. Only this
        # explicitly confirmed migration can read an old Keychain item.
        key = self.validate_key(self.legacy_decrypt(old))
        with self.entry_lock, self.lock, self.store.database() as db:
            current = self.row()
            if not current or current["revision"] != revision:
                raise ServiceError("model_config_changed", "迁移期间配置已变化，旧配置未被覆盖。")
            if self.closed:
                raise ServiceError("service_closed", "应用正在退出，未迁移密钥。")
            db.execute("INSERT INTO model_local_keys VALUES('text',?,1)", (key,))
            db.execute("UPDATE model_configs SET nonce=?,ciphertext=?,auth_tag=?,revision=? WHERE capability=?",
                (b"", b"", b"", self.next_revision(db, current), self.capability))
        if hasattr(self.keychain, "invalidate"):
            self.keychain.invalidate()
        return self.public()

    def next_revision(self, db, old):
        # A tombstone survives deletion. Never reuse an old preview/request's
        # version when the user deletes and then adds the same model again.
        previous = self.store._settings(db).get(self.capability + "_model_revision", 0)
        revision = max(previous, old["revision"] if old else 0) + 1
        self.store._set(db, self.capability + "_model_revision", revision)
        return revision

    def delete(self, *, revision, confirm=False):
        if confirm is not True or type(revision) is not int or revision < 1:
            raise ServiceError("model_delete_confirmation", "请确认删除当前文本配置；未修改数据。")
        with self.entry_lock, self.lock:
            with self.store.database() as db:
                old = self.row()
                if not old or old["revision"] != revision:
                    raise ServiceError("model_config_changed", "文本配置已变化，请刷新后重新确认；未删除配置。")
                self.next_revision(db, old)
                self.invalidate_grant(db, "model_deleted")
                db.execute("DELETE FROM model_configs WHERE capability=?", (self.capability,))
                if self.capability == "text": self.store._set(db, "cloud_demo_consent", None)
            if hasattr(self.keychain, "invalidate"):
                self.keychain.invalidate()
        # No Keychain access/deletion, no API request. Keep history and the
        # master key: it may be needed by other protected configuration.
        return {"deleted": True}

    def request(self, prompt, *, purpose, authorize, schema=None, guard=lambda: None, json_response=True):
        if authorize is not True:
            raise ServiceError("cloud_authorization_required", "请先确认本次数据及指定服务；未发送请求。")
        # Keychain interaction may require the user. Do not hold the pause/
        # shutdown gate while waiting; recheck under that gate before starting.
        with self.lock:
            config = self.row()
            if not config or not config["enabled"]:
                raise ServiceError("model_not_configured", "请先保存并启用文本模型；本地同步不受影响。")
        key = self.decrypt(config)
        with self.entry_lock, self.lock:
            current = self.row()
            if self.closed:
                raise ServiceError("service_closed", "应用正在退出，未开始新的发送。")
            if not current or not current["enabled"] or current["revision"] != config["revision"]:
                raise ServiceError("analysis_scope_changed", "模型配置已变化，未开始新的发送。")
            guard()
            call_id, started = str(uuid.uuid4()), time.monotonic()
            with self.store.database() as db:
                db.execute("INSERT INTO model_calls(call_id,capability,config_revision,purpose,started_at,outcome) VALUES(?,?,?,?,?,?)",
                    (call_id, self.capability, config["revision"], purpose, iso_time(self.clock()), "sending"))
        try:
            answer, usage = self.client.generate(config, key, prompt, schema=schema, json_response=json_response)
        except ServiceError as error:
            self.finish_call(call_id, started, "unknown" if unknown_model_result(error.code) else "failed", error.code)
            if purpose == "connection_test":
                self.test_result(config, "failed", error.code)
            raise
        except Exception:
            self.finish_call(call_id, started, "unknown", "model_result_unknown")
            raise ServiceError("model_result_unknown", "未取得可确认的请求结果，可能已计费；未保存响应正文。", retryable=True) from None
        self.finish_call(call_id, started, "success", None, usage)
        return answer, config

    def enable(self, *, enabled):
        if type(enabled) is not bool:
            raise ServiceError("invalid_switch", "请明确启用或停用。")
        with self.entry_lock, self.lock, self.store.database() as db:
            old = self.row()
            if not old:
                raise ServiceError("model_not_configured", "请先保存文本模型配置。")
            if self.capability == "ask": self.invalidate_grant(db, "model_changed")
            if hasattr(self.keychain, "invalidate"):
                self.keychain.invalidate()
            db.execute("UPDATE model_configs SET enabled=?,revision=?,test_status='untested' WHERE capability=?", (int(enabled), self.next_revision(db, old), self.capability))
            if self.capability == "text": self.store._set(db, "cloud_demo_consent", None)
        return self.public()

    def finish_call(self, call_id, started, outcome, error, usage=None):
        usage = usage or {}
        with self.store.database() as db:
            db.execute("UPDATE model_calls SET finished_at=?,outcome=?,error_code=?,input_tokens=?,output_tokens=?,total_tokens=?,duration_ms=? WHERE call_id=?",
                (iso_time(self.clock()), outcome, error, usage.get("input_tokens"), usage.get("output_tokens"),
                 usage.get("total_tokens"), round((time.monotonic()-started)*1000, 3), call_id))

    def test_result(self, config, status, error=None):
        with self.store.database() as db:
            db.execute("UPDATE model_configs SET test_status=?,tested_at=?,test_error=? WHERE capability=? AND revision=?",
                (status, iso_time(self.clock()), error, self.capability, config["revision"]))

    def test(self, *, authorize=False):
        answer, config = self.request('请只回复：Hello from AIGoCode!',
            purpose="connection_test", authorize=authorize, json_response=False)
        if not isinstance(answer, str) or answer.strip() != "Hello from AIGoCode!":
            self.test_result(config, "failed", "connection_test_invalid")
            raise ServiceError("connection_test_invalid", "服务响应成功，但未通过最小能力检查；请核对模型。")
        self.test_result(config, "passed")
        return self.public()

    def usage(self):
        # Local-time periods; absent token counts remain unknown, not zero.
        from datetime import datetime
        now = datetime.fromtimestamp(self.clock()).astimezone()
        start_today = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        start_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        result = {}
        with self.store.database() as db:
            for period, start in (("today", start_today), ("month", start_month)):
                rows = db.execute("SELECT * FROM model_calls WHERE capability=? AND julianday(started_at)>=julianday(?) AND julianday(started_at)<=julianday(?)", (self.capability, start, now.isoformat())).fetchall()
                result[period] = {"requests": len(rows), "success": sum(r["outcome"] == "success" for r in rows),
                    "failed": sum(r["outcome"] == "failed" for r in rows),
                    "unknown": sum(r["outcome"] == "unknown" for r in rows),
                    "sending": sum(r["outcome"] == "sending" for r in rows),
                    "failed_or_unknown": sum(r["outcome"] in {"failed", "unknown"} for r in rows)}
                for kind in ("input", "output", "total"):
                    known = [r[kind + "_tokens"] for r in rows if r[kind + "_tokens"] is not None]
                    result[period]["known_" + kind + "_tokens"] = sum(known) if known else None
                    result[period]["unknown_" + kind + "_requests"] = len(rows) - len(known)
                result[period]["unknown_usage_requests"] = result[period]["unknown_total_requests"]
                if self.capability == "ask":
                    groups = []
                    for revision in sorted({r["config_revision"] for r in rows}):
                        calls = [r for r in rows if r["config_revision"] == revision]
                        group = {"config_revision": revision, "requests": len(calls)}
                        for kind in ("input", "output", "total"):
                            known = [r[kind+"_tokens"] for r in calls if r[kind+"_tokens"] is not None]
                            group["known_"+kind+"_tokens"] = sum(known) if known else None
                            group["unknown_"+kind+"_requests"] = len(calls)-len(known)
                        groups.append(group)
                    result[period]["services"] = groups
                    if len(groups) > 1:
                        for kind in ("input", "output", "total"):
                            result[period]["known_"+kind+"_tokens"] = None

        return result

    def calls(self, period="today", limit=25, offset=0):
        from datetime import datetime
        if period not in {"today", "month"} or type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ServiceError("invalid_usage_page", "请求记录的时间范围或分页无效。")
        now = datetime.fromtimestamp(self.clock()).astimezone()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "month": start = start.replace(day=1)
        where = "capability=? AND julianday(started_at)>=julianday(?) AND julianday(started_at)<=julianday(?)"
        args = (self.capability, start.isoformat(), now.isoformat())
        with self.store.database() as db:
            total = db.execute("SELECT COUNT(*) FROM model_calls WHERE "+where, args).fetchone()[0]
            rows = db.execute("SELECT call_id,purpose,started_at,finished_at,outcome,input_tokens,output_tokens,total_tokens FROM model_calls WHERE "+where+
                " ORDER BY julianday(started_at) DESC,call_id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            return dict(items=[dict(row) for row in rows], total=total, offset=offset, has_more=offset+len(rows)<total)
