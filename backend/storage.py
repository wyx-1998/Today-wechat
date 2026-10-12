"""Versioned application SQLite storage with one controlled writer."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from backend.contracts import DISCOVERY_REJECTION_RULES, ServiceError, iso_time, parse_time, stable_id
from backend.message_identity import identified


SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE chats(
 account_id TEXT NOT NULL,chat_id TEXT NOT NULL,chat_name TEXT NOT NULL,display_name TEXT,
 chat_type TEXT NOT NULL,sync_enabled INTEGER NOT NULL DEFAULT 0,analysis_enabled INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(account_id,chat_id));
CREATE TABLE tags(tag_id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE);
CREATE TABLE chat_tags(account_id TEXT NOT NULL,chat_id TEXT NOT NULL,tag_id TEXT NOT NULL REFERENCES tags(tag_id) ON DELETE CASCADE,
 PRIMARY KEY(account_id,chat_id,tag_id),FOREIGN KEY(account_id,chat_id) REFERENCES chats(account_id,chat_id));
CREATE TABLE messages(
 message_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,chat_id TEXT NOT NULL,stream_id TEXT NOT NULL,
 local_id TEXT NOT NULL,sender_id TEXT,sender_name TEXT,is_self INTEGER,sent_at TEXT NOT NULL,sent_epoch REAL NOT NULL,
 message_type TEXT NOT NULL,content TEXT,content_state TEXT NOT NULL,analysis_eligible INTEGER NOT NULL);
CREATE INDEX message_chat_time ON messages(account_id,chat_id,sent_epoch DESC,message_id);
CREATE TABLE cursors(account_id TEXT NOT NULL,chat_id TEXT NOT NULL,stream_id TEXT NOT NULL,range_key TEXT NOT NULL,
 local_id TEXT NOT NULL,PRIMARY KEY(account_id,chat_id,stream_id,range_key));
CREATE TABLE sync_runs(run_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,started_at TEXT NOT NULL,
 finished_at TEXT,outcome TEXT NOT NULL,report_json TEXT NOT NULL);
PRAGMA user_version=1;
COMMIT;
"""

MODEL_MIGRATION = """
BEGIN IMMEDIATE;
CREATE TABLE model_configs(capability TEXT PRIMARY KEY, protocol TEXT NOT NULL, base_url TEXT NOT NULL,
 model TEXT NOT NULL, enabled INTEGER NOT NULL, revision INTEGER NOT NULL,
 nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, auth_tag BLOB NOT NULL,
 test_status TEXT NOT NULL, tested_at TEXT, test_error TEXT);
CREATE TABLE model_calls(call_id TEXT PRIMARY KEY, capability TEXT NOT NULL, config_revision INTEGER NOT NULL,
 purpose TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT NOT NULL,
 error_code TEXT, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, duration_ms REAL);
CREATE TABLE analysis_runs(batch_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, message_ids TEXT NOT NULL,
 source_revision INTEGER NOT NULL, config_revision INTEGER NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, finished_at TEXT, error_code TEXT);
CREATE TABLE tasks(task_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, batch_id TEXT NOT NULL,
 title TEXT NOT NULL, owner_type TEXT NOT NULL, owner_id TEXT, due_at TEXT, due_precision TEXT NOT NULL,
 status TEXT NOT NULL, source_message_ids TEXT NOT NULL, created_at TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
CREATE INDEX task_account ON tasks(account_id, created_at);
PRAGMA user_version=2;
COMMIT;
"""

AUTOMATIC_MIGRATION = """
BEGIN IMMEDIATE;
CREATE TABLE text_grants(grant_id TEXT PRIMARY KEY,scope_json TEXT NOT NULL,scope_hash TEXT NOT NULL,
 status TEXT NOT NULL,created_at TEXT NOT NULL,ended_at TEXT,reason TEXT);
CREATE UNIQUE INDEX text_grant_active ON text_grants(status) WHERE status='active';
CREATE TABLE message_arrivals(message_id TEXT PRIMARY KEY REFERENCES messages(message_id) ON DELETE CASCADE,
 arrived_epoch REAL NOT NULL);
CREATE TABLE text_pending(message_id TEXT PRIMARY KEY REFERENCES messages(message_id) ON DELETE CASCADE,
 grant_id TEXT NOT NULL REFERENCES text_grants(grant_id),account_id TEXT NOT NULL,chat_id TEXT NOT NULL,
 arrived_epoch REAL NOT NULL,batch_id TEXT);
CREATE INDEX text_pending_chat ON text_pending(grant_id,chat_id,batch_id,arrived_epoch);
CREATE TABLE text_batches(batch_id TEXT PRIMARY KEY,grant_id TEXT NOT NULL REFERENCES text_grants(grant_id),
 account_id TEXT NOT NULL,chat_id TEXT NOT NULL,message_ids TEXT NOT NULL,input_hash TEXT NOT NULL,
 status TEXT NOT NULL,created_at TEXT NOT NULL,finished_at TEXT,error_code TEXT);
CREATE INDEX text_batches_status ON text_batches(status,created_at);
PRAGMA user_version=3;
COMMIT;
"""

