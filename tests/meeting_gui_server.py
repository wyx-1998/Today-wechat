"""Owned GUI harness: temporary data, no real model transport or WeChat source."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import tempfile

from backend.contracts import ServiceError
from backend.service import LocalService
from tests.test_meetings import Client


def main():
    with tempfile.TemporaryDirectory(prefix="meeting-gui-owned-") as root:
        client = Client()
        service = LocalService(root, model_client=client)
        service.models.save(base_url="https://generated.invalid", model="generated-flash", api_key="generated-local-key")
        allowed = {"state", "today.list", "tasks.list", "message.detail", "meeting.save", "meeting.list",
                   "meeting.detail", "meeting.preview", "meeting.generate", "meeting.publish"}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_OPTIONS(self):
                self.send_response(204); self.response_headers(); self.end_headers()

            def response_headers(self):
                self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:1425")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Type", "application/json")

            def do_POST(self):
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 2_000_000: raise ValueError("size")
                    payload = json.loads(self.rfile.read(size))
                    command = payload["command"]
                    if command not in allowed: raise ValueError("command")
                    result = {"result": service.dispatch(command, payload.get("params", {}))}
                except ServiceError as error:
                    result = {"error": {"code": error.code, "message": error.message}}
                except Exception:
                    result = {"error": {"message": "isolated harness request failed"}}
                self.send_response(200); self.response_headers(); self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 1445), Handler)
        try:
            print("meeting GUI harness ready (temporary local data; intercepted model)", flush=True)
            server.serve_forever()
        finally:
            server.server_close(); service.close()


if __name__ == "__main__":
    main()
