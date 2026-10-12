"""Deterministic concurrency checks of the actual service, not GUI substitutes."""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.contracts import ServiceError, parse_time, stable_id
from backend.demo import CHATS, database, table_name
from backend.reader import SourceReader
from backend.service import LocalService


class ServiceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="today-lifecycle-synthetic-")
        self.addCleanup(self.temporary.cleanup)
        self.now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc).timestamp()
        self.service = LocalService(self.temporary.name, clock=lambda: self.now)
        self.addCleanup(self.service.close)
        self.service.connect_demo()
        self.service.store.update_chat(CHATS[0][0], sync_enabled=True, analysis_enabled=False)

    def controlled_read(self, call_to_block=1):
        entered, release = threading.Event(), threading.Event()
        original = SourceReader.read_batch
        calls = [0]

        def read(reader, *args, **kwargs):
            batch = original(reader, *args, **kwargs)
            calls[0] += 1
            if calls[0] == call_to_block:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("test did not release blocked read")
            return batch

        return entered, release, read

    def launch_sync(self):
        result, errors = [], []

        def work():
            try:
                result.append(self.service.sync())
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=work)
        thread.start()
        return thread, result, errors

    def finish(self, thread, release, errors):
        release.set()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_pause_then_immediate_resume_still_cancels_old_round(self):
        entered, release, read = self.controlled_read()
        with patch.object(SourceReader, "read_batch", read):
            thread, result, errors = self.launch_sync()
            try:
                self.assertTrue(entered.wait(3))
                self.service.pause()
                self.service.resume()
            finally:
                self.finish(thread, release, errors)
        self.assertEqual(result[0]["outcome"], "cancelled")
        self.assertEqual(result[0]["inserted"], 0)
        self.assertEqual(self.service.state()["message_count"], 0)
        self.assertEqual(self.service.sync()["inserted"], 4)

    def test_scope_change_during_read_does_not_commit_old_scope(self):
        entered, release, read = self.controlled_read()
        with patch.object(SourceReader, "read_batch", read):
            thread, result, errors = self.launch_sync()
            try:
                self.assertTrue(entered.wait(3))
                self.service.store.update_chat(CHATS[0][0], sync_enabled=False, analysis_enabled=False)
            finally:
                self.finish(thread, release, errors)
        self.assertEqual(result[0]["outcome"], "cancelled")
        self.assertEqual(self.service.state()["message_count"], 0)

    def test_duplicate_sync_and_connect_return_busy_without_blocking_pause(self):
        entered, release, read = self.controlled_read()
        with patch.object(SourceReader, "read_batch", read):
            thread, _, errors = self.launch_sync()
            try:
                self.assertTrue(entered.wait(3))
                for operation in (self.service.sync, self.service.connect_demo):
                    with self.assertRaises(ServiceError) as caught:
                        operation()
                    self.assertEqual(caught.exception.code, "sync_busy")
                self.assertTrue(self.service.pause()["paused"])
            finally:
                self.finish(thread, release, errors)

    def test_close_during_read_cancels_then_waits_for_safe_exit(self):
        entered, release, read = self.controlled_read()
        with patch.object(SourceReader, "read_batch", read):
            thread, result, errors = self.launch_sync()
            closer = threading.Thread(target=self.service.close)
            try:
                self.assertTrue(entered.wait(3))
                closer.start()
                self.assertTrue(self.service.stop_event.wait(3))
                self.assertTrue(closer.is_alive())
                with self.assertRaises(ServiceError) as caught:
                    self.service.dispatch("demo.append", {})
                self.assertEqual(caught.exception.code, "service_closed")
            finally:
                self.finish(thread, release, errors)
                closer.join(timeout=3)
        self.assertFalse(closer.is_alive())
        self.assertEqual(result[0]["outcome"], "cancelled")
        self.assertEqual(self.service.state()["message_count"], 0)

    def test_pause_second_batch_preserves_first_successful_prefix(self):
        source = self.service._reader().root / "message/message_0.db"
        with database(source) as db:
            db.executemany(f'INSERT INTO "{table_name(CHATS[0][0])}" VALUES(?,2,?,1,0,?)', [(local, int(self.now - 1), f"合成批次消息 {local}") for local in range(6, 606)])
        entered, release, read = self.controlled_read(call_to_block=2)
        with patch.object(SourceReader, "read_batch", read):
            thread, result, errors = self.launch_sync()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.service.state()["message_count"], 500)
                self.service.pause()
            finally:
                self.finish(thread, release, errors)
        self.assertEqual(result[0]["outcome"], "cancelled")
        self.assertEqual(result[0]["inserted"], 500)
        settings = self.service.store.settings()
        stream = self.service._reader().streams(CHATS[0][0])[0]
        key = stable_id(parse_time(settings["time_start"]))
        self.assertEqual(self.service.store.cursor(settings["source"]["account_id"], CHATS[0][0], stream[0], key), "501")
        self.service.resume()
        self.assertEqual(self.service.sync()["inserted"], 104)
        self.assertEqual(self.service.state()["message_count"], 604)

    def test_injected_clock_controls_seven_days_and_exclusive_end(self):
        report = self.service.sync()
        self.assertEqual(parse_time(report["time_start"]), self.now - 7 * 86400)
        self.assertEqual(parse_time(report["time_end"]), self.now)
        self.assertEqual(report["inserted"], 4)
        self.service.dispatch("demo.append", {})
        self.assertEqual(self.service.sync()["inserted"], 0)
        self.now += 1
        self.assertEqual(self.service.sync()["inserted"], 1)
        self.assertGreaterEqual(report["duration_ms"], 0)

    def test_invalid_parameter_types_are_not_storage_failures(self):
        for command, params in [("messages.list", {"chat_id": []}), ("message.detail", {"message_id": 1}), ("tag.delete", {"tag_id": False}), ("tag.save", {"name": "工作", "tag_id": ""})]:
            with self.subTest(command=command), self.assertRaises(ServiceError) as caught:
                self.service.dispatch(command, params)
            self.assertEqual(caught.exception.code, "invalid_identifier")
        with self.assertRaises(ServiceError) as caught:
            self.service.dispatch([], {})
        self.assertEqual(caught.exception.code, "invalid_command")

    def test_deleted_tag_is_not_reported_as_success_again(self):
        tag_id = self.service.store.save_tag("演示标签")
        self.service.store.delete_tag(tag_id)
        with self.assertRaises(ServiceError) as caught:
            self.service.store.delete_tag(tag_id)
        self.assertEqual(caught.exception.code, "tag_missing")

    def test_stdio_rejects_malformed_version_and_unknown_envelope_fields(self):
        with tempfile.TemporaryDirectory(prefix="today-envelope-synthetic-") as directory:
            requests = [
                {"version": True, "id": "a", "command": "source.demo"},
                {"version": 1, "id": "", "command": "source.demo"},
                {"version": 1, "id": "c", "command": "source.demo", "sql": "not allowed"},
            ]
            # Request stream remains open until all responses have arrived.
            worker = subprocess.Popen([sys.executable, "-m", "backend", "--data-dir", directory], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=Path(__file__).resolve().parents[1])
            try:
                for request in requests:
                    worker.stdin.write(json.dumps(request) + "\n")
                    worker.stdin.flush()
                    self.assertEqual(json.loads(worker.stdout.readline())["error"]["code"], "invalid_request")
                worker.stdin.close()
                self.assertEqual(worker.wait(timeout=3), 0)
                self.assertFalse((Path(directory) / "demo-source-v1").exists())
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait()
                for pipe in (worker.stdin, worker.stdout, worker.stderr):
                    pipe.close()


if __name__ == "__main__":
    unittest.main()
