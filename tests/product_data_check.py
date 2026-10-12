"""Read-only hashes of all application tables; optional private SQLite backup."""
import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3


def snapshot(db):
    hashes={}
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        escaped=name.replace('"','""')
        rows=[repr(tuple(row)) for row in db.execute(f'SELECT * FROM "{escaped}"')]
        hashes[name]=hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest()
    settings={key:json.loads(value) for key,value in db.execute("SELECT key,value FROM settings WHERE key IN ('paused','revision')")}
    return dict(hashes=hashes,version=db.execute("PRAGMA user_version").fetchone()[0],settings=settings,
        tasks=db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],calls=db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0])


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--database",required=True);parser.add_argument("--baseline",required=True);parser.add_argument("--capture",action="store_true")
    args=parser.parse_args();path=Path(args.database).resolve(strict=True);baseline=Path(args.baseline)
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)) as db:
        result=snapshot(db)
        if args.capture:
            backupdir=path.parent/"backups";backupdir.mkdir(exist_ok=True)
            backup=backupdir/f"before-settings-ui-{datetime.now().strftime('%Y%m%d-%H%M%S')}.sqlite3"
            # Exclusive create and restrictive mode before any sensitive bytes.
            with backup.open("xb") as stream: backup.chmod(0o600)
            with closing(sqlite3.connect(backup)) as target: db.backup(target)
            with baseline.open("x",encoding="utf-8") as stream:
                baseline.chmod(0o600);json.dump(result,stream)
            print(json.dumps(dict(captured=True,tables=len(result["hashes"]),version=result["version"],tasks=result["tasks"],calls=result["calls"],paused=result["settings"]["paused"],private_backup=True)))
        else:
            expected=json.loads(baseline.read_text())
            changed=[name for name,value in expected["hashes"].items() if result["hashes"].get(name)!=value]
            assert not changed, "Tables changed: "+", ".join(changed)
            assert result["settings"]==expected["settings"]
            print(json.dumps(dict(all_tables_unchanged=True,tables=len(result["hashes"]),tasks=result["tasks"],calls=result["calls"],paused=result["settings"]["paused"])))


if __name__=="__main__": main()
