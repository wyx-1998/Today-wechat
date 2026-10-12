"""Explicit single-chat local check with the final bundled reader.

Never starts the production service or copies model credentials/grants. The
private result directory remains available for inspection; no message bodies,
sender IDs, source paths or keys are printed. CLI chat selection must be exact.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import tempfile
import time

from backend.contracts import iso_time, parse_time
from backend.storage import Store
from tests.product_data_check import snapshot


def public_round(report):
    return {key: report.get(key) for key in ("outcome", "inserted", "duration_ms", "preparation")} | {
        "completed_chats": len(report.get("completed_chats", [])),
        "errors": [error.get("code") for error in report.get("errors", [])]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--chat-name", required=True)
    parser.add_argument("--days", type=int, default=15)
    args = parser.parse_args()
    if not 1 <= args.days <= 30:
        raise ValueError("explicit window must be 1–30 days")
    production = Path(args.database).resolve(strict=True)
    binary = Path(args.binary).resolve(strict=True)
    with closing(sqlite3.connect(production.as_uri() + "?mode=ro", uri=True)) as db:
        before = snapshot(db)
        state = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM settings")}
        if not state["paused"] or not state["source"] or state["source"].get("mode") != "encrypted":
            raise ValueError("production must remain paused with an encrypted source")
        source = state["source"]
        rows = db.execute("SELECT chat_id,chat_name,chat_type FROM chats WHERE account_id=? AND (chat_name=? OR display_name=?)",
            (source["account_id"], args.chat_name, args.chat_name)).fetchall()
        if len(rows) != 1 or rows[0][2] != "group":
            raise ValueError("an exact, unique group is required")
        chat = dict(zip(("chat_id", "chat_name", "chat_type"), rows[0]))
    directory = Path(tempfile.mkdtemp(prefix="wechat-local-sync-private-"))
    directory.chmod(0o700)
    app = directory / "app"
    start = iso_time(time.time() - args.days * 86400)
    isolated = Store(app)
    isolated.configure(source, start, [chat])
    isolated.update_chat(chat["chat_id"], sync_enabled=True, analysis_enabled=False)
    rounds = []
    timer_started = time.monotonic()
    # Only non-secret paths and the exact authorized chat go to this worker.
    # Fresh storage has no model configuration, credentials or cloud grant.
    for cycle in range(2):
        worker = subprocess.Popen([str(binary), "--data-dir", str(app)], cwd=directory,
            env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "TMPDIR": str(directory)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1)
        counter = 0
        try:
            def request(command):
                nonlocal counter
                counter += 1
                worker.stdin.write(json.dumps({"version": 1, "id": str(counter), "command": command, "params": {}}) + "\n")
                worker.stdin.flush()
                deadline = time.monotonic() + 180
                with selectors.DefaultSelector() as selector:
                    selector.register(worker.stdout, selectors.EVENT_READ)
                    while not selector.select(timeout=10):
                        if time.monotonic() > deadline:
                            raise TimeoutError("bounded real sync check timeout")
                        print(json.dumps({"waiting": command, "elapsed_seconds": round(time.monotonic() - timer_started, 1)}), flush=True)
                reply = json.loads(worker.stdout.readline())
                if reply.get("id") != str(counter) or "error" in reply:
                    raise RuntimeError("bundled request failed: " + reply.get("error", {}).get("code", "protocol"))
                return reply["result"]
            report = request("sync.now")
            rounds.append({"stage": "initial" if cycle == 0 else "after_restart", **public_round(report)})
            print(json.dumps(rounds[-1], ensure_ascii=False), flush=True)
            if report["outcome"] != "success":
                break
            if cycle == 0:
                report = request("sync.now")
                rounds.append({"stage": "repeat", **public_round(report)})
                print(json.dumps(rounds[-1], ensure_ascii=False), flush=True)
            request("sync.pause")
            worker.stdin.close()
            if worker.wait(timeout=15) != 0:
                raise RuntimeError("bundled worker did not exit cleanly")
            if worker.stderr.read():
                raise RuntimeError("unexpected worker diagnostics; content withheld")
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=15)
            for stream in (worker.stdin, worker.stdout, worker.stderr):
                if stream and not stream.closed:
                    stream.close()
        if cycle == 0:
            isolated.set_paused(False)  # Only the owned isolated DB, not production.
    with closing(sqlite3.connect(isolated.path.as_uri() + "?mode=ro", uri=True)) as db:
        count, distinct, first, last = db.execute("SELECT COUNT(*),COUNT(DISTINCT message_id),MIN(sent_epoch),MAX(sent_epoch) FROM messages").fetchone()
        outside = db.execute("SELECT COUNT(*) FROM messages WHERE account_id<>? OR chat_id<>? OR sent_epoch<?",
            (source["account_id"], chat["chat_id"], parse_time(start))).fetchone()[0]
        types = dict(db.execute("SELECT message_type,COUNT(*) FROM messages GROUP BY message_type"))
        senders = db.execute("SELECT SUM(sender_id IS NOT NULL),SUM(sender_id IS NULL),SUM(is_self=1) FROM messages").fetchone()
        model_calls = db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
        configs = db.execute("SELECT COUNT(*) FROM model_configs").fetchone()[0]
        grants = db.execute("SELECT COUNT(*) FROM text_grants").fetchone()[0]
        enabled = db.execute("SELECT COUNT(*),SUM(analysis_enabled) FROM chats WHERE sync_enabled=1").fetchone()
        successful = [json.loads(row[0]) for row in db.execute("SELECT report_json FROM sync_runs WHERE outcome='success'")]
        assert all(db.execute("SELECT COUNT(*) FROM messages WHERE sent_epoch>=?", (parse_time(r["time_end"]),)).fetchone()[0] == 0
                   for r in successful if r["inserted"] == count), "messages outside frozen cutoff"
        assert count == distinct and outside == 0
        assert model_calls == configs == grants == 0 and enabled == (1, 0)
    with closing(sqlite3.connect(production.as_uri() + "?mode=ro", uri=True)) as db:
        after = snapshot(db)
    assert before == after, "production changed during read-only check"
    temporary_trees = len(list(app.glob(".wechat-source-set-*")))
    assert temporary_trees == 0, "temporary decrypted tree retained"
    all_success = len(rounds) == 3 and all(r["outcome"] == "success" for r in rounds)
    result = {"chat_name": args.chat_name, "window_days": args.days, "time_start": start,
        "local_sync_success": all_success, "rounds": rounds, "messages": count,
        "unique_messages": distinct, "types": types,
        "first_message": iso_time(first) if first is not None else None,
        "last_message": iso_time(last) if last is not None else None,
        "sender_known": senders[0] or 0, "sender_unknown": senders[1] or 0, "self_messages": senders[2] or 0,
        "model_requests": model_calls, "production_all_tables_unchanged": True,
        "production_paused": after["settings"]["paused"], "temporary_snapshots_remaining": temporary_trees,
        "private_directory": str(directory)}
    # The summary contains no message bodies/keys. Raw results remain private.
    with (directory / "summary.json").open("x", encoding="utf-8") as output:
        os.chmod(directory / "summary.json", 0o600)
        json.dump(result, output, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if not all_success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
