"""Read-only candidate diagnostics. Never prints bodies, titles, reasons or keys.

Source membership and processing coverage do not prove semantic task recall.
"""

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from backend.contracts import DISCOVERY_REJECTION_RULES, ServiceError


def invalid():
    raise ServiceError("candidate_audit_invalid", "候选核对记录不完整或来源不兼容，不能判定处理覆盖。")


def identifiers(value):
    try:
        result = json.loads(value)
    except (ValueError, TypeError):
        invalid()
    if (not isinstance(result, list) or any(not isinstance(mid, str) or not mid for mid in result)
            or len(result) != len(set(result))):
        invalid()
    return result


def audit(path, batch_id, watched=()):
    with closing(sqlite3.connect(Path(path).resolve(strict=True).as_uri()+"?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        parent = db.execute("SELECT account_id,message_ids,status FROM analysis_runs WHERE batch_id=?", (batch_id,)).fetchone()
        if parent is None:
            raise ServiceError("candidate_audit_batch", "所选分析批次不存在，未读取其它批次。")
        fresh = set(identifiers(parent["message_ids"]))
        if not set(watched).issubset(fresh):
            raise ServiceError("candidate_audit_scope", "目标消息不属于所选批次，未扩大核对范围。")
        context_row = db.execute("SELECT message_ids FROM text_batch_contexts WHERE batch_id=?", (batch_id,)).fetchone()
        context = set(identifiers(context_row[0])) if context_row else set()
        if fresh & context:
            invalid()
        allowed = fresh | context
        sources = {}
        for mid in allowed:
            row = db.execute("""SELECT account_id,chat_id,sent_epoch,message_type,content_state,analysis_eligible,
                content IS NOT NULL AS has_content FROM messages WHERE message_id=?""", (mid,)).fetchone()
            if (row is None or row["account_id"] != parent["account_id"] or row["message_type"] != "text"
                    or row["content_state"] != "text_available" or row["analysis_eligible"] != 1 or not row["has_content"]):
                invalid()
            sources[mid] = dict(row)
        attempts = db.execute("SELECT attempt_id,policy_version FROM discovery_attempts WHERE batch_id=? ORDER BY rowid", (batch_id,)).fetchall()
        base = {"parent_status": parent["status"], "attempt_count": len(attempts),
                "input_message_count": len(fresh), "semantic_completeness": "not_evaluated",
                "identity_interpretation": "not_evaluated"}
        if not attempts:
            return base | {"diagnostic_status": "no_candidate_records", "focal_coverage_complete": False,
                           "decision_coverage_complete": False, "watched": []}
        attempt = attempts[-1][0]
        policy = attempts[-1]["policy_version"]
        if policy not in {1, 2, 3}:
            invalid()
        windows = [dict(r) for r in db.execute("SELECT * FROM discovery_windows WHERE attempt_id=? ORDER BY window_index", (attempt,))]
        candidates = [dict(r) for r in db.execute("SELECT * FROM discovery_candidates WHERE attempt_id=? ORDER BY rowid", (attempt,))]
        if [w["window_index"] for w in windows] != list(range(len(windows))):
            invalid()
        window_sources, focus = {}, Counter()
        for window in windows:
            focal, nearby = identifiers(window["focal_message_ids"]), identifiers(window["context_message_ids"])
            if not focal or not set(focal).issubset(fresh) or not set(nearby).issubset(allowed) or set(focal) & set(nearby):
                invalid()
            combined = set(focal + nearby)
            if len({(sources[mid]["account_id"], sources[mid]["chat_id"]) for mid in combined}) != 1:
                invalid()
            focus.update(focal)
            window_sources[window["window_index"]] = (set(focal), combined)
            if window["candidate_count"] != sum(c["window_index"] == window["window_index"] for c in candidates):
                invalid()
        if any(n != 1 for n in focus.values()):
            invalid()
        decisions = Counter()
        candidate_refs = {}
        for candidate in candidates:
            if candidate["window_index"] not in window_sources:
                invalid()
            refs = set(identifiers(candidate["source_message_ids"]))
            focal, combined = window_sources[candidate["window_index"]]
            if not refs or not refs.issubset(combined) or not refs & focal:
                invalid()
            candidate_refs[candidate["candidate_id"]] = refs
            outcome = candidate["outcome"]
            if outcome is None:
                if any(candidate[k] is not None for k in ("task_index", "decision_source_message_ids", "reason")):
                    invalid()
                decisions["undecided"] += 1
                continue
            decision_refs = set(identifiers(candidate["decision_source_message_ids"]))
            if (outcome not in {"retained", "fulfilled", "cancelled", "not_actionable"}
                    or not decision_refs.issubset(allowed) or not decision_refs & refs
                    or not isinstance(candidate["reason"], str) or not candidate["reason"].strip()):
                invalid()
            if outcome == "retained":
                if type(candidate["task_index"]) is not int or candidate["task_index"] < 0:
                    invalid()
            elif candidate["task_index"] is not None:
                invalid()
            if outcome in {"fulfilled", "cancelled"}:
                boundary = (max if policy >= 2 else min)(sources[mid]["sent_epoch"] for mid in refs)
                scope = {(sources[mid]["account_id"], sources[mid]["chat_id"]) for mid in refs}
                if not any(sources[mid]["sent_epoch"] > boundary and (policy == 1 or
                        (sources[mid]["account_id"], sources[mid]["chat_id"]) in scope)
                        for mid in decision_refs - refs):
                    invalid()
            decisions[outcome] += 1
        # Pre-v7 attempts have no failure metadata. Never invent or backfill it.
        rejection = None
        rejections = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_rejections'").fetchone():
            rejections = [dict(r) for r in db.execute("SELECT candidate_id,rule FROM discovery_rejections WHERE attempt_id=? ORDER BY rowid", (attempt,))]
            for item in rejections:
                cid = item["candidate_id"]
                if (item["rule"] not in DISCOVERY_REJECTION_RULES
                        or (cid is not None and cid not in candidate_refs)
                        or (cid is not None and any(c["candidate_id"] == cid and c["outcome"] is not None for c in candidates))
                        or (policy < 3 and (parent["status"] == "success" or any(c["outcome"] is not None for c in candidates)))):
                    invalid()
            rejection = rejections[0] if rejections else None
        focal_complete = set(focus) == fresh
        # Zero candidates only means a complete decision ledger when every focal
        # message was processed. Before the first response, it is not success.
        decision_complete = focal_complete and not decisions["undecided"]
        watched_report = []
        for number, mid in enumerate(watched, 1):
            matching = [c for c in candidates if mid in candidate_refs[c["candidate_id"]]]
            watched_report.append({"message_number": number, "focal_processed": mid in focus,
                "candidate_count": len(matching),
                "outcomes": dict(Counter(c["outcome"] or "undecided" for c in matching))})
        return base | {"attempt_id": attempt, "policy_version": policy,
            "diagnostic_status": "recorded" if windows else "awaiting_valid_window",
            "window_count": len(windows), "candidate_count": len(candidates), "focal_processed": len(focus),
            "focal_coverage_complete": focal_complete, "decision_coverage_complete": decision_complete,
            "decisions": dict(decisions), "watched": watched_report,
            "rejection": rejection,
            "rejections": rejections,
            "rejected_candidate_count": len({r["candidate_id"] for r in rejections if r["candidate_id"] is not None}),
            "partial_publication": parent["status"] == "success" and bool(rejections),
            "publication_success": parent["status"] == "success"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--message-id", action="append", default=[])
    args = parser.parse_args()
    try:
        report = audit(args.database, args.batch_id, args.message_id)
    except ServiceError as error:
        print(json.dumps({"audit_failed": True, "error_code": error.code}))
        return 1
    except (OSError, sqlite3.Error):
        print(json.dumps({"audit_failed": True, "error_code": "candidate_audit_unavailable"}))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
