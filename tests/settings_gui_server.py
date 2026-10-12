"""Loopback-only isolated GUI adapter. No real sources, timers or model client calls."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import time
from backend.contracts import ServiceError, iso_time
from backend.service import LocalService


def main():
    with tempfile.TemporaryDirectory(prefix="wechat-today-settings-gui-") as directory:
        service=LocalService(directory)
        service.store.configure(dict(account_id="isolated",root="not-read",mode="local"),
            "2026-09-18T00:00:00Z", [dict(chat_id=f"c{i:04}",chat_name=f"资料协作 {i:04}" + (" · 很长的会话名称与跨部门资料讨论"*4 if i==0 else ""),chat_type="group" if i%2 else "direct") for i in range(2100)])
        service.store.set_paused(True)
        tags=[service.store.save_tag(f"项目标签 {i:03}") for i in range(300)]
        with service.store.database() as db:
            for i in range(2100):
                db.executemany("INSERT INTO chat_tags VALUES(?,?,?)",[("isolated",f"c{i:04}",tags[j]) for j in range(8 if i==0 else 1)])
                if i<20: db.execute("UPDATE chats SET sync_enabled=1,analysis_enabled=? WHERE chat_id=?",(int(i<10),f"c{i:04}"))
            now=time.time()
            for i in range(60):
                db.execute("INSERT INTO model_calls(call_id,capability,config_revision,purpose,started_at,outcome,input_tokens,output_tokens,total_tokens) VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"call{i:03}","text",1,"automatic_text",iso_time(now-i*60),["success","failed","unknown","sending"][i%4],None if i%3==0 else 150,60,400))
            db.execute("CREATE TRIGGER deny_one BEFORE UPDATE ON chats WHEN NEW.chat_id='c0002' BEGIN SELECT RAISE(ABORT,'controlled'); END")
        allowed={"state","chat.bulk.preview","chat.bulk.apply","tag.save","tag.delete","chat.save","model.calls"}
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    if self.path!="/api": raise ValueError()
                    body=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))))
                    command=body["command"]
                    if command=="harness.change":
                        service.store.save_tag("新增配置标签")
                        result={"changed":True}
                    elif command in allowed: result=service.dispatch(command,body.get("params",{}))
                    else: raise ValueError()
                    status, payload=200, {"result":result}
                except ServiceError as error:
                    status, payload=400, {"error":{"code":error.code,"message":error.message}}
                except Exception:
                    status, payload=400,{"error":{"message":"隔离请求失败"}}
                data=json.dumps(payload,ensure_ascii=False).encode()
                self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
            def log_message(self, *_): pass
        server=ThreadingHTTPServer(("127.0.0.1",1422),Handler)
        print("Isolated settings adapter ready on 127.0.0.1:1422",flush=True)
        try: server.serve_forever()
        finally: server.server_close();service.close()


if __name__=="__main__": main()
