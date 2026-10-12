"""Durable, explicitly granted text batches; never grants by sync/model setup."""

import json
import sqlite3
import threading
import uuid

from backend.contracts import ServiceError, iso_time, parse_time, stable_id, unknown_model_result
from backend.demo import ACCOUNT
from backend.message_identity import identified


class AutomaticText:
    QUIET_SECONDS = 600
    MAX_WAIT_SECONDS = 1800

    def __init__(self, service):
        self.service, self.store = service, service.store
        self.previews = {}
        self.lock = threading.Lock()

    def scope(self, db):
        settings = self.store._settings(db)
        source = settings["source"]
        if not source or source.get("mode") not in {"local", "encrypted"} or source.get("account_id") == ACCOUNT:
            raise ServiceError("real_source_required", "请先连接本地来源，未授权发送。")
        config = db.execute("SELECT protocol,base_url,model FROM model_configs WHERE capability='text'").fetchone()
        if not config:
            raise ServiceError("model_not_configured", "请先配置文本服务，保存配置不会授权发送。")
        chats = db.execute("SELECT chat_id,sync_enabled FROM chats WHERE account_id=? AND analysis_enabled=1 ORDER BY chat_id", (source["account_id"],)).fetchall()
        if not chats or any(not row["sync_enabled"] for row in chats):
            raise ServiceError("analysis_chat_scope", "请选择需要自动分析的会话，并开启这些会话的本地同步。")
        return {"account_id": source["account_id"], "source_hash": stable_id(source),
            "chat_ids": [r["chat_id"] for r in chats], "time_start": iso_time(parse_time(settings["time_start"])),
            "message_types": ["text"], "protocol": config["protocol"], "base_url": config["base_url"], "model": config["model"]}

    def active(self, db):
        row = db.execute("SELECT * FROM text_grants WHERE status='active'").fetchone()
        if row:
            try:
                valid = row["scope_hash"] == stable_id(self.scope(db))
            except ServiceError:
                valid = False
            if not valid:
                self.store.invalidate_text_grant(db, "scope_changed")
                return None
        return dict(row) if row else None

    def preview(self):
        with self.service.guard, self.store.database() as db:
            self.service.ensure_open()
            scope = self.scope(db)
            token = str(uuid.uuid4())
            self.previews = {token: (stable_id(scope), self.service.clock() + 900)}
            chats = [dict(r) for r in db.execute("""SELECT chat_id,COALESCE(display_name,chat_name) AS name,chat_type
                FROM chats WHERE account_id=? AND analysis_enabled=1 ORDER BY chat_id""", (scope["account_id"],))]
            rows = self.available_rows(db, scope)
            source = self.store._settings(db)["source"]
        return scope | {"chats": chats, "preview_token": token,
            **self.service.analysis.build_preview(identified(rows, source)), "continuous": True,
            "warning": "所选会话从读取起点起的文字及后续新增文字将持续发送到上述服务；必要时使用同一授权范围内该会话已处理文字理解后续进展，不为旧上下文单独新建待办。长会话可能逐段请求并再次整体核对，每次请求均可能计费。自动脱敏可能漏检，服务商可能留存内容并产生费用；范围或服务改变后需重新授权。"}

    def save(self, *, authorize=False, preview_token=None):
        if authorize is not True:
            raise ServiceError("cloud_authorization_required", "请确认会话、时间、文字类型和接收服务，未授权发送。")
        with self.service.guard, self.store.database() as db:
            self.service.ensure_open()
            db.execute("BEGIN IMMEDIATE")
            scope = self.scope(db)
            preview = self.previews.get(preview_token) if isinstance(preview_token, str) else None
            if not preview or preview[0] != stable_id(scope) or self.service.clock() > preview[1]:
                raise ServiceError("authorization_preview_changed", "授权范围已变化、过期或未预览，请重新核对后确认。")
            self.previews.clear()
            self.store.invalidate_text_grant(db, "grant_replaced")
            grant = str(uuid.uuid4())
            db.execute("INSERT INTO text_grants VALUES(?,?,?,?,?,?,?)", (grant, json.dumps(scope), stable_id(scope),
                "active", iso_time(self.service.clock()), None, None))
            self.capture(db, dict(db.execute("SELECT * FROM text_grants WHERE grant_id=?", (grant,)).fetchone()))
        return self.public()

    def revoke(self):
        with self.service.guard, self.store.database() as db:
            self.service.ensure_open()
            db.execute("UPDATE text_grants SET status='revoked',ended_at=?,reason='user_revoked' WHERE status='active'", (iso_time(self.service.clock()),))
            db.execute("""UPDATE text_batches SET status='cancelled',error_code='user_revoked' WHERE status='queued'
                AND grant_id IN (SELECT grant_id FROM text_grants WHERE status='revoked')""")
            self.previews.clear()
        return self.public()

    def public(self):
        with self.store.database() as db:
            active = self.active(db)
            recent = active or db.execute("SELECT * FROM text_grants ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
            settings = self.store._settings(db)
            account = settings["source"]["account_id"] if settings["source"] else ""
            batches = [dict(r) | {"message_count": len(json.loads(r["message_ids"])),
                "retryable": bool(active and r["scope_hash"] == active["scope_hash"] and r["status"] in {"failed", "unknown"})}
                for r in db.execute("""SELECT b.batch_id,b.status,b.created_at,b.finished_at,b.error_code,b.grant_id,b.message_ids,g.scope_hash,
                COALESCE(c.display_name,c.chat_name,'来源会话') AS chat_name FROM text_batches b
                JOIN text_grants g ON g.grant_id=b.grant_id
                LEFT JOIN chats c ON c.account_id=b.account_id AND c.chat_id=b.chat_id
                WHERE b.account_id=? ORDER BY b.created_at DESC,b.rowid DESC LIMIT 20""", (account,))]
            for batch in batches:
                batch['current_scope'] = bool(active and batch['grant_id'] == active['grant_id'])
                del batch["message_ids"], batch["grant_id"], batch["scope_hash"]
            pending = db.execute("""SELECT COUNT(*) FROM text_pending p LEFT JOIN text_batches b USING(batch_id)
                WHERE p.grant_id=? AND (p.batch_id IS NULL OR b.status='queued')""", (active["grant_id"] if active else "",)).fetchone()[0]
            counts = dict(db.execute('SELECT status,COUNT(*) FROM text_batches WHERE grant_id=? GROUP BY status',
                (active['grant_id'] if active else '',)).fetchall())
        return {"grant": {"status": recent["status"], "scope": json.loads(recent["scope_json"]),
            "created_at": recent["created_at"], "reason": recent["reason"]} if recent else None,
            "pending_messages": pending, "running_batches": counts.get('running', 0),
            "queued_batches": counts.get('queued', 0), "batches": batches, "quiet_seconds": self.QUIET_SECONDS,
            "max_wait_seconds": self.MAX_WAIT_SECONDS}

    def available_rows(self, db, scope):
        # Completed/failed/unknown submissions must not be silently resent when
        # a new grant is saved. Explicit batch retry is a separate action.
        used = set()
        for row in db.execute("SELECT message_ids FROM analysis_runs WHERE account_id=? AND status IN ('running','success','failed')", (scope["account_id"],)):
            used.update(json.loads(row[0]))
        for row in db.execute("SELECT message_ids FROM text_batches WHERE account_id=? AND status!='cancelled'", (scope["account_id"],)):
            used.update(json.loads(row[0]))
        selected = set(scope["chat_ids"])
        return [dict(r) for r in db.execute("""SELECT * FROM messages WHERE account_id=? AND message_type='text'
            AND content_state='text_available' AND analysis_eligible=1 AND sent_epoch>=? AND sent_epoch<?
            ORDER BY sent_epoch,message_id""", (scope["account_id"], parse_time(scope["time_start"]), self.service.clock()))
            if r["chat_id"] in selected and r["message_id"] not in used]

    def capture(self, db, grant):
        scope = json.loads(grant["scope_json"])
        rows = self.available_rows(db, scope)
        ids = {r["message_id"] for r in rows}
        waiting = db.execute("SELECT message_id FROM text_pending WHERE grant_id=? AND batch_id IS NULL", (grant["grant_id"],)).fetchall()
        # Only queue metadata is removed here, never a source message or result.
        db.executemany("DELETE FROM text_pending WHERE message_id=?", [(r[0],) for r in waiting if r[0] not in ids])
        for row in rows:
            arrival = db.execute("SELECT arrived_epoch FROM message_arrivals WHERE message_id=?", (row["message_id"],)).fetchone()
            if not arrival:
                db.execute("INSERT INTO message_arrivals VALUES(?,?)", (row["message_id"], self.service.clock()))
            # A grant created now gets a fresh initial window even if the
            # stored message originally arrived weeks ago.
            arrived = max(arrival[0] if arrival else self.service.clock(), parse_time(grant["created_at"]))
            db.execute("""INSERT INTO text_pending VALUES(?,?,?,?,?,NULL)
                ON CONFLICT(message_id) DO UPDATE SET grant_id=excluded.grant_id,arrived_epoch=excluded.arrived_epoch,batch_id=NULL
                WHERE text_pending.grant_id!=excluded.grant_id""",
                (row["message_id"], grant["grant_id"], scope["account_id"], row["chat_id"], arrived))

    @staticmethod
    def input_hash(rows, *, legacy=False):
        fields = [(r["message_id"], r["content"], r["sent_at"], r["is_self"], r["message_type"], r["analysis_eligible"]) for r in rows]
        if not legacy:
            fields = [item + (r.get("sender_id"), r.get("account_id"), r.get("chat_id")) for item, r in zip(fields, rows)]
        return stable_id(fields)

    def batch_rows(self, db, batch):
        manifest = db.execute("SELECT policy_version FROM text_batch_contexts WHERE batch_id=?", (batch["batch_id"],)).fetchone()
        if not manifest or manifest[0] not in {0, 1}:
            raise ServiceError("batch_context_changed", "本批次上下文记录不可用，未发送或扩大范围。")
        ids = json.loads(batch["message_ids"])
        rows = []
        for message_id in ids:
            row = db.execute("SELECT * FROM messages WHERE message_id=? AND account_id=? AND chat_id=?", (message_id, batch["account_id"], batch["chat_id"])).fetchone()
            if not row or row["message_type"] != "text" or row["content_state"] != "text_available" or not row["analysis_eligible"]:
                raise ServiceError("batch_source_changed", "本批次来源文字已变化，未扩大范围发送。")
            rows.append(dict(row))
        if not rows or self.input_hash(rows, legacy=manifest[0] == 0) != batch["input_hash"]:
            raise ServiceError("batch_source_changed", "本批次来源文字已变化，未扩大范围发送。")
        return rows

    def context_candidates(self, db, scope, chat, fresh_ids):
        processed = set()
        for run in db.execute("SELECT message_ids FROM analysis_runs WHERE account_id=? AND status='success'", (scope["account_id"],)):
            processed.update(json.loads(run[0]))
        return [dict(r) for r in db.execute("""SELECT * FROM messages WHERE account_id=? AND chat_id=?
            AND message_type='text' AND content_state='text_available' AND analysis_eligible=1
            AND sent_epoch>=? AND sent_epoch<? ORDER BY sent_epoch,message_id""",
            (scope["account_id"], chat, parse_time(scope["time_start"]), self.service.clock()))
            if r["message_id"] in processed and r["message_id"] not in fresh_ids]

    def context_rows(self, db, batch):
        manifest = db.execute("SELECT * FROM text_batch_contexts WHERE batch_id=?", (batch["batch_id"],)).fetchone()
        grant = db.execute("SELECT scope_json FROM text_grants WHERE grant_id=?", (batch["grant_id"],)).fetchone()
        if not manifest or not grant:
            raise ServiceError("batch_context_changed", "本批次上下文记录不可用，未发送或扩大范围。")
        try:
            ids = json.loads(manifest["message_ids"])
            scope = json.loads(grant[0])
            if not isinstance(ids, list) or any(not isinstance(mid, str) for mid in ids) or len(ids) != len(set(ids)):
                raise ValueError()
            if manifest["policy_version"] not in {0, 1} or (manifest["policy_version"] == 0 and ids):
                raise ValueError()
            fresh = set(json.loads(batch["message_ids"]))
            if fresh.intersection(ids) or batch["chat_id"] not in scope["chat_ids"]:
                raise ValueError()
            rows = []
            for mid in ids:
                row = db.execute("SELECT * FROM messages WHERE message_id=? AND account_id=? AND chat_id=?",
                    (mid, batch["account_id"], batch["chat_id"])).fetchone()
                if (not row or row["message_type"] != "text" or row["content_state"] != "text_available"
                        or not row["analysis_eligible"] or not parse_time(scope["time_start"]) <= row["sent_epoch"] < parse_time(batch["created_at"])):
                    raise ValueError()
                rows.append(dict(row))
            if self.input_hash(rows) != manifest["input_hash"]:
                raise ValueError()
            return rows
        except (ValueError, TypeError, KeyError):
            raise ServiceError("batch_context_changed", "本批次上下文已变化或超出范围，未发送或扩大范围。") from None

    def freeze(self, db, grant):
        scope = json.loads(grant["scope_json"])
        for chat in scope["chat_ids"]:
            pending = db.execute("""SELECT p.*,m.sent_epoch FROM text_pending p JOIN messages m USING(message_id)
                WHERE p.grant_id=? AND p.chat_id=? AND p.batch_id IS NULL ORDER BY m.sent_epoch,p.message_id""", (grant["grant_id"], chat)).fetchall()
            if not pending:
                continue
            first = min(r["arrived_epoch"] for r in pending)
            latest = max(r["arrived_epoch"] for r in pending)
            activity = db.execute("""SELECT MAX(a.arrived_epoch) FROM message_arrivals a JOIN messages m USING(message_id)
                WHERE m.account_id=? AND m.chat_id=? AND m.sent_epoch>=? AND m.sent_epoch<?""",
                (scope["account_id"], chat, parse_time(scope["time_start"]), self.service.clock())).fetchone()[0]
            latest = max(latest, activity or latest)
            if self.service.clock() < latest + self.QUIET_SECONDS and self.service.clock() < first + self.MAX_WAIT_SECONDS:
                continue
            ids = [r["message_id"] for r in pending]
            batch_id = stable_id(grant["grant_id"], ids)
            rows = [dict(db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone()) for mid in ids]
            db.execute("INSERT INTO text_batches VALUES(?,?,?,?,?,?,?,?,?,?)", (batch_id, grant["grant_id"],
                scope["account_id"], chat, json.dumps(ids), self.input_hash(rows), "queued", iso_time(self.service.clock()), None, None))
            context = self.context_candidates(db, scope, chat, set(ids))
            db.execute("INSERT INTO text_batch_contexts VALUES(?,?,?,1)", (batch_id,
                json.dumps([r["message_id"] for r in context]), self.input_hash(context)))
            db.executemany("UPDATE text_pending SET batch_id=? WHERE message_id=? AND grant_id=?", [(batch_id, mid, grant["grant_id"]) for mid in ids])

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return {"status": "busy"}
        analysis = self.service.analysis
        acquired = False
        try:
            self.service.ensure_open()
            with self.service.guard, self.store.database() as db:
                db.execute("BEGIN IMMEDIATE")
                grant = self.active(db)
                config = db.execute("SELECT * FROM model_configs WHERE capability='text'").fetchone()
                if not grant or self.store._settings(db)["paused"] or not config or not config["enabled"]:
                    return {"status": "inactive"}
                self.capture(db, grant)
                self.freeze(db, grant)
                queued = db.execute("SELECT * FROM text_batches WHERE grant_id=? AND status='queued' ORDER BY created_at,rowid LIMIT 1", (grant["grant_id"],)).fetchone()
                if not queued:
                    return {"status": "waiting"}
                acquired = analysis.lock.acquire(blocking=False)
                if not acquired:
                    return {"status": "busy"}
                batch = dict(queued)
                try:
                    rows = self.batch_rows(db, batch)
                    context = self.context_rows(db, batch)
                except ServiceError as error:
                    db.execute("UPDATE text_batches SET status='failed',finished_at=?,error_code=? WHERE batch_id=?",
                        (iso_time(self.service.clock()), error.code, batch["batch_id"]))
                    return {"status": "failed", "error_code": error.code}
                generation = self.service.analysis_generation
                revision = config["revision"]
                db.execute("UPDATE text_batches SET status='running',error_code=NULL WHERE batch_id=?", (batch["batch_id"],))
                db.execute("""INSERT INTO analysis_runs VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET
                    message_ids=excluded.message_ids,source_revision=excluded.source_revision,
                    config_revision=excluded.config_revision,status=excluded.status,created_at=excluded.created_at,
                    finished_at=NULL,error_code=NULL""", (batch["batch_id"], batch["account_id"],
                    batch["message_ids"], self.store._settings(db)["revision"], revision, "running", iso_time(self.service.clock()), None, None))
            return self.process(batch, rows, generation, revision, context)
        finally:
            if acquired:
                analysis.lock.release()
            self.lock.release()

    def process(self, batch, rows, generation, revision, context):
        started = False

        def guard():
            self.service.ensure_open()
            with self.store.database() as db:
                current = self.active(db)
                config = db.execute("SELECT enabled,revision FROM model_configs WHERE capability='text'").fetchone()
                if not current or current["grant_id"] != batch["grant_id"]:
                    raise ServiceError("analysis_scope_changed", "授权或范围已改变，本轮结果不发布。")
                if self.store._settings(db)["paused"] or self.service.analysis_generation != generation:
                    raise ServiceError("sync_paused", "同步已暂停，本轮结果不发布。")
                if not config or not config["enabled"] or config["revision"] != revision:
                    raise ServiceError("analysis_scope_changed", "模型配置已改变，本轮结果不发布。")
                self.batch_rows(db, batch)
                self.context_rows(db, batch)

        def send_guard():
            nonlocal started
            guard()
            started = True

        try:
            results = self.service.analysis.candidates(rows, purpose="automatic_text", guard=send_guard,
                check_guard=guard, context_rows=context, batch_id=batch["batch_id"],
                source=self.store.settings()["source"], include_attention=True)
            candidates = results["tasks"]
            with self.service.guard, self.service.models.lock, self.store.database() as db:
                guard()
                self.service.analysis.publish(db, batch_id=batch["batch_id"], account=batch["account_id"], candidates=candidates,
                    attention_items=results["attention_items"], attention_rejections=results["attention_rejections"])
                db.execute("UPDATE text_batches SET status='success',finished_at=?,error_code=NULL WHERE batch_id=?", (iso_time(self.service.clock()), batch["batch_id"]))
            return {"status": "success", "task_count": len(candidates)}
        except (ServiceError, sqlite3.Error) as error:
            code = error.code if isinstance(error, ServiceError) else "storage_failed"
            status = "unknown" if unknown_model_result(code) else "failed"
            with self.store.database() as db:
                grant = self.active(db)
                if not started:
                    if not grant or grant["grant_id"] != batch["grant_id"]:
                        status = "cancelled"
                    elif code in {"sync_paused", "service_closed", "analysis_scope_changed"}:
                        status = "queued"
                db.execute("UPDATE text_batches SET status=?,finished_at=?,error_code=? WHERE batch_id=?", (status, iso_time(self.service.clock()), code, batch["batch_id"]))
                db.execute("UPDATE analysis_runs SET status=?,finished_at=?,error_code=? WHERE batch_id=? AND status!='success'", ("cancelled" if not started else "failed", iso_time(self.service.clock()), code, batch["batch_id"]))
            return {"status": status, "error_code": code}

    def retry(self, *, batch_id, authorize=False):
        if authorize is not True:
            raise ServiceError("cloud_authorization_required", "再次发送可能产生重复费用，请先确认重试。")
        self.store.identifier(batch_id)
        with self.service.guard, self.store.database() as db:
            self.service.ensure_open()
            grant = self.active(db)
            batch = db.execute("SELECT b.*,g.scope_hash FROM text_batches b JOIN text_grants g ON g.grant_id=b.grant_id WHERE batch_id=?", (batch_id,)).fetchone()
            if not grant or not batch or batch["scope_hash"] != grant["scope_hash"] or batch["status"] not in {"failed", "unknown"}:
                raise ServiceError("batch_retry_scope", "该批次不能在当前授权范围下重试，请核对范围。")
            self.batch_rows(db, batch)
            self.context_rows(db, batch)
            self.service.models.retry_credential_access()
            db.execute("UPDATE text_batches SET grant_id=?,status='queued',finished_at=NULL,error_code=NULL WHERE batch_id=?", (grant["grant_id"], batch_id))
            db.execute("UPDATE text_pending SET grant_id=? WHERE batch_id=?", (grant["grant_id"], batch_id))
        return self.public()

    def recover(self):
        with self.store.database() as db:
            db.execute("UPDATE text_batches SET status='unknown',error_code='interrupted',finished_at=? WHERE status='running'", (iso_time(self.service.clock()),))
