"""Read explicitly selected, decrypted SQLite sources. Never writes source DBs."""

from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import sqlite3

from backend.contracts import Message, ReadBatch, ServiceError, iso_time, parse_time, stable_id
from backend.source_layout import message_shards


def quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


@contextmanager
def source_database(path, cancelled=None):
    path = Path(path).resolve()
    if not path.is_file():
        raise ServiceError("source_missing", "本地数据库不存在，请检查选择的解密目录。", retryable=True)
    db = None
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
        db.execute("PRAGMA query_only=ON")
        if cancelled:
            db.set_progress_handler(lambda: int(cancelled()), 1000)
        db.execute("BEGIN")
        yield db
    except sqlite3.Error:
        if cancelled and cancelled():
            raise ServiceError("cancelled", "同步已停止，已保存进度保留。", retryable=True) from None
        raise ServiceError("source_unreadable", "数据库暂不可读、尚未解密或格式不兼容，请检查来源后重试。", retryable=True) from None
    finally:
        if db is not None:
            db.close()


class SourceReader:
    def __init__(self, root, account_id, self_id=None):
        self.root = Path(root).expanduser().resolve()
        self.account_id = account_id
        self.self_id = self_id or None
        self.deferred_until = None
        if not self.root.is_dir() or not (self.root / "message").is_dir():
            raise ServiceError("invalid_source", "请选择含 message 子目录的已解密数据库目录。")

    def safe_path(self, path):
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ServiceError("invalid_source", "数据库路径超出已选择的目录，请检查链接文件。")
        return resolved

    def contacts(self):
        path = self.safe_path(self.root / "contact/contact.db")
        if not path.is_file():
            return {}
        with source_database(path) as db:
            try:
                return {uid: (remark or nickname or uid) for uid, nickname, remark in db.execute("SELECT username,nick_name,remark FROM contact") if uid}
            except sqlite3.Error:
                raise ServiceError("contact_schema", "联系人表结构不兼容；无法确认的发送人不会猜测。", retryable=True) from None

    def discover(self):
        names = self.contacts()
        ids = set()
        path = self.safe_path(self.root / "session/session.db")
        if path.is_file():
            with source_database(path) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for name in ("SessionTable", "session", "Session"):
                    if name not in tables:
                        continue
                    cols = {r[1] for r in db.execute(f"PRAGMA table_info({quote_identifier(name)})")}
                    column = next((c for c in ("username", "userName", "strUsrName") if c in cols), None)
                    if column:
                        ids.update(r[0] for r in db.execute(f"SELECT {quote_identifier(column)} FROM {quote_identifier(name)}") if r[0])
        # Contact entries can be discovered but are not selected or authorized.
        ids.update(uid for uid in names if "@chatroom" in uid or "@im.chatroom" in uid)
        return [{"chat_id": uid, "chat_name": names.get(uid, uid), "chat_type": "group" if "@chatroom" in uid or "@im.chatroom" in uid else "direct"} for uid in sorted(ids) if not uid.startswith("gh_") and uid not in {"weixin", "filehelper", self.self_id}]

    def streams(self, chat_id):
        expected = "Msg_" + hashlib.md5(chat_id.encode("utf-8")).hexdigest()
        result = []
        for path in message_shards(self.root / "message"):
            path = self.safe_path(path)
            with source_database(path) as db:
                tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            for table in tables:
                if table == expected or (table.startswith(expected) and re.fullmatch(r"[0-9]+", table[len(expected):])):
                    relative = str(path.relative_to(self.root))
                    result.append((stable_id(relative, table), path, table))
        if not result:
            raise ServiceError("missing_table", "该会话的消息表尚未取得，请重试该会话或检查解密来源。", retryable=True)
        return result

    def read_batch(self, scope, chat_id, stream, cursor="0", limit=500, cancelled=None):
        if chat_id not in scope.chat_ids or scope.account_id != self.account_id:
            raise ServiceError("out_of_scope", "该会话不在当前读取范围内。")
        if not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdecimal():
            raise ServiceError("invalid_cursor", "同步进度无效，未继续读取。")
        if not 1 <= limit <= 2000:
            raise ServiceError("invalid_limit", "读取批次大小无效。")
        if cancelled and cancelled():
            raise ServiceError("cancelled", "同步已停止，已保存进度保留。", retryable=True)
        stream_id, path, table = stream
        names = self.contacts()
        with source_database(path, cancelled=cancelled) as db:
            cols = {r[1] for r in db.execute(f"PRAGMA table_info({quote_identifier(table)})")}
            def column(options):
                return next((c for c in options if c in cols), None)
            local = column(("local_id", "MsgLocalID")) or "rowid"
            timing = column(("create_time", "msg_time", "CreateTime"))
            body = column(("message_content", "compress_content", "content", "Content"))
            sender = column(("real_sender_id", "sender", "StrTalker"))
            kind = column(("local_type", "MsgType", "Type"))
            server = column(("server_id", "MsgSvrID"))
            if not body:
                raise ServiceError("missing_content", "该会话缺少可识别的正文列，不能判断为没有新消息。", retryable=True)
            if not timing:
                raise ServiceError("missing_time", "该会话缺少时间字段，无法安全应用七天范围。", retryable=True)
            mapping = {}
            if "Name2Id" in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                mapping = dict(db.execute("SELECT rowid,user_name FROM Name2Id"))
            fields = [local, sender, body, timing, kind, server]
            select = ",".join(quote_identifier(f) if f else "NULL" for f in fields)
            qlocal, qtime, qtable = quote_identifier(local), quote_identifier(timing), quote_identifier(table)
            start, end = parse_time(scope.time_start), parse_time(scope.time_end)
            rows = db.execute(f"SELECT {select} FROM {qtable} WHERE CAST({qlocal} AS INTEGER)>? AND {qtime}>=? AND {qtime}<? ORDER BY CAST({qlocal} AS INTEGER) LIMIT ?", (int(cursor), start, end, limit + 1)).fetchall()
            # Do not move past a lower-ID message whose timestamp is still ahead.
            future, future_time = db.execute(f"SELECT MIN(CAST({qlocal} AS INTEGER)),MIN({qtime}) FROM {qtable} WHERE CAST({qlocal} AS INTEGER)>? AND {qtime}>=?", (int(cursor), end)).fetchone()
            if future_time is not None:
                self.deferred_until = min(self.deferred_until, float(future_time)) if self.deferred_until is not None else float(future_time)
        result = []
        for lid, raw_sender, raw_content, seconds, raw_kind, server_id in rows[:limit]:
            if cancelled and cancelled():
                raise ServiceError("cancelled", "同步已停止，已保存进度保留。", retryable=True)
            uid = None
            if raw_sender is not None:
                if isinstance(raw_sender, int) or str(raw_sender).isdecimal():
                    uid = mapping.get(int(raw_sender))
                else:
                    uid = str(raw_sender) or None
            is_self = uid == self.self_id if uid and self.self_id else None
            try:
                type_number = int(raw_kind) if raw_kind is not None else None
            except (ValueError, TypeError):
                type_number = None
            mtype = {1: "text", 3: "image", 34: "audio", 43: "video", 49: "file", 10000: "system"}.get(type_number, "other")
            content, state = None, "type_only"
            if mtype in {"text", "system"}:
                try:
                    if isinstance(raw_content, bytes):
                        if raw_content.startswith(b"\x28\xb5\x2f\xfd"):
                            import zstandard
                            content = zstandard.ZstdDecompressor().decompress(raw_content, max_output_size=16 * 1024 * 1024).decode("utf-8")
                        else:
                            content = raw_content.decode("utf-8")
                    elif raw_content is not None:
                        content = str(raw_content)
                    state = "text_available" if content is not None else "read_failed"
                except (ImportError, UnicodeError, ValueError, RuntimeError):
                    state = "read_failed"
                except Exception:
                    # Codec failures must not leak payloads into error messages.
                    state = "read_failed"
            identity = ("server", str(server_id)) if server_id not in (None, 0, "0", "") else ("local", stream_id, str(lid))
            result.append(Message(stable_id(self.account_id, chat_id, *identity), self.account_id, chat_id, stream_id, str(lid), uid, names.get(uid, uid) if uid else None, is_self, iso_time(seconds), mtype, content, state, mtype == "text" and state == "text_available"))
        next_cursor = max([int(cursor)] + [int(msg.local_id) for msg in result])
        if future is not None:
            next_cursor = min(next_cursor, max(int(cursor), int(future) - 1))
        return ReadBatch(chat_id, stream_id, tuple(result), str(next_cursor), len(rows) > limit)
