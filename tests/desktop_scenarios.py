"""Prepare owned synthetic directories for actual desktop fault/lifecycle checks.

This is a test-data controller, not a replacement GUI or reader. The desktop
app still launches its packaged production backend and reads real SQLite.
"""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from backend.demo import CHATS, database, table_name
from backend.service import LocalService


MARKER = ".owned-gui-scenario.json"


def validate(directory):
    root = Path(directory).resolve(strict=True)
    temporary = Path(tempfile.gettempdir()).resolve()
    if root.parent != temporary or not root.name.startswith("wechat-today-gui-check-"):
        raise ValueError("Only this test's temporary directories are allowed")
    marker = json.loads((root / MARKER).read_text(encoding="utf-8"))
    if marker != {"synthetic": True, "purpose": "desktop-scenarios-v1"}:
        raise ValueError("Unrecognized test-directory ownership")
    for path in (root / "application.sqlite3", root / "demo-source-v1/message/message_0.db"):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Linked databases are not allowed")
    return root


def create():
    root = Path(tempfile.mkdtemp(prefix="wechat-today-gui-check-")).resolve()
    (root / MARKER).write_text(json.dumps({"synthetic": True, "purpose": "desktop-scenarios-v1"}), encoding="utf-8")
    service = LocalService(root)
    try:
        service.connect_demo()
        service.store.update_chat(CHATS[0][0], sync_enabled=True, analysis_enabled=False, display_name="隔离验收 · 项目协作")
        service.pause()
    finally:
        service.close()
    with database(root / "application.sqlite3") as db:
        db.execute("""CREATE TRIGGER gui_check_deny_message BEFORE INSERT ON messages
                      BEGIN SELECT RAISE(ABORT, 'synthetic_gui_storage_failure'); END""")
    return root


def status(root):
    with closing(sqlite3.connect(root / "application.sqlite3")) as db:
        settings = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM settings WHERE key IN ('paused','last_sync','source')")}
        source = settings["source"]
        account = source["account_id"] if source else ""
        messages = db.execute("SELECT count(*) FROM messages WHERE account_id=?", (account,)).fetchone()[0]
        all_messages = db.execute("SELECT count(*) FROM messages").fetchone()[0]
        project_integrity = list(db.execute(
            "SELECT count(*),count(DISTINCT local_id),min(CAST(local_id AS INTEGER)),max(CAST(local_id AS INTEGER)) FROM messages WHERE account_id=? AND chat_id=?",
            (account, CHATS[0][0]),
        ).fetchone())
        cursors = [list(row) for row in db.execute("SELECT chat_id,local_id FROM cursors WHERE account_id=?", (account,))]
        selected_chats = [row[0] for row in db.execute("SELECT chat_id FROM chats WHERE account_id=? AND sync_enabled=1 ORDER BY chat_id", (source["account_id"],))] if source else []
        runs = db.execute("SELECT count(*) FROM sync_runs").fetchone()[0]
        fault = db.execute("SELECT count(*) FROM sqlite_master WHERE name='gui_check_deny_message'").fetchone()[0] == 1
        chat_fault = db.execute("SELECT count(*) FROM sqlite_master WHERE name='gui_check_deny_chat'").fetchone()[0] == 1
    return {"directory": str(root), "synthetic": True, "source_mode": source["mode"] if source else None, "account_id": source["account_id"] if source else None, "self_id": source.get("self_id") if source else None, "selected_chats": selected_chats, "messages": messages, "stored_messages_all_accounts": all_messages, "project_integrity": project_integrity, "cursors": cursors, "runs": runs, "paused": settings["paused"], "last_sync": settings["last_sync"], "fault_active": fault, "chat_fault_active": chat_fault}


def set_chat_fault(directory, enabled):
    root = validate(directory)
    if type(enabled) is not bool:
        raise ValueError("Synthetic chat fault requires a boolean")
    with database(root / "application.sqlite3") as db:
        if enabled:
            db.execute("""CREATE TRIGGER IF NOT EXISTS gui_check_deny_chat BEFORE UPDATE ON chats
                          BEGIN SELECT RAISE(ABORT, 'synthetic_gui_chat_failure'); END""")
        else:
            db.execute("DROP TRIGGER IF EXISTS gui_check_deny_chat")
    return root


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create", "status", "fault-off", "grow", "chat-fault-on", "chat-fault-off"))
    parser.add_argument("--directory")
    parser.add_argument("--count", type=int, default=100000)
    args = parser.parse_args()
    root = create() if args.action == "create" else validate(args.directory)
    if args.action == "fault-off":
        with database(root / "application.sqlite3") as db:
            db.execute("DROP TRIGGER IF EXISTS gui_check_deny_message")
    if args.action in {"chat-fault-on", "chat-fault-off"}:
        set_chat_fault(root, args.action == "chat-fault-on")
    if args.action == "grow":
        if not 1 <= args.count <= 250000:
            raise ValueError("Synthetic count must be 1..250000")
        with database(root / "demo-source-v1/message/message_0.db") as db:
            table = table_name(CHATS[0][0])
            latest = db.execute(f'SELECT MAX(local_id) FROM "{table}"').fetchone()[0]
            timestamp = int(time.time()) - 1
            db.executemany(f'INSERT INTO "{table}" VALUES(?,2,?,1,0,?)',
                ((latest + index, timestamp, f"合成桌面批次 #{latest + index}：" + "测试原文。" * 100) for index in range(1, args.count + 1)))
    print(json.dumps(status(root), ensure_ascii=False))


if __name__ == "__main__":
    main()
