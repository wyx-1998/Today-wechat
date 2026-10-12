"""Bounded DB/WAL-pair capture, before any expensive decoding.

Captured pairs may have different timestamps. This is not a cross-database
transaction. File mutation/replacement during a pair capture fails closed;
later source activity cannot mutate the accepted private copy.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import stat
import tempfile
import time

from backend.platform_files import capture_open_file
from backend.refresh import RefreshError, fingerprint


def stat_version(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def pair_versions(database):
    if database.parent.is_symlink():
        raise RefreshError("non_regular_file")
    return {database: fingerprint(database), Path(str(database) + "-wal"): fingerprint(str(database) + "-wal", optional=True)}


def capture_pair(database, destination, *, cancelled=lambda: False):
    database, destination = Path(database), Path(destination)
    if (destination == database or destination.is_symlink() or destination.exists()
            or Path(str(destination) + "-wal").is_symlink() or Path(str(destination) + "-wal").exists()):
        raise RefreshError("capture_destination_exists")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    begun = time.monotonic()
    for attempt in range(1, 4):
        if cancelled():
            raise RefreshError("cancelled")
        try:
            versions = pair_versions(database)
            with tempfile.TemporaryDirectory(prefix=".capture-pair-", dir=destination.parent) as directory, ExitStack() as handles:
                copies, methods = {}, []
                for path, version in versions.items():
                    if version is None:
                        continue
                    try:
                        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                    except FileNotFoundError:
                        raise RefreshError("source_changed") from None
                    handles.callback(os.close, fd)
                    opened = os.fstat(fd)
                    if not stat.S_ISREG(opened.st_mode) or stat_version(opened) != version:
                        raise RefreshError("source_changed")
                    candidate = Path(directory) / path.name
                    methods.append(capture_open_file(fd, candidate, cancelled=cancelled))
                    copies[path] = (candidate, fd)
                if cancelled():
                    raise RefreshError("cancelled")
                if pair_versions(database) != versions or any(stat_version(os.fstat(fd)) != versions[path] for path, (_, fd) in copies.items()):
                    raise RefreshError("source_changed")
                for path, (candidate, _) in copies.items():
                    os.replace(candidate, destination if path == database else Path(str(destination) + "-wal"))
                return versions, {"attempts": attempt, "clone_files": methods.count("clone"), "copied_files": methods.count("copy"),
                                  "input_bytes": sum(v[2] for v in versions.values() if v),
                                  "duration_ms": round((time.monotonic() - begun) * 1000, 3)}
        except RefreshError as error:
            if error.code != "source_changed" or attempt == 3:
                raise
            # Small bounded backoff; cancellation is observed within 20 ms.
            deadline = time.monotonic() + 0.1 * attempt
            while time.monotonic() < deadline:
                if cancelled():
                    raise RefreshError("cancelled")
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
