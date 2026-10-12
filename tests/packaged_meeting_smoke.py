"""Bundled reader meeting publication with explicitly seeded generated candidates.

No real LLM is called. Provider-format checks are covered separately by test_meetings.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import selectors
import sqlite3
import subprocess
import tempfile

from backend.meetings import normalize, segments
from tests.test_meetings import ORIGINAL, answer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    args = parser.parse_args()
    binary = Path(args.binary).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="packaged-meeting-owned-") as directory:
        worker = subprocess.Popen([str(binary), "--data-dir", directory], cwd=directory,
            env={"PATH":"/usr/bin:/bin", "LANG":"en_US.UTF-8", "TMPDIR":directory},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1)
        counter = 0
        try:
            def request(command, params=None, error=None):
                nonlocal counter
                counter += 1
                worker.stdin.write(json.dumps({"version":1,"id":str(counter),"command":command,"params":params or {}},ensure_ascii=False)+"\n")
                worker.stdin.flush()
                with selectors.DefaultSelector() as selector:
                    selector.register(worker.stdout,selectors.EVENT_READ)
                    assert selector.select(timeout=15), "bundled reader timeout"
                response=json.loads(worker.stdout.readline())
                assert response["id"]==str(counter)
                if error:
                    assert response["error"]["code"]==error, response
                    return
                assert "error" not in response, response
                return response["result"]

            note=request("meeting.save",{"content":ORIGINAL,"title":"项目评审","file_name":"会议.md"})
            assert note["original"]==ORIGINAL and note["status"]=="draft"
            request("meeting.preview",{"meeting_id":note["meeting_id"],"revision":1},error="model_not_configured")
            config=request("model.save",{"base_url":"https://generated.invalid","model":"generated-flash","api_key":"generated-local-key"})
            p=request("meeting.preview",{"meeting_id":note["meeting_id"],"revision":1})
            assert "13800138000" not in json.dumps(p)
            request("meeting.generate",{"meeting_id":note["meeting_id"],"revision":1,"preview_token":p["preview_token"],"authorize":False},error="cloud_authorization_required")
            # Candidate seeding is isolated test setup, not a product import route.
            result=normalize(answer(),segments(ORIGINAL),None)
            with closing(sqlite3.connect(Path(directory)/"application.sqlite3")) as db:
                db.execute("UPDATE meeting_notes SET status='ready',revision=2,generated_json=?,config_revision=? WHERE meeting_id=?",
                    (json.dumps(result,ensure_ascii=False),config["revision"],note["meeting_id"]))
                db.commit()
            saved=request("meeting.publish",{"meeting_id":note["meeting_id"],"revision":2,"candidate_ids":["T1","K1"],"confirm":True})
            assert saved["status"]=="published" and saved["result"]["task_count"]==saved["result"]["key_point_count"]==1
            request("meeting.publish",{"meeting_id":note["meeting_id"],"revision":2,"candidate_ids":["T1"],"confirm":True},error="meeting_changed")
            page=request("today.list")
            assert len(page["items"])==len(page["attention_items"])==1
            assert page["attention_items"][0]["source_kind"]=="meeting" and page["last_analysis"] is None
            original=request("message.detail",{"message_id":saved["source_message_id"]})
            assert original["source_kind"]=="meeting" and original["content"]==ORIGINAL and original["meeting_date"] is None
            assert request("state")["message_count"]==0
            assert request("messages.list")["total"]==0
            assert request("messages.chats")["total"]==0
            assert request("model.calls",{"period":"month","limit":25,"offset":0})["total"]==0
            assert request("meeting.list")["total"]==1
            worker.stdin.close(); assert worker.wait(timeout=10)==0
            assert worker.stderr.read()==""
            print(json.dumps({"packaged_meeting":"passed","published_tasks":1,"published_key_points":1,
                "original_exact":True,"model_requests":0,"candidate_setup":"seeded_in_isolated_db","clean_exit":True}))
        finally:
            if worker.poll() is None: worker.terminate(); worker.wait(timeout=10)
            for stream in (worker.stdin,worker.stdout,worker.stderr):
                if stream and not stream.closed: stream.close()


if __name__=="__main__":
    main()
