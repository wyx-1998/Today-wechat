"""Loopback UI check with temporary generated data; no timers or external requests."""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tests import automatic_smoke
from backend.contracts import ServiceError, iso_time


def main():
    h = automatic_smoke.AutomaticTests('runTest')
    h.setUp()
    s = h.service
    h.now = time.time()
    s.store.set_range(iso_time(h.now-3600))
    with s.store.database() as db:
        db.execute("UPDATE messages SET sent_at=?,sent_epoch=? WHERE message_id='initial'", (iso_time(h.now-30), h.now-30))
        db.execute("UPDATE chats SET chat_name='资料协作群' WHERE chat_id='first'")
        db.execute("UPDATE chats SET chat_name='行业动态交流' WHERE chat_id='second'")
    h.grant(); s.resume()
    allowed = {'state','model.calls','authorization.preview','authorization.save','authorization.revoke',
        'analysis.real.preview','analysis.real','analysis.review.preview','analysis.review','analysis.batch.retry'}
    count = 0
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal count
            try:
                if self.path != '/api': raise ValueError()
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
                h.now = time.time()
                if body['command'] == 'harness.add':
                    count += 1
                    h.add('incoming-'+str(count), content='新到消息，仅用于下一次整理。')
                    with s.store.database() as db:
                        grant = s.automatic.active(db)
                        if grant: s.automatic.capture(db, grant)
                    value = {'added':True}
                elif body['command'] in allowed:
                    value = s.dispatch(body['command'], body.get('params',{}))
                else: raise ValueError()
                code, payload = 200, {'result':value}
            except ServiceError as error:
                code, payload = 400, {'error':{'message':error.message,'code':error.code}}
            except Exception:
                code, payload = 400, {'error':{'message':'隔离操作不受支持'}}
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1',1424), Handler)
    print('Isolated processing adapter ready on 1424; no production database or model network.', flush=True)
    try: server.serve_forever()
    finally: server.server_close(); h.doCleanups()


if __name__ == '__main__': main()
