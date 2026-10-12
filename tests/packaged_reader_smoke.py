"""Verify the bundled reader outside the repo, without a Python executable on PATH."""

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
    parser.add_argument("--binary", required=True)
    args = parser.parse_args()
    binary = Path(args.binary).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="today-packaged-synthetic-") as directory:
        # No virtual environment, repo cwd, PYTHONPATH, or externally installed zstandard.
        env = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "TMPDIR": directory}
        worker = subprocess.Popen(
            [str(binary), "--data-dir", directory], cwd=directory, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        counter = 0
        try:
            def request(command, params=None):
                nonlocal counter
                counter += 1
                identifier = str(counter)
                worker.stdin.write(json.dumps({"version": 1, "id": identifier, "command": command, "params": params or {}}) + "\n")
                worker.stdin.flush()
                with selectors.DefaultSelector() as selector:
                    selector.register(worker.stdout, selectors.EVENT_READ)
                    assert selector.select(timeout=15), "bundled reader response timeout"
                response = json.loads(worker.stdout.readline())
                assert response["version"] == 1 and response["id"] == identifier
                assert "error" not in response, response.get("error")
                return response["result"]

            initial = request("source.demo")
            assert initial["message_count"] == 0
            assert all(not chat["sync_enabled"] for chat in initial["chats"])
            assert request("sync.now")["outcome"] == "no_selection"
            request("chat.save", {"chat_id": "demo_project@chatroom", "sync_enabled": True, "analysis_enabled": False})
            assert request("sync.now")["inserted"] == 4
            messages = request("messages.list")["items"]
            assert request("messages.chats", {"query": "demo_project", "limit": 25})["total"] == 1
            ascending = request("messages.list", {"chat_id": "demo_project@chatroom", "order": "asc", "limit": 2})
            assert ascending["total"] == 4 and ascending["has_more"]
            assert ascending["saved_total"] == 4 and ascending["senders"]
            self_id = next(item["sender_id"] for item in messages if item["effective_is_self"] is True)
            only_self = request("messages.list", {"chat_id": "demo_project@chatroom", "sender_id": self_id, "order": "asc"})
            assert only_self["total"] == 1 and all(item["effective_is_self"] for item in only_self["items"])
            long_message = next(message for message in messages if message["local_id"] == "3")
            assert len(request("message.detail", {"message_id": long_message["message_id"]})["content"]) == 2800
            assert request("sync.now")["inserted"] == 0
            request("sync.pause")
            assert request("state")["paused"] is True
            assert request("today.list")["attention_items"] == []
            assert "attention_items" not in request("tasks.list")
            tag = request("tag.save", {"name": "隔离批量标签"})["tag_id"]
            preview = request("chat.bulk.preview", {"chat_ids": ["demo_project@chatroom"], "operation": "tags_add", "tag_ids": [tag]})
            assert preview["change"] == 1
            assert request("chat.bulk.apply", {"preview_token": preview["preview_token"], "confirm": True})["changed"] == 1
            assert request("state")["paused"] is True
            assert request("model.calls", {"period": "month", "limit": 25, "offset": 0})["total"] == 0
            config = request("model.save", {"base_url": "https://generated.invalid", "model": "generated-flash", "api_key": "generated-local-key-not-for-service"})
            assert config["storage_mode"] == "sqlite_plaintext"
            assert "generated-local-key-not-for-service" not in json.dumps(config)
            assert request("state")["model_usage"]["month"]["requests"] == 0
            assert request("model.delete", {"revision": config["revision"], "confirm": True}) == {"deleted": True}
            assert request("state")["text_model"] is None
            assert request("state")["message_count"] == 4
            with closing(sqlite3.connect(Path(directory) / "application.sqlite3")) as db:
                account = initial["source"]["account_id"]
                refs = json.dumps([long_message["message_id"]])
                stamp = "2026-10-08T00:00:00Z"
                db.execute("INSERT INTO analysis_runs VALUES(?,?,?,?,?,?,?,?,?)",
                    ("packaged-claim-batch", account, refs, 1, 1, "success", stamp, stamp, None))
                db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("packaged-claim-task", account, "packaged-claim-batch", "发送评审资料", "unknown", None,
                     "2026-10-12", "date", "needs_confirmation", refs, stamp, 1))
                db.commit()
            confirmed = request("task.claim_self", {"task_id": "packaged-claim-task", "revision": 1})["items"][0]
            assert (confirmed["owner_type"], confirmed["status"], confirmed["revision"]) == ("self", "pending", 2)
            assert confirmed["source_message_ids"] == [long_message["message_id"]]
            assert confirmed["due_at"] == "2026-10-12"
            assert request("state")["paused"] is True
            assert request("model.calls", {"period": "month", "limit": 25, "offset": 0})["total"] == 0
            worker.stdin.close()
            assert worker.wait(timeout=10) == 0
            assert worker.stderr.read() == "", "unexpected bundled worker diagnostics"
            with closing(sqlite3.connect(Path(directory) / "application.sqlite3")) as db:
                assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 4
                assert db.execute("SELECT max(length(content)) FROM messages").fetchone()[0] == 2800
                assert db.execute("PRAGMA user_version").fetchone()[0] == 12
                assert db.execute("SELECT COUNT(*) FROM model_local_keys").fetchone()[0] == 0
            print(json.dumps({"packaged_reader": "passed", "source": "synthetic", "messages": 4, "long_text_chars": 2800, "external_python_required": False, "local_model_save_delete": "passed", "task_self_claim": "passed", "model_requests": 0, "clean_exit": True}))
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=10)
            for stream in (worker.stdin, worker.stdout, worker.stderr):
                if stream and not stream.closed:
                    stream.close()


if __name__ == "__main__":
    main()
