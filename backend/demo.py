"""Owned synthetic SQLite source. No imports from tests or private runtime paths."""

from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import time

from backend.contracts import ServiceError


ACCOUNT = "synthetic-demo-v1"
SELF = "demo_self"
CHATS = [
    ("demo_project@chatroom", "演示 · 项目协作"),
    ("demo_peer", "演示 · 小林"),
    ("demo_notes@chatroom", "演示 · 资料讨论"),
    ("demo_missing@chatroom", "演示 · 不兼容来源"),
]


def table_name(chat):
    return "Msg_" + hashlib.md5(chat.encode()).hexdigest()


@contextmanager
def database(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def ensure_demo(directory, *, clock=time.time):
    root = Path(directory).resolve() / "demo-source-v1"
    marker = root / "synthetic.sqlite3"
    if marker.is_file():
        with database(marker) as db:
            created = db.execute("SELECT created_at FROM demo_metadata WHERE version=1").fetchone()
        if not created:
            raise ServiceError("demo_invalid", "演示来源版本不匹配，请保留数据目录后重试。")
        return root, created[0]
    # Never overwrite an existing unmarked directory.
    if root.exists():
        raise ServiceError("demo_invalid", "演示目录已存在但无法确认归属，未覆盖任何文件。")
    for folder in ("message", "contact", "session"):
        (root / folder).mkdir(parents=True, mode=0o700)
    created = int(clock())
    with database(root / "contact/contact.db") as db:
        db.execute("CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT)")
        db.executemany("INSERT INTO contact VALUES(?,?,?)", [(uid, name, "") for uid, name in CHATS] + [(SELF, "演示 · 我", ""), ("demo_peer", "演示 · 小林", "")])
    with database(root / "session/session.db") as db:
        db.execute("CREATE TABLE SessionTable(username TEXT,last_timestamp INTEGER)")
        db.executemany("INSERT INTO SessionTable VALUES(?,?)", [(uid, created) for uid, _ in CHATS])
    with database(root / "message/message_0.db") as db:
        db.execute("CREATE TABLE Name2Id(user_name TEXT)")
        db.executemany("INSERT INTO Name2Id VALUES(?)", [(SELF,), ("demo_peer",)])
        for chat, _ in CHATS[:3]:
            db.execute(f'CREATE TABLE "{table_name(chat)}"(local_id INTEGER PRIMARY KEY,real_sender_id INTEGER,create_time INTEGER,local_type INTEGER,status INTEGER,message_content TEXT)')
        project = [
            (1, 2, created - 8 * 86400, 1, 0, "合成：八天前的消息，不应进入首次七天同步。"),
            (2, 1, created - 1800, 1, 0, "合成：我明天下午把方案发给你。仅供原文验证，不代表已经生成待办。"),
            (3, 2, created - 1200, 1, 0, "合成测试原文。" * 400),
            (4, 999, created - 600, 1, 0, "合成：这条消息无法确定发送人，不能冒充会话名称。"),
            (5, 2, created - 300, 3, 0, "合成图片占位；没有可理解的图片内容"),
        ]
        db.executemany(f'INSERT INTO "{table_name(CHATS[0][0])}" VALUES(?,?,?,?,?,?)', project)
        db.executemany(f'INSERT INTO "{table_name(CHATS[1][0])}" VALUES(?,?,?,?,?,?)', [(1, 2, created - 900, 1, 0, "合成：材料已放在共享目录。"), (2, 1, created - 800, 1, 0, "合成：收到，我会确认版本。")])
        db.execute(f'INSERT INTO "{table_name(CHATS[2][0])}" VALUES(1,2,?,1,0,?)', (created - 700, "合成：这里只读取文字，不调用模型分析。"))
    with database(marker) as db:
        db.execute("CREATE TABLE demo_metadata(version INTEGER PRIMARY KEY,created_at INTEGER)")
        db.execute("INSERT INTO demo_metadata VALUES(1,?)", (created,))
    return root, created


def append_demo(directory, source, *, clock=time.time):
    expected = Path(directory).resolve() / "demo-source-v1"
    if not source or source.get("mode") != "demo" or Path(source.get("root", "")).resolve() != expected:
        raise ServiceError("demo_only", "追加演示消息只适用于本应用创建的合成来源。")
    root, _ = ensure_demo(directory)
    with database(root / "message/message_0.db") as db:
        table = table_name(CHATS[0][0])
        local_id = db.execute(f'SELECT MAX(local_id) FROM "{table}"').fetchone()[0] + 1
        db.execute(f'INSERT INTO "{table}" VALUES(?,2,?,1,0,?)', (local_id, int(clock()), f"合成新增消息 #{local_id}：用于验证增量、暂停与重启。"))
    return {"chat_id": CHATS[0][0], "local_id": str(local_id)}
