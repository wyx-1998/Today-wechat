"""Replay a historical prefix of authorized real data in a new private store.

This is real-message historical incremental catch-up, not proof of receiving a
new live message. Original full results, production and WeChat stay untouched.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import selectors
import sqlite3
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--binary", required=True)
    args = parser.parse_args()
    directory = Path(args.directory).resolve(strict=True)
    binary = Path(args.binary).resolve(strict=True)
    if directory.parent != Path(tempfile.gettempdir()).resolve() or not directory.name.startswith("wechat-local-sync-private-"):
        raise ValueError("owned single-chat directory required")
    original = directory / "app/application.sqlite3"
    destination = directory / "app-increment"
    destination.mkdir(mode=0o700)
    target = destination / "application.sqlite3"
    with target.open("xb"):
        target.chmod(0o600)
    with closing(sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)) as source:
        state = {key: json.loads(value) for key, value in source.execute("SELECT key,value FROM settings")}
        assert state["paused"]
        assert source.execute("SELECT COUNT(*) FROM model_configs").fetchone()[0] == 0
        assert source.execute("SELECT COUNT(*) FROM text_grants").fetchone()[0] == 0
        assert source.execute("SELECT COUNT(*) FROM chats WHERE sync_enabled=1").fetchone()[0] == 1
        reference = {row[0]: tuple(row[1:]) for row in source.execute("SELECT message_id,stream_id,local_id,sent_epoch FROM messages")}
        with closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
    assert len(reference) > 1, "need real historical data for prefix replay"
    with closing(sqlite3.connect(target)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        for (stream,) in db.execute("SELECT DISTINCT stream_id FROM messages").fetchall():
            local_ids = [int(row[0]) for row in db.execute("SELECT local_id FROM messages WHERE stream_id=? ORDER BY CAST(local_id AS INTEGER)", (stream,))]
            frontier = local_ids[(len(local_ids) - 1) // 2]
            # Delete derived rows only from this new disposable copy. The
            # original full-results DB remains available for reference/GUI.
            db.execute("DELETE FROM messages WHERE stream_id=? AND CAST(local_id AS INTEGER)>?", (stream, frontier))
            db.execute("UPDATE cursors SET local_id=? WHERE stream_id=?", (str(frontier), stream))
        prefix_count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        db.execute("UPDATE settings SET value='false' WHERE key='paused'")
        # Keep the old checkpoint: changed count/cursors MUST prevent skipping.
    worker = subprocess.Popen([str(binary), "--data-dir", str(destination)], cwd=directory,
        env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "TMPDIR": str(directory)},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
    counter = 0
    try:
        def request(command):
            nonlocal counter
            counter += 1
            worker.stdin.write(json.dumps({"version": 1, "id": str(counter), "command": command, "params": {}}) + "\n")
            worker.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(worker.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=60), "historical catch-up timeout"
            reply = json.loads(worker.stdout.readline())
            assert reply.get("id") == str(counter) and "error" not in reply, "historical catch-up request failed"
            return reply["result"]
        report = request("sync.now")
        repeat = request("sync.now")
        request("sync.pause")
        worker.stdin.close()
        assert worker.wait(timeout=15) == 0 and not worker.stderr.read()
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=15)
        for stream in (worker.stdin, worker.stdout, worker.stderr):
            if stream and not stream.closed:
                stream.close()
    with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as db:
        saved = {row[0]: tuple(row[1:]) for row in db.execute("SELECT message_id,stream_id,local_id,sent_epoch FROM messages")}
        model_calls = db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
        paused = json.loads(db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0])
    with closing(sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)) as db:
        unchanged = {row[0]: tuple(row[1:]) for row in db.execute("SELECT message_id,stream_id,local_id,sent_epoch FROM messages")} == reference
    result = {"mode": "real_historical_increment", "prefix_messages": prefix_count,
        "inserted": report["inserted"], "saved_messages": len(saved), "reference_messages": len(reference),
        "outcome": report["outcome"], "duration_ms": report["duration_ms"], "preparation": report.get("preparation"),
        "missing_messages": len(reference.keys() - saved.keys()), "extra_messages": len(saved.keys() - reference.keys()),
        "metadata_mismatches": sum(saved[key] != value for key, value in reference.items() if key in saved),
        "repeat_inserted": repeat["inserted"], "repeat_skipped": repeat.get("preparation", {}).get("skipped"),
        "model_requests": model_calls, "original_full_results_unchanged": unchanged,
        "paused": paused, "temporary_snapshots_remaining": len(list(destination.glob(".wechat-source-set-*")))}
    with (directory / "increment.json").open("x", encoding="utf-8") as output:
        (directory / "increment.json").chmod(0o600)
        json.dump(result, output, ensure_ascii=False, indent=2)
    print(json.dumps(result), flush=True)
    assert report["outcome"] == "success" and not report["preparation"]["skipped"]
    assert report["inserted"] == len(saved) - prefix_count
    assert not result["missing_messages"] and not result["extra_messages"] and not result["metadata_mismatches"]
    assert not repeat["inserted"] and model_calls == 0 and unchanged and paused
    assert result["temporary_snapshots_remaining"] == 0


if __name__ == "__main__":
    main()
