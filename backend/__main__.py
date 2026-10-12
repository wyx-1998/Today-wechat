"""JSONL stdio worker, owned by the GUI process; no server or daemon."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import threading

from backend.contracts import PROTOCOL_VERSION, ServiceError
from backend.service import LocalService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--upgrade-model-storage", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.upgrade_model_storage:
        # Internal installation maintenance, not a webview/business command.
        # No service timers, recovery writes, sync or model requests are started.
        from backend.models import Models
        from backend.storage import Store
        models = None
        try:
            models = Models(Store(args.data_dir))
            config = models.public()
            if config and config["storage_mode"] != "sqlite_plaintext":
                models.migrate(revision=config["revision"], confirm=True)
            print(json.dumps({"status": "ready" if config else "not_configured"}), flush=True)
        except ServiceError as error:
            print(json.dumps({"error": error.code}), flush=True)
            raise SystemExit(1) from None
        except Exception:
            print(json.dumps({"error": "storage_upgrade_failed"}), flush=True)
            raise SystemExit(1) from None
        finally:
            if models:
                models.close()
        return
    service = LocalService(args.data_dir)
    output_lock = threading.Lock()

    def event(request, packet):
        with output_lock:
            print(json.dumps({"version": PROTOCOL_VERSION, "stream_id": request["id"], **packet}, ensure_ascii=False), flush=True)

    def respond(request):
        response = {"version": PROTOCOL_VERSION, "id": request.get("id") if isinstance(request, dict) else None}
        try:
            if not isinstance(request, dict) or set(request) - {"version", "id", "command", "params"} or type(request.get("version")) is not int or request["version"] != PROTOCOL_VERSION or not isinstance(request.get("id"), str) or not 1 <= len(request["id"]) <= 128 or not isinstance(request.get("command"), str):
                raise ServiceError("invalid_request", "读取程序的请求格式不兼容。")
            response["result"] = service.dispatch(request["command"], request.get("params", {}), event_sink=lambda packet: event(request, packet))
        except ServiceError as error:
            response["error"] = error.payload()
        except Exception:
            response["error"] = {"code": "internal_error", "message": "本地读取程序遇到异常，请保留应用数据后重试。", "retryable": True}
        with output_lock:
            print(json.dumps(response, ensure_ascii=False), flush=True)

    service.start()
    try:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="gui-command") as pool:
            while line := sys.stdin.buffer.readline(2 * 1024 * 1024 + 1):
                if len(line) > 2 * 1024 * 1024:
                    respond({})
                    break
                try:
                    request = json.loads(line)
                except (ValueError, UnicodeError):
                    respond({})
                    continue
                pool.submit(respond, request)
            service.close()
    finally:
        service.close()


if __name__ == "__main__":
    main()
