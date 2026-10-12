"""Read-only local credential status; never prints credentials or private bodies."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def audit(path):
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_local_keys'").fetchone()
        local = db.execute("SELECT COUNT(*) FROM model_local_keys WHERE capability='text'").fetchone()[0] if exists else 0
        config = db.execute("SELECT revision,enabled FROM model_configs WHERE capability='text'").fetchone()
        paused = json.loads(db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0])
        outcomes = dict(db.execute("SELECT outcome,COUNT(*) FROM model_calls GROUP BY outcome"))
        return {"schema_version": version, "credential_storage": "not_configured" if not config else "sqlite_plaintext" if local else "keychain_encrypted",
                "local_credential_present": bool(local), "config_revision": config[0] if config else None,
                "model_enabled": bool(config[1]) if config else False, "paused": paused,
                "model_calls": sum(outcomes.values()), "model_outcomes": outcomes,
                "tasks": db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "task_statuses": dict(db.execute("SELECT status,COUNT(*) FROM tasks GROUP BY status")),
                "database_mode": oct(path.stat().st_mode & 0o777)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    try:
        result = audit(args.database)
    except (OSError, sqlite3.Error, ValueError):
        print(json.dumps({"error": "local_storage_audit_unavailable"}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
