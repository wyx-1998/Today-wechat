"""Local application service; explicit encrypted preparation, no model/network."""

import sqlite3
from contextlib import contextmanager
import threading
import time
import uuid

from backend.contracts import ReadScope, ServiceError, iso_time, parse_time, stable_id
from backend.demo import ACCOUNT, SELF, append_demo, ensure_demo
from backend.reader import SourceReader
from backend.storage import Store
from backend.encrypted_source import encrypted_reader, selected_paths, encrypted_manifest, SNAPSHOT_POLICY
from backend.models import Models
from backend.analysis import Analysis
from backend.automatic import AutomaticText
from backend.reanalysis import Reanalysis
from backend.chat_bulk import ChatBulk
from backend.meetings import Meetings
from backend.daily_news import DailyNews
from backend.ask import Ask


class LocalService:
    def __init__(self, directory, *, interval=60, analysis_interval=15, clock=time.time, keychain=None, model_client=None, ask_client=None):
        self.store = Store(directory)
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self.operation_lock = threading.Lock()
        self.guard = threading.RLock()
        self.busy = False
        self.interval = interval
        self.timer = None
        self.analysis_timer = None
        self.news_timer = None
        self.analysis_interval = analysis_interval
        self.analysis_generation = 0
        self.clock = clock
        self.models = Models(self.store, keychain=keychain, client=model_client, clock=clock, entry_lock=self.guard)
        self.analysis = Analysis(self)
        self.automatic = AutomaticText(self)
        self.reanalysis = Reanalysis(self)
        self.chat_bulk = ChatBulk(self)
        self.meetings = Meetings(self)
        self.daily_news = DailyNews(self)
        self.ask = Ask(self, client=ask_client)

    def ensure_open(self):
        if self.stop_event.is_set():
            raise ServiceError("service_closed", "读取程序正在退出，未开始新的操作。")

    @contextmanager
    def exclusive_operation(self):
        if not self.operation_lock.acquire(blocking=False):
            raise ServiceError("sync_busy", "同步或连接操作正在进行，请等待本轮结束。", retryable=True)
        try:
            self.ensure_open()
            yield
        finally:
            self.operation_lock.release()

    def start(self):
        self.ensure_open()
        if self.timer is None:
            with self.store.database() as db:
                db.execute("UPDATE model_calls SET outcome='unknown',error_code='interrupted' WHERE outcome='sending'")
                db.execute("UPDATE analysis_runs SET status='failed',error_code='interrupted' WHERE status='running'")
            self.automatic.recover()
            self.meetings.recover()
            self.daily_news.recover()
            self.ask.recover()
            self.timer = threading.Thread(target=self._schedule, daemon=True, name="local-sync-timer")
            self.timer.start()
            self.analysis_timer = threading.Thread(target=self._analyze_schedule, daemon=True, name="text-analysis-timer")
            self.analysis_timer.start()
            self.news_timer = threading.Thread(target=self._news_schedule, daemon=True, name="daily-news-timer")
            self.news_timer.start()

    def close(self):
        with self.guard:
            self.stop_event.set()
            self.cancel_event.set()
            self.analysis_generation += 1
        self.ask.close()
        self.models.close()
        if self.timer and self.timer is not threading.current_thread():
            self.timer.join(timeout=3)
        if self.analysis_timer and self.analysis_timer is not threading.current_thread():
            self.analysis_timer.join(timeout=3)
        if self.news_timer and self.news_timer is not threading.current_thread():
            self.news_timer.join(timeout=3)
        # Await the current bounded read/transaction before the process exits.
        with self.operation_lock:
            pass

    def _schedule(self):
        while not self.stop_event.wait(self.interval):
            state = self.store.settings()
            if state["source"] and not state["paused"] and any(c["sync_enabled"] for c in self.store.chats()):
                try:
                    self.sync()
                except ServiceError:
                    pass

    def _analyze_schedule(self):
        while not self.stop_event.wait(self.analysis_interval):
            try:
                self.automatic.tick()
            except (ServiceError, sqlite3.Error):
                # Persisted running requests recover as unknown, not a retry.
                # Diagnostics contain no prompt, source body or provider body.
                pass

    def _news_schedule(self):
        while not self.stop_event.wait(self.analysis_interval):
            try:
                self.daily_news.tick()
            except (ServiceError, sqlite3.Error):
                pass

    def state(self):
        state = self.store.settings()
        with self.guard:
            state["busy"] = self.busy
        state.update(chats=self.store.chats(), tags=self.store.tags(), message_count=self.store.messages(limit=1)["total"], automatic_interval_seconds=self.interval,
            cloud_enabled=bool(self.models.public() and self.models.public()["enabled"]), text_model=self.models.public(), model_usage=self.models.usage(), ask_model=self.ask.models.public(), ask_usage=self.ask.models.usage(),
            automatic_text=self.automatic.public(), daily_news=self.daily_news.public(), results_revision=self.store.results_revision())
        return state

    def _reader(self, source=None):
        source = source or self.store.settings()["source"]
        if not source:
            raise ServiceError("not_connected", "请先连接来源，并选择需要同步的会话。")
        return SourceReader(source["root"], source["account_id"], source.get("self_id"))

    @contextmanager
    def reader_context(self, source, *, metadata_only=False, cancelled=lambda: False):
        if source and source.get("mode") == "encrypted":
            with encrypted_reader(source, self.store.directory, metadata_only=metadata_only,
                                  cancelled=cancelled) as reader:
                yield reader
        else:
            yield self._reader(source)

    def connect_encrypted(self, root, keys_file, *, authorize=False, self_id=None):
        self.ensure_open()
        # Authorization precedes any path checks, key loads or metadata reads.
        if authorize is not True:
            raise ServiceError("authorization_required", "请先确认只读访问所选加密目录及密钥文件，并了解临时解密范围。")
        if self_id is not None and (not isinstance(self_id, str) or len(self_id) > 200):
            raise ServiceError("invalid_identity", "本人标识格式无效。")
        with self.exclusive_operation():
            source_root, key_path = selected_paths(root, keys_file)
            source = {"mode": "encrypted", "root": str(source_root), "keys_file": str(key_path),
                      "account_id": "encrypted-" + stable_id(str(source_root)), "self_id": self_id or None}
            with self.guard:
                token = threading.Event()
                self.cancel_event = token
            cancelled = lambda: token.is_set() or self.stop_event.is_set()
            with self.reader_context(source, metadata_only=True, cancelled=cancelled) as reader:
                discovered = reader.discover()
            with self.guard:
                self.ensure_open()
                if cancelled():
                    raise ServiceError("cancelled", "连接已停止，原来源及配置保留。", retryable=True)
                previous = self.store.settings()
                same_account = previous["source"] and previous["source"]["account_id"] == source["account_id"]
                start = previous["time_start"] if same_account else iso_time(self.clock() - 7 * 86400)
                self.store.configure(source, start, discovered)
                if previous["source"] != source: self.ask.cancel_active()
        return self.state()

    def connect_demo(self):
        with self.exclusive_operation():
            root, created = ensure_demo(self.store.directory, clock=self.clock)
            source = {"mode": "demo", "root": str(root), "account_id": ACCOUNT, "self_id": SELF}
            previous = self.store.settings()
            start = previous["time_start"] if previous["source"] == source else iso_time(created - 7 * 86400)
            self.store.configure(source, start, SourceReader(root, ACCOUNT, SELF).discover())
            if previous["source"] != source: self.ask.cancel_active()
        return self.state()

    def connect_local(self, root, *, authorize=False, self_id=None):
        self.ensure_open()
        if authorize is not True:
            raise ServiceError("authorization_required", "请先确认只读访问所选已解密目录。")
        if not isinstance(root, str) or not root:
            raise ServiceError("invalid_source", "请选择已解密目录。")
        if self_id is not None and (not isinstance(self_id, str) or len(self_id) > 200):
            raise ServiceError("invalid_identity", "本人标识格式无效。")
        with self.exclusive_operation():
            reader = SourceReader(root, "local-" + stable_id(root), self_id)
            account = "local-" + stable_id(str(reader.root))
            reader.account_id = account
            source = {"mode": "local", "root": str(reader.root), "account_id": account, "self_id": self_id or None}
            previous = self.store.settings()
            start = previous["time_start"] if previous["source"] == source else iso_time(self.clock() - 7 * 86400)
            self.store.configure(source, start, reader.discover())
            if previous["source"] != source: self.ask.cancel_active()
        return self.state()

    def pause(self):
        with self.guard:
            self.ensure_open()
            self.cancel_event.set()
            self.analysis_generation += 1
            self.store.set_paused(True)
        return self.state()

    def resume(self):
        with self.guard:
            self.ensure_open()
            self.store.set_paused(False)
        return self.state()

    def sync(self):
        if not self.operation_lock.acquire(blocking=False):
            raise ServiceError("sync_busy", "同步正在进行，请等待本轮结束。", retryable=True)
        try:
            with self.guard:
                self.ensure_open()
                state = self.store.settings()
                if state["paused"]:
                    raise ServiceError("sync_paused", "同步已暂停，请先恢复。")
                source = state["source"]
                if not source:
                    raise ServiceError("not_connected", "请先连接来源，并选择需要同步的会话。")
                scope = ReadScope(source["account_id"], tuple(c["chat_id"] for c in self.store.chats() if c["sync_enabled"]), state["time_start"], iso_time(self.clock()))
                # Each round owns its token. Resume never erases cancellation
                # of a round that was already paused.
                run_cancel = threading.Event()
                self.cancel_event = run_cancel
                self.busy = True
            cancelled = lambda: run_cancel.is_set() or self.stop_event.is_set()
            monotonic_start = time.monotonic()
            report = {"run_id": str(uuid.uuid4()), "account_id": scope.account_id, "started_at": iso_time(self.clock()), "outcome": "running", "inserted": 0, "completed_chats": [], "errors": [], "time_start": scope.time_start, "time_end": scope.time_end}
            range_key = stable_id(parse_time(scope.time_start))
            checkpoint = None
            scope_hash = stable_id(SNAPSHOT_POLICY, source, sorted(scope.chat_ids), scope.time_start, state["revision"])
            if scope.chat_ids:
                try:
                    previous = state.get("encrypted_sync_checkpoint")
                    end = parse_time(scope.time_end)
                    unchanged = (source.get("mode") == "encrypted" and isinstance(previous, dict)
                        and previous.get("scope_hash") == scope_hash
                        and previous.get("store_signature") == self.store.sync_store_signature(scope.account_id)
                        and end >= previous.get("end_epoch", float("inf"))
                        and (previous.get("deferred_until") is None or end <= previous["deferred_until"])
                        and encrypted_manifest(source) == previous.get("source_manifest"))
                    if unchanged:
                        report["completed_chats"] = list(scope.chat_ids)
                        report["preparation"] = {"skipped": True, "captured_databases": 0, "capture_attempts": 0,
                            "clone_files": 0, "copied_files": 0, "input_bytes": 0, "capture_ms": 0, "decrypt_ms": 0}
                    else:
                        with self.reader_context(source, cancelled=cancelled) as reader:
                            self._consume(reader, scope, range_key, state["revision"], cancelled, report)
                            if source.get("mode") == "encrypted":
                                report["preparation"] = reader.preparation_metrics
                                checkpoint = {"scope_hash": scope_hash, "source_manifest": reader.source_manifest,
                                    "store_signature": self.store.sync_store_signature(scope.account_id),
                                    "end_epoch": end, "deferred_until": reader.deferred_until}
                except ServiceError as error:
                    report["errors"].append({"chat_id": "__source__", **error.payload()})
            with self.guard:
                current = self.store.settings()
                invalidated = current["paused"] or current["revision"] != state["revision"] or current["source"] != source
                report["outcome"] = "cancelled" if cancelled() or invalidated or any(e["code"] == "cancelled" for e in report["errors"]) else "partial" if report["errors"] else "no_selection" if not scope.chat_ids else "success"
                report["finished_at"] = iso_time(self.clock())
                report["duration_ms"] = round((time.monotonic() - monotonic_start) * 1000, 3)
                self.store.record_run(report, checkpoint=checkpoint, expected=state, cancelled=cancelled)
            return report
        finally:
            with self.guard:
                self.busy = False
            self.operation_lock.release()

    def _consume(self, reader, scope, range_key, revision, cancelled, report):
        for chat in scope.chat_ids:
            if cancelled():
                break
            try:
                for stream in reader.streams(chat):
                    cursor = self.store.cursor(scope.account_id, chat, stream[0], range_key)
                    while True:
                        batch = reader.read_batch(scope, chat, stream, cursor, cancelled=cancelled)
                        report["inserted"] += self.store.commit_batch(scope, batch, range_key, revision, cancelled=cancelled, arrived_epoch=self.clock())
                        if not batch.has_more:
                            break
                        if batch.next_cursor == cursor:
                            raise ServiceError("future_frontier", "消息编号与时间乱序，后续批次暂缓，稍后重试。", retryable=True)
                        cursor = batch.next_cursor
                report["completed_chats"].append(chat)
            except ServiceError as error:
                report["errors"].append({"chat_id": chat, **error.payload()})
                if error.code == "cancelled":
                    break
            except sqlite3.Error:
                report["errors"].append({"chat_id": chat, "code": "storage_failed", "message": "本批次保存失败，未推进成功游标；请检查本地存储后重试。", "retryable": True})

    def dispatch(self, command, params, *, event_sink=None):
        self.ensure_open()
        allowed = {
            "ask.model.save": ("base_url", "model", "api_key", "enabled", "protocol"),
            "ask.model.enable": ("enabled",), "ask.model.delete": ("revision", "confirm"),
            "ask.model.test": ("authorize",), "ask.model.calls": ("period", "limit", "offset"),
            "ask.list": (), "ask.detail": ("ask_thread_id",),
            "ask.preview": ("question", "scope"),
            "ask.start": ("preview_token", "authorize", "persistent", "ask_thread_id"),
            "ask.stop": ("run_id",), "ask.revoke": (),
            "state": (), "source.demo": (), "source.local": ("root", "authorize", "self_id"),
            "source.encrypted": ("root", "keys_file", "authorize", "self_id"),
            "source.refresh": (), "chat.save": ("chat_id", "sync_enabled", "analysis_enabled", "display_name", "tag_ids"),
            "chat.bulk.preview": ("chat_ids", "operation", "tag_ids"), "chat.bulk.apply": ("preview_token", "confirm"),
            "model.calls": ("period", "limit", "offset"),
            "tag.save": ("name", "tag_id"), "tag.delete": ("tag_id",), "range.save": ("time_start",),
            "sync.now": (), "sync.pause": (), "sync.resume": (),
            "messages.chats": ("query", "limit", "offset"),
            "messages.list": ("chat_id", "limit", "offset", "time_start", "time_end", "sender_id", "sender_unknown", "order"),
            "message.detail": ("message_id",), "demo.append": (),
            "model.save": ("base_url", "model", "api_key", "enabled", "protocol"),
            "model.test": ("authorize",), "model.enable": ("enabled",),
            "model.delete": ("revision", "confirm"),
            "analysis.preview": (), "analysis.demo": ("authorize", "preview_token"),
            "analysis.real.preview": ("chat_id",), "analysis.real": ("chat_id", "authorize", "preview_token"),
            "analysis.review.preview": ("chat_id",), "analysis.review": ("chat_id", "authorize", "preview_token"),
            "task.change.resolve": ("change_id", "task_revision", "accept"),
            "tasks.list": (), "today.list": (), "task.update": ("task_id", "revision", "status"),
            "task.claim_self": ("task_id", "revision"),
            "meeting.save": ("title", "content", "meeting_date", "file_name"),
            "meeting.list": ("query", "limit", "offset"), "meeting.detail": ("meeting_id",),
            "meeting.preview": ("meeting_id", "revision"),
            "meeting.generate": ("meeting_id", "revision", "preview_token", "authorize"),
            "meeting.publish": ("meeting_id", "revision", "candidate_ids", "confirm"),
            "daily.preview": (), "daily.enable": ("preview_token", "authorize"), "daily.disable": ("revision",),
            "daily.preview_run": ("day",), "daily.run": ("day", "preview_token", "authorize"),
            "daily.detail": ("day",), "daily.list": ("limit", "offset"),
            "authorization.preview": (), "authorization.save": ("authorize", "preview_token"), "authorization.revoke": (),
            "analysis.batch.retry": ("batch_id", "authorize"),
        }
        if not isinstance(command, str) or command not in allowed or not isinstance(params, dict) or set(params) - set(allowed[command]):
            raise ServiceError("invalid_command", "请求格式或操作不受支持，未修改数据。")
        try:
            if command.startswith("ask.model."):
                method = command.removeprefix("ask.model.")
                if method == "test": return self.ask.connection_check(**params)
                result = getattr(self.ask.models, method)(**params)
                if method in {"save", "enable", "delete"}: self.ask.cancel_active()
                return result
            if command.startswith("ask."):
                if command == "ask.start":
                    return self.ask.execute(self.ask.start(**params), event_sink or (lambda packet: None))
                return getattr(self.ask, command.removeprefix("ask."))(**params)
            if command == "state":
                return self.state()
            if command.startswith("daily."):
                operation = {"daily.preview":self.daily_news.preview,"daily.enable":self.daily_news.enable,
                    "daily.disable":self.daily_news.disable,"daily.preview_run":self.daily_news.preview_run,
                    "daily.run":self.daily_news.run,"daily.detail":self.daily_news.detail,"daily.list":self.daily_news.list}[command]
                return operation(**params)
            if command.startswith("meeting."):
                operation = {"meeting.save": self.meetings.save, "meeting.list": self.meetings.list,
                    "meeting.detail": self.meetings.detail, "meeting.preview": self.meetings.preview,
                    "meeting.generate": self.meetings.generate, "meeting.publish": self.meetings.publish}[command]
                return operation(**params)
            if command == "chat.bulk.preview":
                return self.chat_bulk.preview(**params)
            if command == "chat.bulk.apply":
                return self.chat_bulk.apply(**params)
            if command == "model.calls":
                return self.models.calls(**params)
            if command == "authorization.preview":
                return self.automatic.preview()
            if command == "authorization.save":
                return self.automatic.save(**params)
            if command == "authorization.revoke":
                return self.automatic.revoke()
            if command == "analysis.batch.retry":
                return self.automatic.retry(**params)
            if command == "source.demo":
                return self.connect_demo()
            if command == "source.local":
                return self.connect_local(**params)
            if command == "source.encrypted":
                return self.connect_encrypted(**params)
            if command == "model.save":
                return self.models.save(**params)
            if command == "model.test":
                return self.models.test(**params)
            if command == "model.enable":
                return self.models.enable(**params)
            if command == "model.delete":
                return self.models.delete(**params)
            if command == "analysis.preview":
                return self.analysis.preview()
            if command == "analysis.demo":
                return self.analysis.run(**params)
            if command == "analysis.real.preview":
                return self.analysis.real_preview(**params)
            if command == "analysis.real":
                return self.analysis.run_real(**params)
            if command == "analysis.review.preview":
                return self.reanalysis.preview(**params)
            if command == "analysis.review":
                return self.reanalysis.run(**params)
            if command == "task.change.resolve":
                return self.reanalysis.resolve(**params)
            if command == "tasks.list":
                return self.analysis.tasks()
            if command == "today.list":
                return self.analysis.tasks(today=True)
            if command == "task.update":
                return self.analysis.update_task(**params)
            if command == "task.claim_self":
                return self.analysis.claim_self(**params)
            if command == "source.refresh":
                with self.exclusive_operation():
                    with self.reader_context(self.store.settings()["source"], metadata_only=True,
                                             cancelled=self.stop_event.is_set) as reader:
                        discovered = reader.discover()
                    self.ensure_open()
                    self.store.refresh_chats(discovered)
            elif command == "chat.save":
                self.store.update_chat(**params)
            elif command == "tag.save":
                return {"tag_id": self.store.save_tag(**params)}
            elif command == "tag.delete":
                self.store.delete_tag(**params)
            elif command == "range.save":
                if parse_time(params.get("time_start")) >= self.clock():
                    raise ServiceError("invalid_time_range", "开始时间需要早于现在。")
                self.store.set_range(**params)
            elif command == "sync.now":
                return self.sync()
            elif command == "sync.pause":
                return self.pause()
            elif command == "sync.resume":
                return self.resume()
            elif command == "messages.list":
                return self.store.messages(**params)
            elif command == "messages.chats":
                return self.store.message_chats(**params)
            elif command == "message.detail":
                return self.store.message_detail(**params)
            elif command == "demo.append":
                return append_demo(self.store.directory, self.store.settings()["source"], clock=self.clock)
            return self.state()
        except TypeError:
            raise ServiceError("invalid_parameters", "操作参数不完整或格式无效，未完成操作。") from None
        except sqlite3.Error:
            raise ServiceError("storage_failed", "本地存储操作失败，请检查磁盘空间后重试。", retryable=True) from None
