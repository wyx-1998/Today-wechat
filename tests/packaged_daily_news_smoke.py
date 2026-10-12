"""Actual packaged reader local consent and seeded-edition checks, zero cloud calls."""
from contextlib import closing
import argparse
import json
from pathlib import Path
import selectors
import sqlite3
import subprocess

from backend.contracts import iso_time
from tests.test_daily_news import NewsTests


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--binary",required=True);args=parser.parse_args()
    binary=Path(args.binary).resolve(strict=True)
    f=NewsTests();f.setUp();f.add("original");f.s.close()
    worker=None
    try:
        directory=f.root.name
        worker=subprocess.Popen([str(binary),"--data-dir",directory],cwd=directory,
            env={"PATH":"/usr/bin:/bin","LANG":"en_US.UTF-8","TMPDIR":directory},stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True,encoding="utf-8",bufsize=1)
        counter=0
        def request(command,params=None,error=None):
            nonlocal counter
            counter+=1;worker.stdin.write(json.dumps({"version":1,"id":str(counter),"command":command,"params":params or {}})+"\n");worker.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(worker.stdout,selectors.EVENT_READ);assert selector.select(timeout=10)
            value=json.loads(worker.stdout.readline());assert value["id"]==str(counter)
            if error:assert value["error"]["code"]==error;return
            assert "error" not in value,value
            return value["result"]
        assert not request("state")["daily_news"]["enabled"]
        p=request("daily.preview")
        request("daily.enable",{"preview_token":p["preview_token"],"authorize":False},error="cloud_authorization_required")
        enabled=request("daily.enable",{"preview_token":p["preview_token"],"authorize":True})
        day=p["first_day"]
        preview=request("daily.preview_run",{"day":day})
        assert "13800138000" not in json.dumps(preview)
        request("daily.run",{"day":day,"preview_token":preview["preview_token"],"authorize":False},error="cloud_authorization_required")
        body={"topics":[{"candidate_id":"C1","title":"模型发布","summary":"发布了文本模型。","kind":"ai","source_message_ids":["original"]}],
            "overview":["C1"],"message_count":1,"chat_count":2,"coverage_warnings":[],"rejected_count":0}
        with closing(sqlite3.connect(Path(directory)/"application.sqlite3")) as db:
            db.execute("INSERT INTO news_editions VALUES(?,?,?,?,?,?,?)",("owned-news",day,1,json.dumps(body),json.dumps(["original"]),"owned-input",iso_time(f.now)))
            db.commit()
        report=request("daily.detail",{"day":day});assert report["edition"]["body"]==body
        assert request("daily.list")["days"]==[day]
        assert "13800138000" in request("message.detail",{"message_id":"original"})["content"]
        assert request("today.list")["items"]==[]
        request("daily.disable",{"revision":enabled["revision"]})
        assert request("daily.detail",{"day":day})["edition"]["body"]==body
        assert request("model.calls",{"period":"month","limit":25,"offset":0})["total"]==0
        worker.stdin.close();assert worker.wait(timeout=10)==0;assert worker.stderr.read()==""
        print(json.dumps({"packaged_daily_news":"passed","consent":"explicit","edition_setup":"seeded_in_isolated_db",
            "original_exact":True,"model_requests":0,"clean_exit":True}))
    finally:
        if worker:
            if worker.poll() is None:worker.terminate();worker.wait(timeout=10)
            for stream in (worker.stdin,worker.stdout,worker.stderr):
                if stream and not stream.closed:stream.close()
        f.doCleanups()


if __name__=="__main__":main()
