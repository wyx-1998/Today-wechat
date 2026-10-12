"""Temporary Ask UI harness with actual backend and intercepted model, never production data."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import tempfile
import time
import threading
from backend.contracts import ServiceError
from tests.test_ask import AskTests


def main():
    fixture=AskTests();fixture.setUp();service=fixture.s
    class SlowClient:
        def stream(self,config,key,prompt,control):
            for text in ['结论：方案需要周五提交。','[M1]\n\n','事实：小王提出了提交要求。','[M1]']:
                control.check();time.sleep(.6);yield {'text':text}
            yield {'finished':True,'usage':{'input_tokens':20,'output_tokens':14,'total_tokens':34}}
    service.ask.provider=SlowClient()
    fixture.network.stop()  # HTTP harness is loopback only; provider stays intercepted.
    packets=[];lock=threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            try:
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                command=payload['command'];params=payload.get('params',{})
                if command=='harness.events':
                    with lock:result=[p for p in packets if p['sequence']>params.get('after',0) and p['run_id']==params['run_id']]
                else:
                    def sink(packet):
                        with lock:packets.append(packet)
                    result=service.dispatch(command,params,event_sink=sink)
                value={'result':result}
            except ServiceError as e:value={'error':e.payload()}
            except Exception:value={'error':{'message':'isolated harness error'}}
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(value,ensure_ascii=False).encode())
    server=ThreadingHTTPServer(('127.0.0.1',1449),Handler)
    try:print('isolated Ask GUI harness ready',flush=True);server.serve_forever()
    finally:server.server_close();fixture.doCleanups()

if __name__=='__main__':main()
