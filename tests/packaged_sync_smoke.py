"""Actual bundled AES capture/skip/restart, with no private data or model setup."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import selectors
import sqlite3
import subprocess
import tempfile

from backend.demo import CHATS, SELF
from tests.encrypted_gui_scenarios import create, append


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    binary = Path(parser.parse_args().binary).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="encrypted-service-synthetic-") as directory:
        fixture = create(directory)
        root = Path(directory)
        original = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / "encrypted").glob("*/*")}
        rounds = []
        for index in range(2):
            worker = subprocess.Popen([str(binary), "--data-dir", fixture["app_directory"]], cwd=directory,
                env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "TMPDIR": directory},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
            counter = 0
            try:
                def request(command, params=None):
                    nonlocal counter
                    counter += 1
                    worker.stdin.write(json.dumps({"version": 1, "id": str(counter), "command": command, "params": params or {}}) + "\n")
                    worker.stdin.flush()
                    with selectors.DefaultSelector() as selector:
                        selector.register(worker.stdout, selectors.EVENT_READ)
                        assert selector.select(timeout=20), "bundled sync timeout"
                    reply = json.loads(worker.stdout.readline())
                    assert reply["id"] == str(counter) and "error" not in reply, "bundled request failed"
                    return reply["result"]
                if index == 0:
                    state = request("source.encrypted", {"root": fixture["source"], "keys_file": fixture["keys_file"], "authorize": True, "self_id": SELF})
                    assert state["message_count"] == 0
                    request("chat.save", {"chat_id": CHATS[0][0], "sync_enabled": True, "analysis_enabled": False})
                    report = request("sync.now")
                    assert report["outcome"] == "success" and report["inserted"] == 4
                    assert not report["preparation"]["skipped"]
                    assert report["preparation"]["captured_databases"] == 3
                    rounds.append(report["preparation"])
                skipped = request("sync.now")
                assert skipped["outcome"] == "success" and skipped["inserted"] == 0
                assert skipped["preparation"]["skipped"]
                assert request("model.calls")["total"] == 0
                if index == 1:
                    for path, digest in original.items():
                        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest, "original was modified"
                    append(root)
                    report = request("sync.now")
                    assert report["outcome"] == "success" and report["inserted"] == 1
                    assert not report["preparation"]["skipped"]
                    assert request("sync.now")["preparation"]["skipped"]
                    assert request("messages.list")["total"] == 5
                    request("sync.pause")
                worker.stdin.close()
                assert worker.wait(timeout=10) == 0
                assert worker.stderr.read() == "", "unexpected bundled diagnostics"
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    worker.wait(timeout=10)
                for stream in (worker.stdin, worker.stdout, worker.stderr):
                    if stream and not stream.closed:
                        stream.close()
            assert not list((root / "app").glob(".wechat-source-set-*")), "temporary snapshots retained"
        with closing(sqlite3.connect(Path(fixture["app_directory"]) / "application.sqlite3")) as db:
            assert db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 5
        print(json.dumps({"packaged_sync": "passed", "source": "isolated_aes", "first_preparation": rounds[0],
            "unchanged_skip": True, "restart_skip": True, "wal_increment": 1, "messages": 5,
            "external_python_required": False, "model_requests": 0, "temporary_snapshots_remaining": 0}))


if __name__ == "__main__":
    main()
