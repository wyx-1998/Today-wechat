"""External, read-only coverage audit. Never launches sync/models or prints bodies.

Run only after explicit permission for the named source and interval. Counts
prove input coverage, not semantic task recall; compare expected tasks separately.
"""

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from backend.contracts import ReadScope, ServiceError, parse_time, stable_id
from backend.reader import SourceReader, quote_identifier, source_database


def content_hash(value):
    if value is None:
        return None
    try:
        if isinstance(value, bytes):
            if value.startswith(b"\x28\xb5\x2f\xfd"):
                try:
                    import zstandard
                except ImportError:
                    raise ServiceError("audit_codec_missing", "核对环境缺少压缩解码依赖，不能宣称正文一致。") from None
                value = zstandard.ZstdDecompressor().decompress(value, max_output_size=16*1024*1024)
            value = value.decode("utf-8")
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    except ServiceError:
        raise
    except Exception:
        # No payloads in errors. Failed decoding is an explicit coverage gap.
        return None


def source_inventory(reader, scope):
    """Independent full interval query: no production sync cursor or read_batch."""
    inventory, failures = {}, {}
    for chat in scope.chat_ids:
        records = {}
        try:
            for stream_id, path, table in reader.streams(chat):
                with source_database(path) as db:
                    columns = {r[1] for r in db.execute(f"PRAGMA table_info({quote_identifier(table)})")}
                    def find(options):
                        return next((name for name in options if name in columns), None)
                    local = find(("local_id", "MsgLocalID")) or "rowid"
                    timing = find(("create_time", "msg_time", "CreateTime"))
                    kind = find(("local_type", "MsgType", "Type"))
                    server = find(("server_id", "MsgSvrID"))
                    body = find(("message_content", "compress_content", "content", "Content"))
                    if not timing:
                        raise ServiceError("missing_time", "无法核对时间范围。")
                    fields = ",".join(quote_identifier(c) if c else "NULL" for c in (local, kind, server))
                    fields += "," + (f"CASE WHEN CAST({quote_identifier(kind)} AS INTEGER)=1 THEN {quote_identifier(body)} ELSE NULL END" if kind and body else "NULL")
                    query = f"SELECT {fields} FROM {quote_identifier(table)} WHERE {quote_identifier(timing)}>=? AND {quote_identifier(timing)}<?"
                    for lid, raw_type, server_id, content in db.execute(query, (parse_time(scope.time_start), parse_time(scope.time_end))):
                        identity = ("server", str(server_id)) if server_id not in (None, 0, "0", "") else ("local", stream_id, str(lid))
                        mid = stable_id(scope.account_id, chat, *identity)
                        try:
                            is_text = int(raw_type) == 1
                        except (ValueError, TypeError):
                            is_text = False
                        record = {"is_text": is_text, "content_hash": content_hash(content) if is_text else None}
                        if mid in records and records[mid] != record:
                            raise ServiceError("source_identity_conflict", "来源身份重复且类型冲突。")
                        records[mid] = record
            inventory[chat] = records
        except ServiceError as error:
            inventory[chat] = None
            failures[chat] = error.code
    return inventory, failures


