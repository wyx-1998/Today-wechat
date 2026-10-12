"""Owned macOS fixtures for real AES service and actual desktop checks."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import tempfile
import time

from backend.demo import CHATS, SELF, table_name
from contextlib import closing
import sqlite3
from tests.encrypted_refresh_smoke import KEY, PAGE_SIZE, RESERVE, encode_wal, encrypt_page


def build_database(plain, source, schema, wal_sql=""):
    # Public SQLite C API creates reserve=80 sources, independent of production.
    lib = ctypes.CDLL("/usr/lib/libsqlite3.dylib")
    lib.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    lib.sqlite3_file_control.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    lib.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.sqlite3_close.argtypes = [ctypes.c_void_p]
    connection = ctypes.c_void_p()
    if lib.sqlite3_open(os.fsencode(plain), ctypes.byref(connection)) != 0:
        raise RuntimeError("synthetic SQLite open failed")
    try:
        reserved = ctypes.c_int(RESERVE)
        if lib.sqlite3_file_control(connection, b"main", 38, ctypes.byref(reserved)) != 0:
            raise RuntimeError("synthetic reserve setup failed")
        def execute(sql):
            if lib.sqlite3_exec(connection, sql.encode(), None, None, None) != 0:
                raise RuntimeError("synthetic fixture SQL failed")
        execute("PRAGMA page_size=4096; PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;" + schema + "PRAGMA wal_checkpoint(TRUNCATE);")
        base = plain.read_bytes()
        source.write_bytes(b"".join(encrypt_page(base[offset:offset + PAGE_SIZE], offset // PAGE_SIZE + 1)
                                    for offset in range(0, len(base), PAGE_SIZE)))
        if wal_sql:
            execute(wal_sql)
            Path(str(source) + "-wal").write_bytes(encode_wal(Path(str(plain) + "-wal").read_bytes()))
    finally:
        if lib.sqlite3_close(connection) != 0:
            raise RuntimeError("synthetic SQLite close failed")
    source.chmod(0o600)


def create(directory=None):
    root = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="wechat-encrypted-gui-check-"))
    root = root.resolve()
    root.mkdir(exist_ok=True, mode=0o700)
    (root / ".owned-encrypted-fixture.json").write_text('{"synthetic":true,"purpose":"encrypted-gui-v1"}')
    encrypted = root / "encrypted"
    for folder in ("message", "contact", "session"):
        (encrypted / folder).mkdir(parents=True, mode=0o700)
    (root / "plain").mkdir(mode=0o700)
    now = int(time.time())
    contacts = "CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT);"
    for uid, name in CHATS + [(SELF, "合成 · 我")]:
        contacts += f"INSERT INTO contact VALUES('{uid}','合成加密 · {name}','');"
    build_database(root / "plain/contact.db", encrypted / "contact/contact.db", contacts)
    sessions = "CREATE TABLE SessionTable(username TEXT);" + "".join(f"INSERT INTO SessionTable VALUES('{uid}');" for uid, _ in CHATS)
    build_database(root / "plain/session.db", encrypted / "session/session.db", sessions)
    schema = "CREATE TABLE Name2Id(user_name TEXT);INSERT INTO Name2Id VALUES('demo_self'),('demo_peer');"
    for uid, _ in CHATS[:3]:
        schema += f'CREATE TABLE "{table_name(uid)}"(local_id INTEGER PRIMARY KEY,real_sender_id INTEGER,create_time INTEGER,local_type INTEGER,status INTEGER,message_content TEXT);'
    project = table_name(CHATS[0][0])
    schema += f"INSERT INTO \"{project}\" VALUES(1,2,{now - 8 * 86400},1,0,'合成：八天前，不应保存');"
    wal = f"INSERT INTO \"{project}\" VALUES(2,1,{now - 1800},1,0,'合成加密：我明天发方案'),(3,2,{now - 1200},1,0,'{'合成加密。' * 560}'),(4,999,{now - 600},1,0,'合成加密：未知发送人'),(5,2,{now - 300},3,0,'合成图片类型');"
    build_database(root / "plain/message.db", encrypted / "message/message_0.db", schema, wal)
    values = {str(path.relative_to(encrypted)): {"enc_key": KEY.hex()} for path in encrypted.glob("*/*.db")}
    key_file = root / "synthetic-keys.json"
    key_file.write_text(json.dumps(values))
    key_file.chmod(0o600)
    wrong = root / "synthetic-wrong-keys.json"
    wrong.write_text(json.dumps({relative: {"enc_key": bytes(reversed(KEY)).hex()} for relative in values}))
    wrong.chmod(0o600)
    return {"synthetic": True, "directory": str(root), "source": str(encrypted), "keys_file": str(key_file),
            "wrong_keys_file": str(wrong), "app_directory": str(root / "app"), "self_id": SELF}


def append(directory):
    root = Path(directory).resolve(strict=True)
    if (root.parent != Path(tempfile.gettempdir()).resolve()
            or not root.name.startswith(("wechat-encrypted-gui-check-", "encrypted-service-synthetic-"))
            or json.loads((root / ".owned-encrypted-fixture.json").read_text()) != {"synthetic": True, "purpose": "encrypted-gui-v1"}):
        raise ValueError("only this test's owned temporary fixture is allowed")
    plain = root / "plain/message.db"
    source = root / "encrypted/message/message_0.db"
    if any(path.is_symlink() for path in (plain, source, plain.parent, source.parent)):
        raise ValueError("linked fixture paths are not allowed")
    table = table_name(CHATS[0][0])
    with closing(sqlite3.connect(plain.as_uri() + "?mode=ro", uri=True)) as db:
        next_id = db.execute(f'SELECT MAX(local_id) FROM "{table}"').fetchone()[0] + 1
    build_database(plain, source, "", f"INSERT INTO \"{table}\" VALUES({next_id},2,{int(time.time()) - 1},1,0,'合成加密增量 #{next_id}');")
    return {"synthetic": True, "directory": str(root), "local_id": next_id}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create or append only owned synthetic encrypted GUI data")
    parser.add_argument("--append", help="Exact owned synthetic directory; never a private source")
    args = parser.parse_args()
    print(json.dumps(append(args.append) if args.append else create(), ensure_ascii=False))
