"""Owned loopback-only data for the actual message-browser component."""
import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from backend.contracts import ServiceError, iso_time, parse_time
from backend.service import LocalService


def main():
    with tempfile.TemporaryDirectory(prefix="wechat-today-messages-gui-") as directory:
        service=LocalService(directory)
        service.store.configure(dict(account_id="isolated",root="not-read",mode="local",self_id="self"),"2026-09-01T00:00:00Z",
            [dict(chat_id=f"c{i:04}",chat_name=f"资料协作 {i:04}",chat_type="group") for i in range(2100)])
        service.store.set_paused(True)
        with service.store.database() as db:
            for chat in range(80):
                for index in range(130 if chat==0 else 3):
                    sender=["self","other","second",None][index%4]
                    date=iso_time(parse_time("2026-10-01T00:00:00+08:00")+index*1800)
                    body="完整原文"*700 if index==8 else None if index==11 else f"资料核对记录 {index+1}：请保留当前材料及实际取得时间。"
                    db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"m{chat:04}-{index:03}","isolated",f"c{chat:04}","stream",str(index),sender,"同名同事" if sender in ("other","second") else "本人" if sender else None,None,date,parse_time(date),"image" if body is None else "text",body,"type_only" if body is None else "text_available",int(body is not None)))
            db.execute("INSERT INTO chats VALUES(?,?,?,?,?,?,?)",("another","c0000","另一个账号的会话",None,"group",0,0))
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",("another-message","another","c0000","stream","1","else","另一个发送人",None,"2026-10-01T00:00:00Z",parse_time("2026-10-01T00:00:00Z"),"text","另一账号的内容","text_available",1))
        flags={"delay":False,"fail":False}
        flags_lock=threading.Lock()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    if self.path!="/api":raise ValueError()
                    body=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))))
                    command=body["command"]
                    if command=="harness.delay":
                        with flags_lock: flags["delay"]=not flags["delay"]; result={"delay":flags["delay"]}
                    elif command=="harness.fail":
                        with flags_lock: flags["fail"]=True
                        result={"fail":True}
                    elif command=="harness.account":
                        with service.store.database() as db:
                            service.store._set(db,"source",dict(account_id="another",root="not-read",mode="local",self_id="else"))
                            service.store._set(db,"revision",service.store._settings(db)["revision"]+1)
                        result={"changed":True}
                    elif command in {"state","messages.chats","messages.list","message.detail"}:
                        with flags_lock:
                            fail=flags["fail"] and command=="messages.list"
                            if fail:flags["fail"]=False
                            delayed=flags["delay"] and command in {"messages.list","message.detail"}
                        if fail:raise ServiceError("storage_failed","本地存储操作失败，请重新查询。")
                        result=service.dispatch(command,body.get("params",{}))
                        if delayed:time.sleep(.8)
                    else:raise ValueError()
                    status,payload=200,{"result":result}
                except ServiceError as error:status,payload=400,{"error":error.payload()}
                except Exception:status,payload=400,{"error":{"message":"隔离请求失败"}}
                data=json.dumps(payload,ensure_ascii=False).encode()
                self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
            def log_message(self,*_):pass
        server=ThreadingHTTPServer(("127.0.0.1",1452),Handler)
        print("Isolated message adapter ready on 127.0.0.1:1452",flush=True)
        try:server.serve_forever()
        finally:server.server_close();service.close()


if __name__=="__main__":main()
