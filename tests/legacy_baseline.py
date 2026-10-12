"""Observe the existing collector on synthetic DBs without freezing its bugs.

Exit code 0 means the diagnostic ran, NOT that product requirements passed.
Fixing collector.py may reduce 'gap' values; no test requires a bug to persist.
"""

from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from tests.fixture_support import database, fixture_environment, install_commit_failure, load_suite


COLLECTOR_SOURCE = Path(__file__).resolve().parents[1] / "scripts/collector.py"


def _legacy_read(case, repeat=1, commit_failure=False):
    spec = importlib.util.spec_from_file_location("synthetic_legacy_collector", COLLECTOR_SOURCE)
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    with fixture_environment(case) as paths:
        collector.MSG_DIR = str(paths.message_db.parent)
        collector.CONTACT_DB = str(paths.contact_db)
        collector.COLLECTOR_DB = str(paths.collector_db)
        collector.SELF_WXID = case["self_id"]
        collector._names = {p["sender_id"]: p["display_name"] for p in case["contacts"]}
        collector._names.update({c["id"]: c["name"] for c in case["chats"]})
        original_connect = sqlite3.connect
        opened = []

        def tracked_connect(target, *args, **kwargs):
            # Reject any accidental access outside this generated fixture.
            target_text = str(target)
            if target_text.startswith("file:"):
                target_text = target_text[5:].split("?", 1)[0]
            if not Path(target_text).resolve().is_relative_to(paths.root.resolve()):
                raise AssertionError("Legacy probe attempted a non-fixture database")
            connection = original_connect(target, *args, **kwargs)
            opened.append(connection)
            if commit_failure and Path(target_text) == paths.collector_db:
                install_commit_failure(connection)
            return connection

        runs = []
        try:
            with patch.object(collector.sqlite3, "connect", side_effect=tracked_connect), redirect_stdout(StringIO()):
                for _ in range(repeat):
                    # Explicitly use the same starting cursor for the replay probe.
                    runs.append(collector.sync_one(case["selected_chats"][0], "0"))
        finally:
            for connection in opened:
                connection.close()
        with database(paths.collector_db) as db:
            rows = db.execute("SELECT chatroom_id,local_id,sender,content FROM messages ORDER BY id").fetchall()
        return runs, rows


def observe_legacy():
    cases, expected = load_suite()
    reports = []
    _, rows = _legacy_read(cases["long_text"])
    wanted_length = expected["long_text"]["content_lengths"]["long"]
    length = len(rows[0][3]) if rows else None
    reports.append({"check": "full_original", "task": "READ-005", "expected_length": wanted_length, "observed_length": length, "gap": length != wanted_length})

    _, rows = _legacy_read(cases["seven_day_boundary"])
    wanted_count = len(expected["seven_day_boundary"]["stored_labels"])
    wanted_ids = {msg["local_id"] for msg in cases["seven_day_boundary"]["messages"] if msg["label"] in expected["seven_day_boundary"]["stored_labels"]}
    actual_ids = {row[1] for row in rows}
    reports.append({"check": "exact_message_window", "task": "READ-003", "expected_count": wanted_count, "observed_count": len(rows), "gap": actual_ids != wanted_ids})

    _, rows = _legacy_read(cases["unknown_sender"])
    sender = rows[0][2] if rows else None
    reports.append({"check": "unknown_sender", "task": "READ-006", "expected": "unknown identity", "observed": sender, "gap": bool(sender)})

    empty, _ = _legacy_read(cases["empty_table"])
    missing_case = dict(cases["missing_table"])
    missing_case["selected_chats"] = [missing_case["selected_chats"][1]]
    missing, _ = _legacy_read(missing_case)
    reports.append({"check": "empty_vs_missing_table", "task": "READ-001 / READ-011", "empty_return": empty[0], "missing_return": missing[0], "gap": empty[0] == missing[0]})

    runs, rows = _legacy_read(cases["commit_failure"], commit_failure=True)
    reports.append({"check": "commit_failure_count", "task": "READ-007", "reported_inserted": runs[0][0], "persisted_count": len(rows), "returned_cursor": runs[0][1], "gap": runs[0][0] != len(rows)})

    runs, rows = _legacy_read(cases["repeated_sync"], repeat=2)
    wanted_count = len(expected["repeated_sync"]["stored_labels"])
    reports.append({"check": "message_insert_dedup", "task": "READ-007", "expected_count": wanted_count, "stored_count": len(rows), "rounds": runs, "gap": len(rows) != wanted_count or runs[1][0] != 0, "scope": "message inserts only; does not prove cursor atomicity"})
    return {"synthetic_only": True, "product_acceptance": False, "observations": reports}


if __name__ == "__main__":
    with patch("socket.socket", side_effect=AssertionError("No network in synthetic baseline")), patch("socket.create_connection", side_effect=AssertionError("No network in synthetic baseline")):
        print(json.dumps(observe_legacy(), ensure_ascii=False, indent=2))
