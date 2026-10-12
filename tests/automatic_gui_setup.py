"""Owns generated GUI fixtures only. No private input, network or OS keychain."""

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from backend.contracts import iso_time
from backend.service import LocalService
from tests.model_smoke import MemoryKeychain, FakeClient, KEY

PREFIX = "wechat-today-auto-gui-"
OWNER = "generated-automatic-gui-v1"
ACCOUNT = "generated-gui-account"


def owned(directory):
    path = Path(directory).absolute()
    if path.is_symlink() or path.parent.resolve() != Path(tempfile.gettempdir()).resolve() or not path.name.startswith(PREFIX):
        raise ValueError("Only this helper generated directory is allowed")
    path = path.resolve(strict=True)
    database = path / "application.sqlite3"
    if database.is_symlink() or database.stat().st_nlink != 1:
        raise ValueError("Invalid fixture database")
    with closing(sqlite3.connect(database.as_uri()+"?mode=ro", uri=True)) as db:
        row = db.execute("SELECT value FROM settings WHERE key='gui_fixture_owner'").fetchone()
    if not row or json.loads(row[0]) != OWNER:
        raise ValueError("Not an owned GUI fixture")
    return path


def create():
    root = Path(tempfile.mkdtemp(prefix=PREFIX))
    source = root / "source"
    contacts = [("fictional-review", "资料协作"), ("fictional-plan", "工作安排")]
    for folder in ("contact", "session", "message"):
        (source / folder).mkdir(parents=True)
    with closing(sqlite3.connect(source / "contact/contact.db")) as db:
        db.execute("CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT)")
        db.executemany("INSERT INTO contact VALUES(?,?,?)", [(uid, name, name) for uid,name in contacts]); db.commit()
    with closing(sqlite3.connect(source / "session/session.db")) as db:
        db.execute("CREATE TABLE SessionTable(username TEXT)")
        db.executemany("INSERT INTO SessionTable VALUES(?)", [(uid,) for uid,_ in contacts]); db.commit()
    now = time.time()
    with closing(sqlite3.connect(source / "message/message_0.db")) as db:
        for uid,_ in contacts:
            table = "Msg_" + hashlib.md5(uid.encode()).hexdigest()
            db.execute(f"CREATE TABLE {table}(local_id INTEGER,create_time REAL,message_content TEXT,local_type INTEGER)")
        db.commit()
    service = LocalService(root, keychain=MemoryKeychain(), model_client=FakeClient())
    try:
        service.models.save(base_url="https://synthetic.invalid", model="generated-flash", api_key=KEY)
        service.store.configure({"mode":"local","account_id":ACCOUNT,"root":str(source),"self_id":None},
            iso_time(now-7*86400), [{"chat_id":uid,"chat_name":name,"chat_type":"direct"} for uid,name in contacts])
        service.pause()
        service.store.update_chat(contacts[0][0], sync_enabled=True, analysis_enabled=False)
        with service.store.database() as db:
            service.store._set(db, "gui_fixture_owner", OWNER)
            for i,mid in enumerate(("previous", "next-1", "next-2")):
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (mid,ACCOUNT,contacts[0][0],"generated-stream",str(i),
                    None,None,None,iso_time(now-120+i),now-120+i,"text","明天发送资料。", "text_available",1))
            db.execute("INSERT INTO analysis_runs VALUES(?,?,?,?,?,?,?,?,?)", ("historical",ACCOUNT,'["previous"]',1,1,"success",iso_time(now-100),iso_time(now-90),None))
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("historical-task",ACCOUNT,"historical","发送资料","unknown",None,None,"unknown","needs_confirmation",'["previous"]',iso_time(now-90),1))
    finally:
        service.close()
    return {"directory":str(root), "generated_only":True, "paused":True, "model_requests":0}


def inspect(directory):
    root = owned(directory)
    with closing(sqlite3.connect(root / "application.sqlite3")) as db:
        return {"generated_only":True,"paused":json.loads(db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0]),
            "messages":db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "model_requests":db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0],
            "grants":db.execute("SELECT status FROM text_grants ORDER BY rowid").fetchall(),
            "tasks":db.execute("SELECT task_id,status,revision FROM tasks ORDER BY task_id").fetchall()}


def add_result(directory):
    root = owned(directory)
    now = iso_time(time.time())
    with closing(sqlite3.connect(root / "application.sqlite3")) as db:
        db.execute("INSERT OR IGNORE INTO analysis_runs VALUES(?,?,?,?,?,?,?,?,?)", ("added-result",ACCOUNT,'["next-1"]',1,1,"success",now,now,None))
        db.execute("INSERT OR IGNORE INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("added-task",ACCOUNT,"added-result","确认资料接收","unknown",None,None,"unknown","needs_confirmation",'["next-1"]',now,1))
        db.commit()
    return inspect(directory)


def add_changes(directory):
    """Generate pending UI proposals only; never pretends a model was called."""
    root = owned(directory)
    now = time.time()
    with closing(sqlite3.connect(root / "application.sqlite3")) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != 12:
            raise ValueError("Open the fixture with the current application before adding changes")
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR IGNORE INTO analysis_runs VALUES(?,?,?,?,?,?,?,?,?)", (
            "generated-changes",ACCOUNT,'["previous","next-1","next-2"]',1,1,"success",iso_time(now),iso_time(now),None))
        proposals = (
            ("historical-task", "details", {"title":"发送完整资料清单","owner_type":"unknown","due_at":None}, "previous"),
            ("generated-received-task", "completed", {"status":"completed"}, "next-1"),
            ("generated-cancelled-task", "cancelled", {"status":"cancelled"}, "next-2"),
        )
        for task_id, kind, proposed, mid in proposals:
            if task_id != "historical-task":
                db.execute("INSERT OR IGNORE INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                    task_id,ACCOUNT,"generated-changes","确认资料接收" if kind == "completed" else "安排资料沟通",
                    "unknown",None,None,"unknown","needs_confirmation",json.dumps([mid]),iso_time(now),1))
            db.execute("INSERT OR IGNORE INTO task_evidence_links VALUES(?,?,?)", (task_id,"generated-changes",json.dumps([mid])))
            db.execute("INSERT OR IGNORE INTO task_changes VALUES(?,?,?,?,?,?,?,?,?)", (
                "generated-"+kind,task_id,"generated-changes",kind,json.dumps(proposed),json.dumps([mid]),"pending",iso_time(now),None))
        db.commit()
    return inspect(directory) | {"changes_generated_without_model":True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create","inspect","add-result","add-changes"))
    parser.add_argument("--directory")
    args = parser.parse_args()
    if args.action == "create":
        result = create()
    else:
        if not args.directory:
            parser.error("--directory is required")
        result = {"inspect":inspect,"add-result":add_result,"add-changes":add_changes}[args.action](args.directory)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