CONTEXT_MIGRATION = f"""
BEGIN IMMEDIATE;
CREATE TABLE text_batch_contexts(batch_id TEXT PRIMARY KEY REFERENCES text_batches(batch_id),
 message_ids TEXT NOT NULL,input_hash TEXT NOT NULL,policy_version INTEGER NOT NULL);
INSERT INTO text_batch_contexts SELECT batch_id,'[]','{stable_id([])}',0 FROM text_batches;
PRAGMA user_version=4;
COMMIT;
"""

REVIEW_MIGRATION = """
BEGIN IMMEDIATE;
CREATE TABLE review_runs(batch_id TEXT PRIMARY KEY REFERENCES analysis_runs(batch_id),
 chat_id TEXT NOT NULL,time_start TEXT NOT NULL,time_end TEXT NOT NULL,input_hash TEXT NOT NULL,
 policy_version INTEGER NOT NULL);
CREATE TABLE task_evidence_links(task_id TEXT NOT NULL REFERENCES tasks(task_id),
 batch_id TEXT NOT NULL REFERENCES analysis_runs(batch_id),source_message_ids TEXT NOT NULL,
 PRIMARY KEY(task_id,batch_id));
INSERT INTO task_evidence_links SELECT t.task_id,t.batch_id,t.source_message_ids FROM tasks t
 JOIN analysis_runs r ON r.batch_id=t.batch_id AND r.account_id=t.account_id;
CREATE TABLE task_changes(change_id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(task_id),
 batch_id TEXT NOT NULL REFERENCES analysis_runs(batch_id),kind TEXT NOT NULL,proposed TEXT NOT NULL,
 source_message_ids TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,resolved_at TEXT);
CREATE INDEX task_changes_pending ON task_changes(task_id,status);
PRAGMA user_version=5;
COMMIT;
"""

DISCOVERY_MIGRATION = """
BEGIN IMMEDIATE;
CREATE TABLE discovery_attempts(attempt_id TEXT PRIMARY KEY,
 batch_id TEXT NOT NULL REFERENCES analysis_runs(batch_id),created_at TEXT NOT NULL,policy_version INTEGER NOT NULL);
CREATE TABLE discovery_windows(attempt_id TEXT NOT NULL REFERENCES discovery_attempts(attempt_id) ON DELETE CASCADE,
 window_index INTEGER NOT NULL,focal_message_ids TEXT NOT NULL,context_message_ids TEXT NOT NULL,
 candidate_count INTEGER NOT NULL,PRIMARY KEY(attempt_id,window_index));
CREATE TABLE discovery_candidates(attempt_id TEXT NOT NULL,candidate_id TEXT NOT NULL,window_index INTEGER NOT NULL,
 title TEXT NOT NULL,owner_type TEXT NOT NULL,due_date TEXT,source_message_ids TEXT NOT NULL,
 outcome TEXT,task_index INTEGER,decision_source_message_ids TEXT,reason TEXT,
 PRIMARY KEY(attempt_id,candidate_id),
 FOREIGN KEY(attempt_id,window_index) REFERENCES discovery_windows(attempt_id,window_index) ON DELETE CASCADE);
CREATE INDEX discovery_batch ON discovery_attempts(batch_id);
PRAGMA user_version=6;
COMMIT;
"""

DISCOVERY_REJECTION_MIGRATION = """
BEGIN IMMEDIATE;
CREATE TABLE discovery_rejections(
 attempt_id TEXT NOT NULL REFERENCES discovery_attempts(attempt_id) ON DELETE CASCADE,
 candidate_id TEXT,rule TEXT NOT NULL CHECK(rule IN (%s)),created_at TEXT NOT NULL,
 PRIMARY KEY(attempt_id,candidate_id,rule),
 FOREIGN KEY(attempt_id,candidate_id) REFERENCES discovery_candidates(attempt_id,candidate_id));
PRAGMA user_version=7;
COMMIT;
""" % ",".join("'" + rule + "'" for rule in DISCOVERY_REJECTION_RULES)

