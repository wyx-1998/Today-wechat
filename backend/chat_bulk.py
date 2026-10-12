"""Frozen, local-only configuration previews. Never authorize or send content."""
import hashlib
import json
import secrets
import sqlite3

from backend.contracts import ServiceError


class ChatBulk:
    OPERATIONS = {"sync_on", "sync_off", "analysis_on", "analysis_off", "tags_add", "tags_remove"}

    def __init__(self, service):
        self.service = service
        self.previews = {}

    def _snapshot(self, db):
        state = self.service.store._settings(db)
        if not state["source"]:
            raise ServiceError("not_connected", "请先连接本地来源。")
        account = state["source"]["account_id"]
        chats = {r["chat_id"]: dict(r) for r in db.execute("SELECT * FROM chats WHERE account_id=?", (account,))}
        links = {}
        for r in db.execute("SELECT chat_id,tag_id FROM chat_tags WHERE account_id=? ORDER BY tag_id", (account,)):
            links.setdefault(r[0], []).append(r[1])
        tags = [dict(r) for r in db.execute("SELECT * FROM tags ORDER BY tag_id")]
        # No secret-bearing model fields enter the fingerprint or response.
        models = [tuple(r) for r in db.execute("SELECT capability,protocol,base_url,model,enabled,revision FROM model_configs ORDER BY capability")]
        value = [state["source"], state["time_start"], state["revision"], state["paused"], chats, links, tags, models]
        fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return account, chats, links, {t["tag_id"] for t in tags}, fingerprint

    @staticmethod
    def plan(chat, previous_tags, operation, tag_ids):
        if chat is None:
            return "unavailable", None
        sync, analysis = bool(chat["sync_enabled"]), bool(chat["analysis_enabled"])
        tags = set(previous_tags)
        before = sync, analysis, tags.copy()
        if operation == "sync_on": sync = True
        elif operation == "sync_off": sync, analysis = False, False
        elif operation == "analysis_on":
            if not sync: return "unavailable", None
            analysis = True
        elif operation == "analysis_off": analysis = False
        elif operation == "tags_add": tags.update(tag_ids)
        elif operation == "tags_remove": tags.difference_update(tag_ids)
        return ("unchanged" if before == (sync, analysis, tags) else "change"), (sync, analysis, tags)

    def preview(self, chat_ids, operation, tag_ids=None):
        tag_ids = [] if tag_ids is None else tag_ids
        if (not isinstance(chat_ids, list) or not chat_ids or any(not isinstance(v, str) or not v for v in chat_ids)
                or len(set(chat_ids)) != len(chat_ids) or not isinstance(operation, str) or operation not in self.OPERATIONS
                or not isinstance(tag_ids, list) or any(not isinstance(v, str) for v in tag_ids) or len(set(tag_ids)) != len(tag_ids)
                or (operation.startswith("tags_") and not tag_ids) or (not operation.startswith("tags_") and tag_ids)):
            raise ServiceError("invalid_bulk", "请选择会话及一种有效的批量操作。")
        with self.service.guard, self.service.store.database() as db:
            db.execute("BEGIN")
            account, chats, links, known_tags, fingerprint = self._snapshot(db)
            if not set(tag_ids) <= known_tags:
                raise ServiceError("tag_missing", "标签已变化，请重新选择。")
            counts = {"change": 0, "unchanged": 0, "unavailable": 0}
            for cid in chat_ids:
                category, _ = self.plan(chats.get(cid), links.get(cid, []), operation, tag_ids)
                counts[category] += 1
            now = self.service.clock()
            self.previews = {k: v for k, v in self.previews.items() if v["expires"] > now}
            token = secrets.token_urlsafe(32)
            self.previews[token] = dict(account=account, fingerprint=fingerprint, expires=now+300,
                                        chat_ids=chat_ids.copy(), operation=operation, tag_ids=tag_ids.copy())
            return dict(preview_token=token, selected=len(chat_ids), operation=operation, **counts)

    def apply(self, preview_token, confirm=False):
        if confirm is not True:
            raise ServiceError("confirmation_required", "请先核对批量配置的影响并确认。")
        with self.service.guard, self.service.store.database() as db:
            # Keep all savepoints inside one outer transaction. Releasing the
            # first savepoint must not prematurely commit the first chat.
            db.execute("BEGIN IMMEDIATE")
            preview = self.previews.pop(preview_token, None) if isinstance(preview_token, str) else None
            if not preview or preview["expires"] <= self.service.clock():
                raise ServiceError("bulk_preview_expired", "预览已过期，请重新预览；尚未保存。")
            account, chats, links, _, fingerprint = self._snapshot(db)
            if account != preview["account"] or fingerprint != preview["fingerprint"]:
                raise ServiceError("bulk_preview_changed", "会话、标签或配置已变化，请重新预览；尚未保存。")
            result = dict(changed=0, unchanged=0, unavailable=0, failed=0, failed_ids=[], unavailable_ids=[])
            scope_changed = False
            for cid in preview["chat_ids"]:
                category, proposed = self.plan(chats.get(cid), links.get(cid, []), preview["operation"], preview["tag_ids"])
                if category != "change":
                    key = "unchanged" if category == "unchanged" else "unavailable"
                    result[key] += 1
                    if key == "unavailable": result["unavailable_ids"].append(cid)
                    continue
                sync, analysis, tags = proposed
                db.execute("SAVEPOINT bulk_chat")
                try:
                    db.execute("UPDATE chats SET sync_enabled=?,analysis_enabled=? WHERE account_id=? AND chat_id=?", (int(sync), int(analysis), account, cid))
                    db.execute("DELETE FROM chat_tags WHERE account_id=? AND chat_id=?", (account, cid))
                    db.executemany("INSERT INTO chat_tags VALUES(?,?,?)", [(account, cid, tag) for tag in sorted(tags)])
                except sqlite3.Error:
                    db.execute("ROLLBACK TO bulk_chat")
                    result["failed"] += 1
                    result["failed_ids"].append(cid)
                else:
                    result["changed"] += 1
                    old = chats[cid]
                    scope_changed |= bool(old["analysis_enabled"] or analysis) and (bool(old["sync_enabled"]), bool(old["analysis_enabled"])) != (sync, analysis)
                finally:
                    db.execute("RELEASE bulk_chat")
            if result["changed"]:
                if scope_changed: self.service.store.invalidate_text_grant(db, "chat_scope_changed")
                self.service.store._set(db, "revision", self.service.store._settings(db)["revision"]+1)
            return result