def audit_application(path, scope, *, inventory=None, failures=None):
    """Open existing SQLite read-only, one snapshot. No Store/LocalService startup."""
    if not scope.chat_ids:
        raise ServiceError("empty_audit_scope", "核对需要明确选择会话。")
    start, end = parse_time(scope.time_start), parse_time(scope.time_end)
    placeholders = ",".join("?" for _ in scope.chat_ids)
    where = f"m.account_id=? AND m.chat_id IN ({placeholders}) AND m.sent_epoch>=? AND m.sent_epoch<?"
    params = (scope.account_id, *scope.chat_ids, start, end)
    with closing(sqlite3.connect(Path(path).resolve(strict=True).as_uri()+"?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        messages = [dict(r) for r in db.execute(f"SELECT m.message_id,m.chat_id,m.message_type,m.content_state,m.analysis_eligible,m.is_self,m.content FROM messages m WHERE {where}", params)]
        rows = {r["message_id"]: r for r in messages}
        eligible = {mid for mid,r in rows.items() if r["message_type"] == "text" and r["content_state"] == "text_available" and r["analysis_eligible"]}
        runs = [dict(r) for r in db.execute(f"""SELECT DISTINCT r.batch_id,r.status,r.message_ids FROM analysis_runs r
            JOIN json_each(r.message_ids) j JOIN messages m ON m.message_id=j.value
            WHERE r.account_id=? AND {where}""", (scope.account_id, *params))]
        success, running, failed = set(), set(), set()
        submissions = Counter()
        invalid_inputs = 0
        context_by_run, context_uses, invalid_context = {}, 0, 0
        review_uses, invalid_reviews = 0, 0
        context_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_batch_contexts'").fetchone()
        review_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_runs'").fetchone()
        link_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_evidence_links'").fetchone()
        for run in runs:
            mids = json.loads(run["message_ids"])
            review = db.execute("SELECT * FROM review_runs WHERE batch_id=?", (run["batch_id"],)).fetchone() if review_table else None
            if review:
                try:
                    if review["policy_version"] != 1 or len(mids) != len(set(mids)):
                        raise ValueError()
                    reviewed = []
                    for mid in mids:
                        row = db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone()
                        if (not row or row["account_id"] != scope.account_id or row["chat_id"] != review["chat_id"]
                                or row["message_type"] != "text" or row["content_state"] != "text_available" or not row["analysis_eligible"]
                                or not parse_time(review["time_start"]) <= row["sent_epoch"] < parse_time(review["time_end"])):
                            raise ValueError()
                        reviewed.append(row)
                    digest = stable_id([(r["message_id"],r["account_id"],r["chat_id"],r["sender_id"],r["sent_at"],
                        r["sent_epoch"],r["content"],r["is_self"],r["analysis_eligible"],r["message_type"],r["content_state"]) for r in reviewed])
                    if digest != review["input_hash"]:
                        raise ValueError()
                    if run["status"] == "success":
                        review_uses += len(mids)
                except (ValueError, TypeError, KeyError, ServiceError):
                    invalid_reviews += 1
            context_by_run[run["batch_id"]] = set()
            batch = db.execute("SELECT b.*,g.scope_json FROM text_batches b JOIN text_grants g USING(grant_id) WHERE b.batch_id=?",
                (run["batch_id"],)).fetchone()
            if batch and context_table:
                manifest = db.execute("SELECT * FROM text_batch_contexts WHERE batch_id=?", (run["batch_id"],)).fetchone()
                try:
                    if not manifest:
                        raise ValueError()
                    context_ids = json.loads(manifest["message_ids"])
                    grant_scope = json.loads(batch["scope_json"])
                    if (not isinstance(context_ids, list) or any(not isinstance(mid, str) for mid in context_ids)
                            or len(context_ids) != len(set(context_ids)) or set(context_ids).intersection(mids)
                            or manifest["policy_version"] not in {0, 1}
                            or (manifest["policy_version"] == 0 and context_ids)
                            or batch["chat_id"] not in grant_scope["chat_ids"]):
                        raise ValueError()
                    context_rows = []
                    for mid in context_ids:
                        row = db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone()
                        if (not row or row["account_id"] != scope.account_id or row["chat_id"] != batch["chat_id"]
                                or row["message_type"] != "text" or row["content_state"] != "text_available" or not row["analysis_eligible"]
                                or not parse_time(grant_scope["time_start"]) <= row["sent_epoch"] < parse_time(batch["created_at"])):
                            raise ValueError()
                        context_rows.append(row)
                    digest = stable_id([(r["message_id"],r["content"],r["sent_at"],r["is_self"],r["message_type"],
                        r["analysis_eligible"],r["sender_id"],r["account_id"],r["chat_id"]) for r in context_rows])
                    if digest != manifest["input_hash"]:
                        raise ValueError()
                    context_by_run[run["batch_id"]] = set(context_ids)
                    if run["status"] == "success":
                        context_uses += len(context_ids)
                except (ValueError, TypeError, KeyError, ServiceError):
                    invalid_context += 1
            for mid in mids:
                if mid in eligible:
                    if run["status"] == "success":
                        success.add(mid)
                        if not review:
                            submissions[mid] += 1
                    elif run["status"] == "running":
                        running.add(mid)
                    elif run["status"] == "failed":
                        failed.add(mid)
                elif mid in rows:
                    invalid_inputs += 1
        tasks, invalid_evidence = [], 0
        for run in runs:
            for task in db.execute("SELECT task_id,source_message_ids,status,owner_type FROM tasks WHERE account_id=? AND batch_id=?", (scope.account_id, run["batch_id"])):
                refs = json.loads(task["source_message_ids"])
                if not isinstance(refs, list) or not refs or any(not isinstance(mid, str) for mid in refs):
                    invalid_evidence += 1
                    continue
                if not any(mid in rows for mid in refs):
                    continue
                tasks.append(task)
                fresh_ids = set(json.loads(run["message_ids"]))
                allowed_refs = fresh_ids | context_by_run[run["batch_id"]]
                if link_table:
                    for link in db.execute("SELECT * FROM task_evidence_links WHERE task_id=?", (task["task_id"],)):
                        linked_run = db.execute("SELECT account_id,status,message_ids FROM analysis_runs WHERE batch_id=?", (link["batch_id"],)).fetchone()
                        linked_refs = json.loads(link["source_message_ids"])
                        if (not linked_run or linked_run["account_id"] != scope.account_id or linked_run["status"] != "success"
                                or not isinstance(linked_refs, list) or not linked_refs
                                or any(not isinstance(mid, str) for mid in linked_refs)
                                or len(linked_refs) != len(set(linked_refs))):
                            invalid_evidence += 1
                            continue
                        members = set(json.loads(linked_run["message_ids"]))
                        if not members.intersection(linked_refs) or not set(linked_refs).issubset(members | context_by_run.get(link["batch_id"], set())):
                            invalid_evidence += 1
                            continue
                        allowed_refs.update(linked_refs)
                if not fresh_ids.intersection(refs):
                    invalid_evidence += 1
                # Valid evidence must exist in this account and be an actual
                # text member of its originating run, not merely any message.
                for mid in refs:
                    evidence = db.execute("SELECT account_id,message_type,content_state FROM messages WHERE message_id=?", (mid,)).fetchone()
                    if not evidence or evidence["account_id"] != scope.account_id or evidence["message_type"] != "text" or evidence["content_state"] != "text_available" or mid not in allowed_refs:
                        invalid_evidence += 1
        chats = []
        for index,chat in enumerate(scope.chat_ids, 1):
            saved = {mid for mid,r in rows.items() if r["chat_id"] == chat}
            text = saved & eligible
            truth = inventory.get(chat) if inventory is not None else None
            counts = {"chat_number": index, "saved_messages": len(saved), "eligible_text": len(text),
                "unreadable_text": sum(rows[mid]["message_type"] == "text" and mid not in eligible for mid in saved),
                "non_text": sum(rows[mid]["message_type"] != "text" for mid in saved),
                "unknown_sender_text": sum(rows[mid]["is_self"] is None for mid in text),
                "success_text": len(text & success), "unprocessed_text": len(text-success),
                "running_text": len(text & running), "failed_text": len(text & failed),
                "source_messages": len(truth) if truth is not None else None,
                "source_text": sum(r["is_text"] for r in truth.values()) if truth is not None else None,
                "missing_from_application": len(set(truth)-saved) if truth is not None else None,
                "not_in_current_source": len(saved-set(truth)) if truth is not None else None,
                "source_unreadable_text": sum(r["is_text"] and r["content_hash"] is None for r in truth.values()) if truth is not None else None,
                "text_content_mismatches": sum(truth[mid]["is_text"] and truth[mid]["content_hash"] is not None and truth[mid]["content_hash"] != content_hash(rows[mid]["content"]) for mid in saved & set(truth)) if truth is not None else None,
                "source_error": (failures or {}).get(chat)}
            chats.append(counts)
    consistent = (inventory is not None and all(c["source_messages"] is not None and not c["source_error"] and
        not c["missing_from_application"] and not c["not_in_current_source"] and not c["source_unreadable_text"] and not c["text_content_mismatches"] for c in chats))
    covered = (consistent and all(c["source_messages"] is not None and not c["source_error"] and
        not c["missing_from_application"] and not c["not_in_current_source"] and not c["unreadable_text"] and not c["unprocessed_text"] for c in chats)
        and not invalid_inputs and not invalid_evidence and not invalid_context and not invalid_reviews and not any(n>1 for n in submissions.values()))
    return {"time_start": scope.time_start, "time_end": scope.time_end, "chats": chats,
        "tasks_with_in_scope_evidence": len(tasks), "task_states": dict(Counter(t["status"] for t in tasks)),
        "task_owners": dict(Counter(t["owner_type"] for t in tasks)), "invalid_task_source_refs": invalid_evidence,
        "context_text_uses": context_uses, "invalid_context_manifests": invalid_context,
        "review_text_uses": review_uses, "invalid_review_manifests": invalid_reviews,
        "ineligible_analysis_inputs": invalid_inputs, "text_in_multiple_successful_runs": sum(n>1 for n in submissions.values()),
        "text_input_coverage_complete": bool(covered), "content_consistency": "matched" if consistent else "not_evaluated" if inventory is None else "unverified_or_mismatch",
        "semantic_task_completeness": "not_evaluated"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--chat-name", action="append", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--allow-local-read", action="store_true")
    args = parser.parse_args()
    if not args.allow_local_read:
        parser.error("Explicit local source-read permission is required; no databases opened")
    try:
        path = Path(args.database).resolve(strict=True)
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT value FROM settings WHERE key='source'").fetchone()
            source = json.loads(row[0]) if row else None
            if not source or source.get("mode") not in {"local", "encrypted"}:
                raise ServiceError("local_source_required", "请选择本地来源。")
            selected = []
            for name in args.chat_name:
                matches = db.execute("SELECT chat_id FROM chats WHERE account_id=? AND chat_name=?", (source["account_id"], name)).fetchall()
                if len(matches) != 1:
                    raise ServiceError("audit_chat_match", "会话名称未唯一匹配，未读取来源。")
                selected.append(matches[0][0])
        scope = ReadScope(source["account_id"], tuple(selected), args.start, args.end)
        if source["mode"] == "encrypted":
            from backend.encrypted_source import encrypted_reader
            with tempfile.TemporaryDirectory(prefix="wechat-today-private-audit-") as working:
                with encrypted_reader(source, Path(working)) as reader:
                    inventory, failures = source_inventory(reader, scope)
        else:
            inventory, failures = source_inventory(SourceReader(source["root"], scope.account_id, source.get("self_id")), scope)
        result = audit_application(path, scope, inventory=inventory, failures=failures)
    except (ServiceError, OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
        result = {"audit_failed": True, "error_code": error.code if isinstance(error, ServiceError) else "audit_unavailable",
            "text_input_coverage_complete": False, "semantic_task_completeness": "not_evaluated"}
        print(json.dumps(result))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
