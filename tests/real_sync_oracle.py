"""Independent SQL membership check against a real sync's frozen time window.

Only accepts the owned private single-chat directory created by real_sync_check.
No model, production service, private message body output or source writes.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from backend.contracts import parse_time, stable_id
from backend.encrypted_source import encrypted_reader
from backend.reader import source_database, quote_identifier


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    directory = Path(args.directory).resolve(strict=True)
    if directory.parent != Path(tempfile.gettempdir()).resolve() or not directory.name.startswith("wechat-local-sync-private-"):
        raise ValueError("owned isolated directory required")
    path = directory / "app/application.sqlite3"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        state = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM settings")}
        chats = db.execute("SELECT account_id,chat_id,analysis_enabled FROM chats WHERE sync_enabled=1").fetchall()
        if not state["paused"] or len(chats) != 1 or chats[0][2] or db.execute("SELECT COUNT(*) FROM model_configs").fetchone()[0]:
            raise ValueError("paused single-chat local-only store required")
        report = json.loads(db.execute("SELECT report_json FROM sync_runs WHERE outcome='success' ORDER BY started_at LIMIT 1").fetchone()[0])
        saved = {row[0]: tuple(row[1:]) for row in db.execute("SELECT message_id,stream_id,local_id,sent_epoch FROM messages")}
        start, end = parse_time(report["time_start"]), parse_time(report["time_end"])
    account, chat, _ = chats[0]
    expected = {}
    raw_count = 0
    streams_count = 0
    begun = time.monotonic()
    with encrypted_reader(state["source"], directory / "app") as reader:
        same_versions = reader.source_manifest == state["encrypted_sync_checkpoint"]["source_manifest"]
        for stream_id, source, table in reader.streams(chat):
            streams_count += 1
            with source_database(source) as db:
                cols = {row[1] for row in db.execute(f"PRAGMA table_info({quote_identifier(table)})")}
                local = next((col for col in ("local_id", "MsgLocalID") if col in cols), "rowid")
                timing = next((col for col in ("create_time", "msg_time", "CreateTime") if col in cols), None)
                server = next((col for col in ("server_id", "MsgSvrID") if col in cols), None)
                if timing is None:
                    raise ValueError("source timestamp schema unavailable")
                fields = f'{quote_identifier(local)},' + (quote_identifier(server) if server else "NULL") + f',{quote_identifier(timing)}'
                # Direct SQL over the full frozen window, without a saved cursor,
                # SourceReader.read_batch, pagination or the application's rows.
                rows = db.execute(f"SELECT {fields} FROM {quote_identifier(table)} WHERE {quote_identifier(timing)}>=? AND {quote_identifier(timing)}<?", (start, end)).fetchall()
                raw_count += len(rows)
                for local_id, server_id, timestamp in rows:
                    identity = ("server", str(server_id)) if server_id not in (None, 0, "0", "") else ("local", stream_id, str(local_id))
                    key = stable_id(account, chat, *identity)
                    expected.setdefault(key, (stream_id, str(local_id), float(timestamp)))
        metrics = reader.preparation_metrics
    result = {"source_rows": raw_count, "source_unique_messages": len(expected), "saved_messages": len(saved),
        "source_streams": streams_count, "missing_messages": len(expected.keys() - saved.keys()),
        "extra_messages": len(saved.keys() - expected.keys()),
        "metadata_mismatches": sum(saved[key] != value for key, value in expected.items() if key in saved),
        "accepted_source_versions_match": same_versions,
        "duration_ms": round((time.monotonic() - begun) * 1000, 3), "preparation": metrics}
    with (directory / "oracle.json").open("x", encoding="utf-8") as output:
        (directory / "oracle.json").chmod(0o600)
        json.dump(result, output, ensure_ascii=False, indent=2)
    print(json.dumps(result), flush=True)
    assert not result["missing_messages"] and not result["extra_messages"] and not result["metadata_mismatches"]
    assert same_versions, "different captured file versions: not a same-snapshot completeness check"
    assert not list((directory / "app").glob(".wechat-source-set-*"))


if __name__ == "__main__":
    main()