LOCAL_MODEL_MIGRATION = """
BEGIN IMMEDIATE;
CREATE TABLE model_local_keys(capability TEXT PRIMARY KEY REFERENCES model_configs(capability) ON DELETE CASCADE,
 api_key TEXT NOT NULL,storage_version INTEGER NOT NULL CHECK(storage_version=1));
PRAGMA user_version=8;
COMMIT;
"""

ATTENTION_MIGRATION = """
BEGIN IMMEDIATE;
CREATE TABLE attention_items(
 attention_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,batch_id TEXT NOT NULL REFERENCES analysis_runs(batch_id),
 kind TEXT NOT NULL CHECK(kind IN ('information','key_point')),title TEXT NOT NULL,summary TEXT NOT NULL,
 attention_reason TEXT NOT NULL,source_message_ids TEXT NOT NULL,related_chat_ids TEXT NOT NULL,
 event_at TEXT,observed_at TEXT NOT NULL,evidence_state TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX attention_account ON attention_items(account_id,observed_at DESC,attention_id);
CREATE TABLE attention_rejections(batch_id TEXT NOT NULL REFERENCES analysis_runs(batch_id),
 item_index INTEGER NOT NULL,rule TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(batch_id,item_index));
PRAGMA user_version=9;
COMMIT;
"""


MEETING_MIGRATION = """
BEGIN IMMEDIATE;
CREATE TABLE meeting_notes(
 meeting_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,title TEXT NOT NULL,meeting_date TEXT,
 original TEXT NOT NULL,content_hash TEXT NOT NULL,file_name TEXT,status TEXT NOT NULL,
 revision INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,error_code TEXT,
 generated_json TEXT,config_revision INTEGER,published_batch_id TEXT,source_message_id TEXT,
 UNIQUE(account_id,content_hash));
CREATE INDEX meeting_account ON meeting_notes(account_id,created_at DESC,meeting_id);
PRAGMA user_version=10;
COMMIT;
"""


NEWS_MIGRATION = """
BEGIN IMMEDIATE;
CREATE TABLE news_settings(account_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL,revision INTEGER NOT NULL,
 scope_json TEXT NOT NULL,scope_hash TEXT NOT NULL,first_day TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE news_runs(run_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,day TEXT NOT NULL,status TEXT NOT NULL,
 manifest_json TEXT NOT NULL,input_hash TEXT NOT NULL,scope_hash TEXT NOT NULL,subscription_revision INTEGER NOT NULL,
 config_revision INTEGER NOT NULL,created_at TEXT NOT NULL,finished_at TEXT,error_code TEXT);
CREATE INDEX news_run_day ON news_runs(account_id,day,created_at);
CREATE TABLE news_editions(account_id TEXT NOT NULL,day TEXT NOT NULL,revision INTEGER NOT NULL,
 body_json TEXT NOT NULL,manifest_json TEXT NOT NULL,input_hash TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(account_id,day,revision));
PRAGMA user_version=11;
COMMIT;
"""


ASK_MIGRATION = """
BEGIN IMMEDIATE;
CREATE TABLE ask_threads(ask_thread_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,title TEXT NOT NULL,
 status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE ask_turns(turn_id TEXT PRIMARY KEY,ask_thread_id TEXT NOT NULL REFERENCES ask_threads(ask_thread_id),
 question TEXT NOT NULL,conclusion TEXT NOT NULL DEFAULT '',answer_status TEXT NOT NULL,
 created_at TEXT NOT NULL,answered_at TEXT,error_code TEXT);
CREATE TABLE ask_runs(run_id TEXT PRIMARY KEY,turn_id TEXT NOT NULL UNIQUE REFERENCES ask_turns(turn_id),
 scope_json TEXT NOT NULL,config_revision INTEGER NOT NULL,coverage_json TEXT NOT NULL,
 evidence_json TEXT NOT NULL,call_id TEXT,status TEXT NOT NULL,sequence INTEGER NOT NULL DEFAULT 0);
CREATE TABLE ask_citations(run_id TEXT NOT NULL REFERENCES ask_runs(run_id),paragraph_index INTEGER NOT NULL,
 message_id TEXT NOT NULL,label TEXT NOT NULL,PRIMARY KEY(run_id,paragraph_index,message_id));
CREATE TABLE ask_grants(grant_id TEXT PRIMARY KEY,scope_json TEXT NOT NULL,scope_hash TEXT NOT NULL,
 config_revision INTEGER NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE VIRTUAL TABLE ask_fts USING fts5(message_id UNINDEXED,tokens);
CREATE TABLE ask_index(message_id TEXT PRIMARY KEY,content_hash TEXT NOT NULL);
PRAGMA user_version=12;
COMMIT;
"""


