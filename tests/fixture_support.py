"""Synthetic fixture construction and consistency checks, NOT a product reader.

The SQLite layouts below mirror the columns used by the current collector.
They do not reproduce encryption, WAL extraction, or all WeChat versions.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory


FIXTURES = Path(__file__).resolve().parent / "fixtures"


@contextmanager
def database(path):
    """Commit/rollback AND close; sqlite3.Connection.__exit__ alone does not close."""
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def epoch(iso_time):
    value = datetime.fromisoformat(iso_time)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Fixture timestamps must have an explicit timezone")
    return int(value.timestamp())


def content_of(message):
    payload = message["payload"]
    if set(payload) == {"text"} and isinstance(payload["text"], str):
        return payload["text"]
    if set(payload) == {"repeat"}:
        recipe = payload["repeat"]
        if (
            set(recipe) == {"unit", "count"}
            and isinstance(recipe["unit"], str)
            and type(recipe["count"]) is int
            and 0 < recipe["count"] <= 10000
        ):
            return recipe["unit"] * recipe["count"]
    raise ValueError("Invalid synthetic text recipe")


def message_table(chat_id):
    return "Msg_" + hashlib.md5(chat_id.encode("utf-8")).hexdigest()


def validate_case(case):
    if case.get("synthetic") is not True:
        raise ValueError("Only explicitly synthetic fixtures are accepted")
    if not case["id"] or not case["self_id"].startswith("fixture_"):
        raise ValueError("Missing fixture identity")
    chat_ids = [chat["id"] for chat in case["chats"]]
    if len(set(chat_ids)) != len(chat_ids):
        raise ValueError("Duplicate chat identity")
    for chat in case["chats"]:
        if not chat["id"].startswith("fixture_"):
            raise ValueError("Non-fixture chat identity")
        if chat["table_state"] not in {"present", "missing", "missing_content"}:
            raise ValueError("Unknown source table state")
    selected = case["selected_chats"]
    if len(set(selected)) != len(selected) or not set(selected) <= set(chat_ids):
        raise ValueError("Selected chats must be unique known chats")
    start, end = epoch(case["window"]["start"]), epoch(case["window"]["end"])
    if start >= end or case["window"]["bounds"] != "[start,end)":
        raise ValueError("Invalid fixture time window")
    contacts = case["contacts"]
    refs = [contact["sender_ref"] for contact in contacts]
    if len(set(refs)) != len(refs):
        raise ValueError("Duplicate sender reference")
    for contact in contacts:
        if type(contact["sender_ref"]) is not int or contact["sender_ref"] <= 0:
            raise ValueError("Sender references must be positive integers")
        if not contact["sender_id"].startswith("fixture_"):
            raise ValueError("Non-fixture sender identity")
    labels, keys = set(), set()
    for message in case["messages"]:
        label = message["label"]
        if not label or label in labels:
            raise ValueError("Duplicate or empty message label")
        labels.add(label)
        local_id = message["local_id"]
        if not isinstance(local_id, str) or not local_id.isdecimal() or int(local_id) <= 0:
            raise ValueError("Invalid local message ID")
        key = (message["chat_id"], int(local_id))
        if message["chat_id"] not in chat_ids or key in keys:
            raise ValueError("Unknown chat or duplicate source message key")
        keys.add(key)
        if type(message["sender_ref"]) is not int or type(message["type"]) is not int:
            raise ValueError("Invalid source message column")
        epoch(message["sent_at"])
        content_of(message)
    stored = case["initial"]["stored_labels"]
    if len(set(stored)) != len(stored) or not set(stored) <= labels:
        raise ValueError("Invalid initial stored message labels")
    cursors = case["initial"]["cursors"]
    for chat_id, cursor in cursors.items():
        if chat_id not in chat_ids or not isinstance(cursor, str) or not cursor.isdecimal():
            raise ValueError("Invalid initial cursor")
        saved_ids = [
            int(msg["local_id"]) for msg in case["messages"]
            if msg["chat_id"] == chat_id and msg["label"] in stored
        ]
        if int(cursor) > max(saved_ids, default=0):
            raise ValueError("Initial cursor skips unsaved messages")
    if type(case["repeat_sync"]) is not int or case["repeat_sync"] not in {1, 2}:
        raise ValueError("Unsupported fixture replay count")
    fault = case["fault"]
    if fault["kind"] not in {"none", "write", "commit"}:
        raise ValueError("Unknown fixture fault")
    if fault["kind"] != "none" and fault.get("chat_id") not in selected:
        raise ValueError("Fault must target a selected chat")


def load_suite():
    inputs = json.loads((FIXTURES / "reader_inputs.json").read_text(encoding="utf-8"))
    expected = json.loads((FIXTURES / "reader_expected.json").read_text(encoding="utf-8"))
    for manifest in (inputs, expected):
        if manifest.get("schema_version") != 1 or manifest.get("synthetic") is not True:
            raise ValueError("Unsupported or non-synthetic fixture manifest")
    cases = inputs["cases"]
    ids = [case["id"] for case in cases]
    if len(set(ids)) != len(ids) or set(ids) != set(expected["cases"]):
        raise ValueError("Input and expected case IDs must match exactly")
    for case in cases:
        validate_case(case)
    return {case["id"]: case for case in cases}, expected["cases"]


def eligible_messages(case):
    """In-memory fixture oracle only; never used by production sync code."""
    start, end = epoch(case["window"]["start"]), epoch(case["window"]["end"])
    states = {chat["id"]: chat["table_state"] for chat in case["chats"]}
    cursors = case["initial"]["cursors"]
    return [
        msg for msg in case["messages"]
        if msg["chat_id"] in case["selected_chats"]
        and states[msg["chat_id"]] == "present"
        and start <= epoch(msg["sent_at"]) < end
        and int(msg["local_id"]) > int(cursors.get(msg["chat_id"], "0"))
    ]


def derive_expectation(case):
    """Check independently authored expected JSON against the synthetic inputs.

