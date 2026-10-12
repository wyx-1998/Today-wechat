"""Owned browser harness. Temporary messages and intercepted model only."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json

from backend.contracts import ServiceError
from tests.test_daily_news import NewsTests


def main():
    fixture=NewsTests();fixture.setUp()
    # HTTP needs sockets; the service still uses NewsClient, never GeminiClient.
    fixture.network.stop()
    fixture.add("ai-a",content="AI 团队发布模型，支持长文本和工具调用。正式使用前仍需评估稳定性。")
    fixture.add("ai-b",content="AI 团队发布模型，支持长文本和工具调用。正式使用前仍需评估稳定性。",chat="b")
    fixture.add("work",content="项目公告：评审调整为线上，材料提交时间不变。")
    fixture.now+=60;fixture.sync()
    service=fixture.s
    allowed={"state","today.list","tasks.list","message.detail","model.calls","daily.preview","daily.enable",
             "daily.disable","daily.detail","daily.list","daily.preview_run","daily.run"}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def response_headers(self):
            self.send_header("Content-Type","application/json")
            self.send_header("Access-Control-Allow-Origin","http://127.0.0.1:1426")
            self.send_header("Access-Control-Allow-Headers","Content-Type")
        def do_OPTIONS(self):self.send_response(204);self.response_headers();self.end_headers()
        def do_POST(self):
            try:
                size=int(self.headers.get("Content-Length","0"))
                if not 0<size<=2_000_000:raise ValueError()
                payload=json.loads(self.rfile.read(size))
                command=payload["command"]
                if command=="harness.tick":result=service.daily_news.tick()
                elif command=="harness.advance":fixture.now+=86400;fixture.sync();result={"day_advanced":True}
                elif command in allowed:result=service.dispatch(command,payload.get("params",{}))
                else:raise ValueError()
                value={"result":result}
            except ServiceError as error:value={"error":error.payload()}
            except Exception:value={"error":{"message":"owned harness request failed"}}
            self.send_response(200);self.response_headers();self.end_headers()
            self.wfile.write(json.dumps(value,ensure_ascii=False).encode())
    server=ThreadingHTTPServer(("127.0.0.1",1446),Handler)
    try:
        print("owned daily-news GUI harness ready; model transport intercepted",flush=True)
        server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();fixture.doCleanups()


if __name__=="__main__":main()
