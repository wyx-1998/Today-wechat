"""Build and atomically publish one validated DB copy, never edit the source.

The caller supplies an authenticated page decoder and must obtain source
authorization before calling this module. This module does not discover paths,
load keys, elevate permissions, or establish a multi-database snapshot.
WAL layout: https://www.sqlite.org/fileformat2.html#walformat
"""

from contextlib import closing
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import struct
import tempfile


class RefreshError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WalCommit:
    frames: tuple
    database_pages: int | None


def checksum(data, big_endian, seed=(0, 0)):
    if len(data) % 8:
        raise RefreshError("wal_checksum_input")
    first, second = seed
    for left, right in struct.iter_unpack(">II" if big_endian else "<II", data):
        first = (first + left + second) & 0xffffffff
        second = (second + right + first) & 0xffffffff
    return first, second


def committed_wal(data, page_size=4096, *, cancelled=lambda: False):
    if cancelled():
        raise RefreshError("cancelled")
    if not data:
        return WalCommit((), None)
    if len(data) < 32:
        raise RefreshError("wal_header_incomplete")
    magic, version, size, _, salt1, salt2, check1, check2 = struct.unpack(">8I", data[:32])
    if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or size != page_size:
        raise RefreshError("wal_format_unsupported")
    big_endian = magic == 0x377f0683
    running = checksum(data[:24], big_endian)
    if running != (check1, check2):
        raise RefreshError("wal_header_checksum")
    frames, last_commit, database_pages = [], 0, None
    frame_size = page_size + 24
    for offset in range(32, len(data) - frame_size + 1, frame_size):
        if cancelled():
            raise RefreshError("cancelled")
        header = data[offset:offset + 24]
        pgno, pages, first_salt, second_salt, c1, c2 = struct.unpack(">6I", header)
        if (first_salt, second_salt) != (salt1, salt2) or pgno == 0:
            # The old/preallocated tail is not a second stream to scan past.
            break
        if pgno >= 0xffffffff or pages >= 0xffffffff:
            raise RefreshError("wal_page_invalid")
        page = bytes(data[offset + 24:offset + frame_size])
        running = checksum(page, big_endian, checksum(header[:8], big_endian, running))
        if running != (c1, c2):
            # Fail closed: do not promote a damaged prefix as a new snapshot.
            raise RefreshError("wal_frame_checksum")
        frames.append((pgno, page))
        if pages:
            last_commit, database_pages = len(frames), pages
    # Complete, checksummed but uncommitted frames never become visible.
    return WalCommit(tuple(frames[:last_commit]), database_pages)


def fingerprint(path, optional=False):
    try:
        value = Path(path).lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise RefreshError("source_missing") from None
    if not stat.S_ISREG(value.st_mode):
        raise RefreshError("non_regular_file")
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def validate_copy(path, page_size=4096):
    size = path.stat().st_size
    if size < page_size or size % page_size:
        raise RefreshError("copy_size_invalid")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            if db.execute("PRAGMA page_size").fetchone()[0] != page_size:
                raise RefreshError("copy_page_size_invalid")
            if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise RefreshError("copy_integrity_failed")
    except sqlite3.Error:
        raise RefreshError("copy_unreadable") from None


def refresh_snapshot(db_path, out_path, decode, *, full=True, use_wal=True,
                     page_size=4096, cancelled=lambda: False):
    source, target = Path(db_path).absolute(), Path(out_path).absolute()
    wal = Path(str(source) + "-wal")
    if source.resolve() == target.resolve() or wal.resolve() == target.resolve():
        raise RefreshError("source_target_overlap")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(target) + ".refresh.lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RefreshError("refresh_busy") from None
        sources = {source: fingerprint(source), wal: fingerprint(wal, optional=True)}
        previous = fingerprint(target, optional=True)
        if not full and previous is None:
            raise RefreshError("copy_missing")
        if sources[source][2] == 0 or sources[source][2] % page_size:
            raise RefreshError("source_page_incomplete")

        def check_cancelled():
            if cancelled():
                raise RefreshError("cancelled")

        def check_stable():
            check_cancelled()
            if any(fingerprint(path, optional=True) != version for path, version in sources.items()):
                raise RefreshError("source_changed")
            if fingerprint(target, optional=True) != previous:
                raise RefreshError("copy_changed")
            if any(Path(str(target) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise RefreshError("copy_sidecar_present")

        check_stable()
        wal_data = bytearray()
        if use_wal and sources[wal] is not None:
            with wal.open("rb") as original_wal:
                while True:
                    check_cancelled()
                    chunk = original_wal.read(1024 * 1024)
                    if not chunk:
                        break
                    wal_data.extend(chunk)
        commit = committed_wal(wal_data, page_size, cancelled=cancelled)
        base_pages = sources[source][2] // page_size
        if commit.database_pages is not None and commit.database_pages > base_pages + len(commit.frames):
            raise RefreshError("wal_size_invalid")
        with tempfile.TemporaryDirectory(prefix=".wechat-refresh-", dir=target.parent) as directory:
            candidate = Path(directory) / "candidate.sqlite3"
            fd = os.open(candidate, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w+b") as output:
                if full:
                    with source.open("rb") as original:
                        for pgno in range(1, base_pages + 1):
                            check_cancelled()
                            page = original.read(page_size)
                            if len(page) != page_size:
                                raise RefreshError("source_page_incomplete")
                            decoded = decode(page, pgno)
                            if len(decoded) != page_size:
                                raise RefreshError("decoded_page_invalid")
                            output.write(decoded)
                else:
                    with target.open("rb") as original:
                        while True:
                            check_cancelled()
                            chunk = original.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                for pgno, page in commit.frames:
                    check_cancelled()
                    if pgno > commit.database_pages:
                        continue
                    decoded = decode(page, pgno)
                    if len(decoded) != page_size:
                        raise RefreshError("decoded_page_invalid")
                    output.seek((pgno - 1) * page_size)
                    output.write(decoded)
                if commit.database_pages is not None:
                    output.truncate(commit.database_pages * page_size)
                output.flush()
                os.fsync(output.fileno())
            validate_copy(candidate, page_size)
            check_stable()
            os.replace(candidate, target)
        return {"base_pages": base_pages if full else 0,
                "committed_frames": len(commit.frames), "database_pages": commit.database_pages,
                "source_signatures": {"db": list(sources[source]),
                                      "wal": list(sources[wal]) if sources[wal] else None}}
    finally:
        os.close(lock_fd)