Failure cases model rollback of the test transaction. They do not prescribe
every future reader's partial-commit strategy or its public status enums.
"""
    rows = eligible_messages(case)
    by_chat = {chat["id"]: chat for chat in case["chats"]}
    failed = {}
    cursors = dict(case["initial"]["cursors"])
    stored = set(case["initial"]["stored_labels"])
    for chat_id in case["selected_chats"]:
        cursors.setdefault(chat_id, "0")
        state = by_chat[chat_id]["table_state"]
        if state != "present":
            failed[chat_id] = "missing_table" if state == "missing" else "missing_content"
        elif case["fault"]["kind"] != "none" and case["fault"]["chat_id"] == chat_id:
            failed[chat_id] = case["fault"]["kind"]
        else:
            messages = [msg for msg in rows if msg["chat_id"] == chat_id]
            stored.update(msg["label"] for msg in messages)
            if messages:
                cursors[chat_id] = str(max(int(msg["local_id"]) for msg in messages))
    if not case["selected_chats"]:
        outcome = "no_selection"
    elif failed:
        outcome = "failed" if len(failed) == len(case["selected_chats"]) else "partial_success"
    else:
        outcome = "success" if rows else "no_new"
    contacts = {person["sender_ref"]: person for person in case["contacts"]}
    identities = {}
    for msg in rows:
        person = contacts.get(msg["sender_ref"])
        identities[msg["label"]] = {
            "sender_id": person["sender_id"] if person else None,
            "is_self": person["sender_id"] == case["self_id"] if person else None,
        }
    return {
        "read_labels": [msg["label"] for msg in rows],
        "stored_labels": [msg["label"] for msg in case["messages"] if msg["label"] in stored],
        "cursors": cursors,
        "failed_chats": failed,
        "outcome": outcome,
        "content_lengths": {msg["label"]: len(content_of(msg)) for msg in rows},
        "identities": identities,
    }


@dataclass(frozen=True)
class FixturePaths:
    root: Path
    message_db: Path
    contact_db: Path
    session_db: Path
    collector_db: Path


@contextmanager
def fixture_environment(case):
    """Create ONLY an owned temporary fixture, and remove it even on failure."""
    validate_case(case)
    with TemporaryDirectory(prefix="today-wechat-synthetic-") as directory:
        root = Path(directory)
        for name in ("message", "contact", "session"):
            (root / "decrypted" / name).mkdir(parents=True)
        paths = FixturePaths(
            root=root,
            message_db=root / "decrypted/message/message_0.db",
            contact_db=root / "decrypted/contact/contact.db",
            session_db=root / "decrypted/session/session.db",
            collector_db=root / "collector.db",
        )
        with database(paths.contact_db) as db:
            db.execute("CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT)")
            db.executemany("INSERT INTO contact VALUES(?,?,?)", [
                (p["sender_id"], p["display_name"], "") for p in case["contacts"]
            ] + [(c["id"], c["name"], "") for c in case["chats"]])
        with database(paths.session_db) as db:
            db.execute("CREATE TABLE SessionTable(username TEXT, last_timestamp INTEGER)")
            for chat in case["chats"]:
                times = [epoch(msg["sent_at"]) for msg in case["messages"] if msg["chat_id"] == chat["id"]]
                db.execute("INSERT INTO SessionTable VALUES(?,?)", (chat["id"], max(times, default=0)))
        with database(paths.message_db) as db:
            db.execute("CREATE TABLE Name2Id(user_name TEXT)")
            db.executemany("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", [
                (p["sender_ref"], p["sender_id"]) for p in case["contacts"]
            ])
            for chat in case["chats"]:
                if chat["table_state"] == "missing":
                    continue
                table = message_table(chat["id"])
                has_content = chat["table_state"] != "missing_content"
                body_column = ",message_content TEXT" if has_content else ""
                db.execute(f"CREATE TABLE [{table}] (local_id INTEGER PRIMARY KEY,real_sender_id INTEGER,create_time INTEGER,local_type INTEGER,status INTEGER{body_column})")
                for msg in case["messages"]:
                    if msg["chat_id"] != chat["id"]:
                        continue
                    values = (int(msg["local_id"]), msg["sender_ref"], epoch(msg["sent_at"]), msg["type"], 0)
                    if has_content:
                        values += (content_of(msg),)
                    placeholders = ",".join("?" for _ in values)
                    db.execute(f"INSERT INTO [{table}] VALUES({placeholders})", values)
        with database(paths.collector_db) as db:
            db.executescript("""
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, chatroom_id TEXT NOT NULL,
                    sender TEXT, content TEXT, msg_time INTEGER, local_id TEXT,
                    msg_type INTEGER DEFAULT 1, UNIQUE(chatroom_id,local_id)
                );
                CREATE TABLE watched_chats (chatroom_id TEXT PRIMARY KEY, chatroom_name TEXT, added_at INTEGER);
                CREATE TABLE sync_state (chatroom_id TEXT PRIMARY KEY, last_local_id TEXT DEFAULT '0', last_sync_at INTEGER DEFAULT 0);
            """)
            db.executemany("INSERT INTO watched_chats VALUES(?,?,0)", [(c["id"], c["name"]) for c in case["chats"]])
            for msg in case["messages"]:
                if msg["label"] in case["initial"]["stored_labels"]:
                    db.execute("INSERT INTO messages(chatroom_id,content,msg_time,local_id,msg_type) VALUES(?,?,?,?,?)", (msg["chat_id"], content_of(msg), epoch(msg["sent_at"]), msg["local_id"], msg["type"]))
            db.executemany("INSERT INTO sync_state(chatroom_id,last_local_id) VALUES(?,?)", case["initial"]["cursors"].items())
        yield paths


def install_write_failure(db):
    db.execute("""CREATE TEMP TRIGGER fixture_write_failure BEFORE INSERT ON messages
                  BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END""")


def install_commit_failure(db):
    def deny_commit(action, argument, _second, _database, _trigger):
        if action == sqlite3.SQLITE_TRANSACTION and argument == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    db.set_authorizer(deny_commit)