class Store:
    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not value or len(value) > 1000:
            raise ServiceError("invalid_identifier", "记录标识无效，请刷新后重新选择。")
        return value

    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "application.sqlite3"
        self.lock = threading.RLock()
        with self.database() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                try:
                    db.executescript(SCHEMA)
                except sqlite3.Error:
                    db.rollback()
                    raise ServiceError("migration_failed", "本地数据初始化失败；请保留数据目录后重试。") from None
            elif version not in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}:
                raise ServiceError("schema_version", "本地数据版本不兼容，请使用匹配的应用版本。")
            if version in {0, 1}:
                try:
                    db.executescript(MODEL_MIGRATION)
                except sqlite3.Error:
                    db.rollback()
                    raise ServiceError("migration_failed", "模型配置迁移失败，原有消息保留；请保留数据后重试。") from None
            if version in {0, 1, 2}:
                try:
                    db.executescript(AUTOMATIC_MIGRATION)
                except sqlite3.Error:
                    db.rollback()
                    raise ServiceError("migration_failed", "自动整理配置迁移失败，原有数据保留；请保留数据后重试。") from None
            if version in {0, 1, 2, 3}:
                try:
                    db.executescript(CONTEXT_MIGRATION)
                except sqlite3.Error:
                    db.rollback()
                    raise ServiceError("migration_failed", "整理上下文迁移失败，原有消息及结果保留；请保留数据后重试。") from None
            if version in {0, 1, 2, 3, 4}:
                try:
                    db.executescript(REVIEW_MIGRATION)
                except sqlite3.Error:
                    db.rollback()
                    raise ServiceError("migration_failed", "事项关联迁移失败，原有消息及结果保留；请保留数据后重试。") from None
            if version in {0, 1, 2, 3, 4, 5}:
                try:
                    db.executescript(DISCOVERY_MIGRATION)
                except sqlite3.Error:
                    db.rollback()
                    raise ServiceError("migration_failed", "候选核对记录迁移失败，原有消息及结果保留；请保留数据后重试。") from None
            if version in {0, 1, 2, 3, 4, 5, 6}:
                try:
                    db.executescript(DISCOVERY_REJECTION_MIGRATION)
                except sqlite3.Error:
                    db.rollback()
                    raise ServiceError("migration_failed", "核对诊断记录迁移失败，原有数据保留；请保留数据后重试。") from None
            if version in {0, 1, 2, 3, 4, 5, 6, 7}:
                try:
                    db.executescript(LOCAL_MODEL_MIGRATION)
                except sqlite3.Error:
                    db.rollback()
                    raise ServiceError("migration_failed", "本地模型配置结构迁移失败；原配置保留，请保留数据后重试。") from None
            if version in {0, 1, 2, 3, 4, 5, 6, 7, 8}:
                try:
                    db.executescript(ATTENTION_MIGRATION)
                except sqlite3.Error:
                    db.rollback()
                    raise ServiceError("migration_failed", "关注内容结构更新失败；原结果保留，请保留数据后重试。") from None
            if version < 10:
                try:
                    db.executescript(MEETING_MIGRATION)
                except sqlite3.Error:
                    db.rollback()
                    raise ServiceError("migration_failed", "会议记录更新失败，原有数据保留。") from None
            if version < 11:
                try:
                    db.executescript(NEWS_MIGRATION)
                except sqlite3.Error:
                    db.rollback()
                    raise ServiceError("migration_failed", "日报记录更新失败，原有数据保留。") from None
            if version < 12:
                try:
                    db.executescript(ASK_MIGRATION)
                except sqlite3.Error:
                    db.rollback()
                    raise ServiceError("migration_failed", "问答记录更新失败，原有数据保留。") from None
            defaults = {"revision": 0, "paused": False, "source": None, "time_start": None, "last_sync": None}
            for key, value in defaults.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (key, json.dumps(value)))
            for key, value in {"vault_id": str(uuid.uuid4()), "cloud_demo_consent": None}.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (key, json.dumps(value)))
        self.path.chmod(0o600)

    @contextmanager
    def database(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=2)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                with db:
                    yield db
            finally:
                db.close()

    @staticmethod
    def _settings(db):
        return {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT key,value FROM settings")}

    @staticmethod
    def _set(db, key, value):
        db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def settings(self):
        with self.database() as db:
            return self._settings(db)

    def results_revision(self):
        with self.database() as db:
            state = self._settings(db)
            account = state["source"]["account_id"] if state["source"] else ""
            return db.execute("SELECT COUNT(*) FROM analysis_runs WHERE account_id=? AND status='success'", (account,)).fetchone()[0]

    @staticmethod
    def invalidate_text_grant(db, reason):
        # Persist invalidation when the setting changes, even if changed back
        # before the next scheduler tick. Never reactivate an old grant.
        db.execute("UPDATE text_grants SET status='invalid',reason=? WHERE status='active'", (reason,))
        db.execute("""UPDATE text_batches SET status='cancelled',error_code=? WHERE status='queued'
            AND grant_id IN (SELECT grant_id FROM text_grants WHERE status='invalid')""", (reason,))

    def configure(self, source, time_start, discovered):
        parse_time(time_start)
        with self.database() as db:
            state = self._settings(db)
            if state["source"] != source or state["time_start"] != time_start:
                self.invalidate_text_grant(db, "source_changed")
            if not state["source"] or state["source"]["account_id"] != source["account_id"]:
                self._set(db, "last_sync", None)
            self._set(db, "source", source)
            self._set(db, "time_start", time_start)
            self._set(db, "revision", state["revision"] + 1)
            for chat in discovered:
                db.execute("""INSERT INTO chats(account_id,chat_id,chat_name,chat_type) VALUES(?,?,?,?)
                            ON CONFLICT(account_id,chat_id) DO UPDATE SET chat_name=excluded.chat_name,chat_type=excluded.chat_type""", (source["account_id"], chat["chat_id"], chat["chat_name"], chat["chat_type"]))

    def refresh_chats(self, discovered):
        state = self.settings()
        if not state["source"]:
            raise ServiceError("not_connected", "请先连接本地来源。")
        with self.database() as db:
            for chat in discovered:
                db.execute("""INSERT INTO chats(account_id,chat_id,chat_name,chat_type) VALUES(?,?,?,?)
                            ON CONFLICT(account_id,chat_id) DO UPDATE SET chat_name=excluded.chat_name,chat_type=excluded.chat_type""", (state["source"]["account_id"], chat["chat_id"], chat["chat_name"], chat["chat_type"]))

    def chats(self):
        state = self.settings()
        account = state["source"]["account_id"] if state["source"] else ""
        with self.database() as db:
            tag_links = {}
            for row in db.execute("SELECT ct.chat_id,t.tag_id,t.name FROM chat_tags ct JOIN tags t USING(tag_id) WHERE ct.account_id=? ORDER BY t.name", (account,)):
                tag_links.setdefault(row["chat_id"], []).append({"tag_id": row["tag_id"], "name": row["name"]})
            rows = db.execute("""SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.account_id=c.account_id AND m.chat_id=c.chat_id) AS message_count
                               FROM chats c WHERE account_id=? AND chat_type!='meeting_note' ORDER BY COALESCE(display_name,chat_name),chat_id""", (account,)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["sync_enabled"] = bool(item["sync_enabled"])
                item["analysis_enabled"] = bool(item["analysis_enabled"])
                item["tags"] = tag_links.get(item["chat_id"], [])
                result.append(item)
            return result

    def update_chat(self, chat_id, *, sync_enabled, analysis_enabled, display_name=None, tag_ids=()):
        self.identifier(chat_id)
        if type(sync_enabled) is not bool or type(analysis_enabled) is not bool:
            raise ServiceError("invalid_switch", "同步和分析开关需要分别设置。")
        if analysis_enabled and not sync_enabled:
            raise ServiceError("analysis_requires_sync", "请先开启本地同步，再纳入文字分析。")
        if display_name is not None and (not isinstance(display_name, str) or len(display_name) > 100):
            raise ServiceError("invalid_name", "会话显示名称不能超过 100 个字符。")
        if not isinstance(tag_ids, (list, tuple)) or any(not isinstance(t, str) for t in tag_ids) or len(tag_ids) != len(set(tag_ids)):
            raise ServiceError("invalid_tags", "会话标签格式无效或重复。")
        with self.database() as db:
            state = self._settings(db)
            if not state["source"]:
                raise ServiceError("not_connected", "请先连接本地来源。")
            account = state["source"]["account_id"]
            previous = db.execute("SELECT sync_enabled,analysis_enabled FROM chats WHERE account_id=? AND chat_id=?", (account, chat_id)).fetchone()
            if not previous:
                raise ServiceError("chat_missing", "会话不存在，请刷新会话列表。")
            known = {r[0] for r in db.execute("SELECT tag_id FROM tags")}
            if not set(tag_ids) <= known:
                raise ServiceError("tag_missing", "标签已不存在，请刷新后重新选择。")
            if (previous[1] or analysis_enabled) and tuple(previous) != (int(sync_enabled), int(analysis_enabled)):
                self.invalidate_text_grant(db, "chat_scope_changed")
            db.execute("UPDATE chats SET sync_enabled=?,analysis_enabled=?,display_name=? WHERE account_id=? AND chat_id=?", (int(sync_enabled), int(analysis_enabled), display_name.strip() or None if display_name is not None else None, account, chat_id))
            db.execute("DELETE FROM chat_tags WHERE account_id=? AND chat_id=?", (account, chat_id))
            db.executemany("INSERT INTO chat_tags VALUES(?,?,?)", [(account, chat_id, tag) for tag in tag_ids])
            self._set(db, "revision", state["revision"] + 1)

    def tags(self):
        with self.database() as db:
            return [dict(r) for r in db.execute("SELECT * FROM tags ORDER BY name")]

    def save_tag(self, name, tag_id=None):
        if tag_id is not None:
            self.identifier(tag_id)
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 30:
            raise ServiceError("invalid_tag_name", "请输入 1–30 个字符的标签名。")
        with self.database() as db:
            try:
                if tag_id:
                    if not db.execute("SELECT 1 FROM tags WHERE tag_id=?", (tag_id,)).fetchone():
                        raise ServiceError("tag_missing", "标签已不存在，请刷新标签列表。")
                    db.execute("UPDATE tags SET name=? WHERE tag_id=?", (name.strip(), tag_id))
                else:
                    tag_id = str(uuid.uuid4())
                    db.execute("INSERT INTO tags VALUES(?,?)", (tag_id, name.strip()))
            except sqlite3.IntegrityError:
                raise ServiceError("duplicate_tag", "这个标签名已存在，请使用其他名称。") from None
        return tag_id

    def delete_tag(self, tag_id):
        self.identifier(tag_id)
        with self.database() as db:
            if not db.execute("DELETE FROM tags WHERE tag_id=?", (tag_id,)).rowcount:
                raise ServiceError("tag_missing", "标签已不存在，请刷新标签列表。")

    def set_paused(self, paused):
        if type(paused) is not bool:
            raise ServiceError("invalid_pause", "暂停状态格式无效。")
        with self.database() as db:
            self._set(db, "paused", paused)

    def set_range(self, time_start):
        parse_time(time_start)
        with self.database() as db:
            state = self._settings(db)
            if state["time_start"] != time_start:
                self.invalidate_text_grant(db, "time_range_changed")
            self._set(db, "time_start", time_start)
            self._set(db, "revision", state["revision"] + 1)

    def cursor(self, account_id, chat_id, stream_id, range_key):
        with self.database() as db:
            row = db.execute("SELECT local_id FROM cursors WHERE account_id=? AND chat_id=? AND stream_id=? AND range_key=?", (account_id, chat_id, stream_id, range_key)).fetchone()
            return row[0] if row else "0"

    def commit_batch(self, scope, batch, range_key, revision, *, cancelled=lambda: False, fault=None, arrived_epoch=None):
        if batch.chat_id not in scope.chat_ids or any(msg.account_id != scope.account_id or msg.chat_id != batch.chat_id or msg.stream_id != batch.stream_id for msg in batch.messages):
            raise ServiceError("out_of_scope", "读取批次与当前账号或会话不一致，未写入数据。")
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = self._settings(db)
            selected = db.execute("SELECT sync_enabled FROM chats WHERE account_id=? AND chat_id=?", (scope.account_id, batch.chat_id)).fetchone()
            if cancelled() or settings["paused"] or settings["revision"] != revision or not settings["source"] or settings["source"]["account_id"] != scope.account_id or not selected or not selected[0]:
                raise ServiceError("cancelled", "范围或暂停状态已改变，未提交本批次，成功进度保留。", retryable=True)
            inserted = 0
            for msg in batch.messages:
                if cancelled():
                    raise ServiceError("cancelled", "同步已停止，本批次未提交。", retryable=True)
                if fault == "write":
                    raise sqlite3.OperationalError("synthetic failure")
                result = db.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (msg.message_id, msg.account_id, msg.chat_id, msg.stream_id, msg.local_id, msg.sender_id, msg.sender_name, None if msg.is_self is None else int(msg.is_self), msg.sent_at, parse_time(msg.sent_at), msg.message_type, msg.content, msg.content_state, int(msg.analysis_eligible)))
                inserted += result.rowcount
                if result.rowcount:
                    db.execute("INSERT INTO message_arrivals VALUES(?,?)", (msg.message_id, time.time() if arrived_epoch is None else arrived_epoch))
            db.execute("INSERT OR REPLACE INTO cursors VALUES(?,?,?,?,?)", (scope.account_id, batch.chat_id, batch.stream_id, range_key, batch.next_cursor))
            if fault == "commit":
                raise sqlite3.OperationalError("synthetic failure")
            if cancelled():
                raise ServiceError("cancelled", "同步已停止，本批次未提交。", retryable=True)
            # Explicit commit: do not report inserted until it actually succeeds.
            db.commit()
            return inserted

    def sync_store_signature(self, account_id):
        with self.database() as db:
            db.execute("BEGIN")
            cursors = [tuple(row) for row in db.execute("SELECT chat_id,stream_id,range_key,local_id FROM cursors WHERE account_id=? ORDER BY chat_id,stream_id,range_key", (account_id,))]
            count = db.execute("SELECT COUNT(*) FROM messages WHERE account_id=? AND stream_id!='meeting_note'", (account_id,)).fetchone()[0]
            return stable_id(cursors, count)

    def record_run(self, report, *, checkpoint=None, expected=None, cancelled=lambda: False):
        with self.database() as db:
            # One transaction publishes the run and its successful baseline.
            db.execute("BEGIN IMMEDIATE")
            if expected is not None:
                state = self._settings(db)
                if (cancelled() or state["paused"] or state["revision"] != expected["revision"]
                        or state["source"] != expected["source"]):
                    report["outcome"] = "cancelled"
            db.execute("INSERT OR REPLACE INTO sync_runs VALUES(?,?,?,?,?,?)", (report["run_id"], report["account_id"], report["started_at"], report.get("finished_at"), report["outcome"], json.dumps(report, ensure_ascii=False)))
            self._set(db, "last_sync", report)
            if checkpoint is not None and report["outcome"] == "success":
                self._set(db, "encrypted_sync_checkpoint", checkpoint)

    def message_chats(self, *, query="", limit=25, offset=0):
        if not isinstance(query, str) or len(query) > 200:
            raise ServiceError("invalid_query", "会话搜索内容需在 200 个字符以内。")
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ServiceError("invalid_page", "会话分页参数无效。")
        with self.database() as db:
            db.execute("BEGIN")
            source = self._settings(db)["source"]
            account = source["account_id"] if source else ""
            where, args = "m.account_id=? AND m.stream_id!='meeting_note'", [account]
            if query.strip():
                pattern = "%" + query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                where += " AND (COALESCE(c.display_name,'') LIKE ? ESCAPE '\\' OR COALESCE(c.chat_name,'') LIKE ? ESCAPE '\\' OR m.chat_id LIKE ? ESCAPE '\\')"
                args.extend([pattern] * 3)
            grouped = f"""SELECT m.chat_id,COALESCE(c.display_name,c.chat_name,m.chat_id) AS chat_name,
                COALESCE(c.chat_type,'unknown') AS chat_type,COUNT(*) AS message_count,MAX(m.sent_epoch) AS last_epoch
                FROM messages m LEFT JOIN chats c ON c.account_id=m.account_id AND c.chat_id=m.chat_id
                WHERE {where} GROUP BY m.chat_id"""
            total = db.execute(f"SELECT COUNT(*) FROM ({grouped})", args).fetchone()[0]
            rows = db.execute(f"""SELECT g.*,
                (SELECT SUBSTR(content,1,80) FROM messages WHERE account_id=? AND chat_id=g.chat_id
                 ORDER BY sent_epoch DESC,message_id LIMIT 1) AS content_preview
                FROM ({grouped}) g ORDER BY g.last_epoch DESC,g.chat_id LIMIT ? OFFSET ?""", [account, *args, limit, offset]).fetchall()
            return {"items": [dict(row) for row in rows], "total": total, "offset": offset,
                    "has_more": offset + len(rows) < total}

    def messages(self, chat_id=None, *, limit=100, offset=0, time_start=None, time_end=None,
                 sender_id=None, sender_unknown=False, order="desc"):
        if chat_id is not None:
            self.identifier(chat_id)
        if sender_id is not None:
            self.identifier(sender_id)
        if type(sender_unknown) is not bool or (sender_unknown and sender_id is not None) or order not in ("asc", "desc"):
            raise ServiceError("invalid_filter", "消息筛选参数无效，请重新选择。")
        start = parse_time(time_start) if time_start is not None else None
        end = parse_time(time_end) if time_end is not None else None
        if start is not None and end is not None and start >= end:
            raise ServiceError("invalid_time_range", "开始时间需要早于结束时间。")
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise ServiceError("invalid_page", "消息分页参数无效。")
        with self.database() as db:
            db.execute("BEGIN")
            settings = self._settings(db)
            account = settings["source"]["account_id"] if settings["source"] else ""
            where, args = "m.account_id=? AND m.stream_id!='meeting_note'", [account]
            if chat_id:
                where += " AND m.chat_id=?"
                args.append(chat_id)
            # Facets cover the saved conversation, not merely this page or the active filter.
            saved = db.execute(f"SELECT COUNT(*),MIN(sent_epoch),MAX(sent_epoch) FROM messages m WHERE {where}", args).fetchone()
            senders = db.execute(f"""SELECT account_id,NULLIF(sender_id,'') AS sender_id,
                MAX(NULLIF(sender_name,'')) AS sender_name,
                CASE WHEN MIN(is_self)=MAX(is_self) AND COUNT(is_self)=COUNT(*) THEN MIN(is_self) ELSE NULL END AS is_self,
                COUNT(*) AS message_count FROM messages m WHERE {where}
                GROUP BY NULLIF(sender_id,'') ORDER BY sender_name,sender_id""", args).fetchall()
            sender_items = identified([dict(row) for row in senders], settings["source"])
            if start is not None:
                where += " AND m.sent_epoch>=?"
                args.append(start)
            if end is not None:
                where += " AND m.sent_epoch<?"
                args.append(end)
            if sender_id is not None:
                where += " AND m.sender_id=?"
                args.append(sender_id)
            elif sender_unknown:
                where += " AND (m.sender_id IS NULL OR m.sender_id='')"
            total = db.execute(f"SELECT COUNT(*) FROM messages m WHERE {where}", args).fetchone()[0]
            rows = db.execute(f"""SELECT m.*,COALESCE(c.display_name,c.chat_name,m.chat_id) AS chat_name FROM messages m
                                 LEFT JOIN chats c ON c.account_id=m.account_id AND c.chat_id=m.chat_id
                                 WHERE {where} ORDER BY sent_epoch {order},message_id LIMIT ? OFFSET ?""", args + [limit, offset]).fetchall()
            items = []
            for row in identified([dict(r) for r in rows], settings["source"]):
                item = dict(row)
                item["is_self"] = None if item["is_self"] is None else bool(item["is_self"])
                item["effective_is_self"] = None if item["effective_is_self"] is None else bool(item["effective_is_self"])
                item["analysis_eligible"] = bool(item["analysis_eligible"])
                text = item["content"] or ""
                item["content_preview"] = text[:100] + ("…" if len(text) > 100 else "")
                items.append(item)
            return {"items": items, "total": total, "offset": offset, "has_more": offset + len(items) < total,
                    "saved_total": saved[0], "time_start": iso_time(saved[1]) if saved[1] is not None else None,
                    "time_end": iso_time(saved[2]) if saved[2] is not None else None, "senders": sender_items}

    def message_detail(self, message_id):
        self.identifier(message_id)
        with self.database() as db:
            db.execute("BEGIN")
            settings = self._settings(db)
            account = settings["source"]["account_id"] if settings["source"] else ""
            row = db.execute("""SELECT m.*,COALESCE(c.display_name,c.chat_name,m.chat_id) AS chat_name
                              FROM messages m LEFT JOIN chats c ON c.account_id=m.account_id AND c.chat_id=m.chat_id
                              WHERE m.account_id=? AND m.message_id=?""", (account, message_id)).fetchone()
            if row is None:
                raise ServiceError("message_missing", "该消息暂不可用，请返回已同步消息列表重新选择。")
            item = dict(row)
            meeting = db.execute("SELECT title,meeting_date,created_at FROM meeting_notes WHERE account_id=? AND source_message_id=?",
                (account, message_id)).fetchone()
            chat = db.execute("SELECT chat_type FROM chats WHERE account_id=? AND chat_id=?", (account, item["chat_id"])).fetchone()
            if meeting or (chat and chat[0] in {"meeting_note", "manual_meeting"}):
                item.update(source_kind="meeting", meeting_title=meeting["title"] if meeting else item["chat_name"],
                    meeting_date=meeting["meeting_date"] if meeting else None,
                    imported_at=meeting["created_at"] if meeting else item["sent_at"])
            def payload(value):
                value = identified([value], settings["source"])[0]
                value["is_self"] = None if value["is_self"] is None else bool(value["is_self"])
                value["effective_is_self"] = None if value["effective_is_self"] is None else bool(value["effective_is_self"])
                value["analysis_eligible"] = bool(value["analysis_eligible"])
                text = value["content"] or ""
                value["content_preview"] = text[:100] + ("…" if len(text) > 100 else "")
                return value

            neighbors = db.execute("""SELECT * FROM messages WHERE account_id=? AND chat_id=? AND message_id!=?
                                     ORDER BY ABS(sent_epoch-?),sent_epoch,message_id LIMIT 6""", (account, item["chat_id"], message_id, item["sent_epoch"])).fetchall()
            item["context"] = sorted([payload({**dict(n), "chat_name": item["chat_name"]}) for n in neighbors], key=lambda n: (n["sent_epoch"], n["message_id"]))
            return payload(item)
